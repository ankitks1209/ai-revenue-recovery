"""M5.3.1 — Payment Link result ingestion → EXECUTING -> RECOVERED/FAILED.

Uses existing webhook verification + webhook_events uniqueness + SessionLocal atomic
lifecycle + RecoveryAttempt + operator audit. No live Razorpay calls.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import update

from src.database import (
    FailedPayment as FailedPaymentModel,
    OperatorAuditModel,
    RecoveryAttemptModel,
    RecoveryLifecycleModel,
    SessionLocal,
)
from src.application.recommend_recovery import RecommendRecovery
from src.domain.audit import ActionType, AuditEvent, Outcome as AuditOutcome, ReasonCode
from src.domain.escalation import EscalationPolicy
from src.domain.masking import MaskingPolicy
from src.domain.recovery_lifecycle import (
    HardStopViolation,
    InvalidTransitionError,
    RecoveryLifecycle,
    RecoveryState,
    transition,
)
from src.domain.webhook_events import (
    InvalidSignatureError,
    MalformedPayloadError,
    MissingEventIdError,
    WebhookIngestResult,
    WebhookIngestStatus,
)
from src.infrastructure.ports_webhook import WebhookEventRepositoryPort, WebhookSignatureVerifierPort
from src.infrastructure.razorpay.payment_link_payload_mapper import parse_payment_link_payload
from src.infrastructure.razorpay.razorpay_recovery_rail import _reference_id
from src.infrastructure.recovery_lifecycle_repository import RecoveryLifecycleRepository
from src.infrastructure.repository import SQLiteFailedPaymentRepository


class IngestPaymentLinkResult:
    """Orchestrates verified payment_link webhook → lifecycle + attempt + audit."""

    def __init__(
        self,
        verifier: WebhookSignatureVerifierPort,
        webhook_repo: WebhookEventRepositoryPort,
        payment_repository=None,
        lifecycle_repository=None,
        session_factory=SessionLocal,
        masking: MaskingPolicy | None = None,
        recommender: RecommendRecovery | None = None,
        escalation_policy: EscalationPolicy | None = None,
    ) -> None:
        self._verifier = verifier
        self._webhook_repo = webhook_repo
        self._payment_repo = payment_repository or SQLiteFailedPaymentRepository(session_factory=session_factory)
        self._lifecycle_repo = lifecycle_repository or RecoveryLifecycleRepository(session_factory)
        self._session_factory = session_factory
        self._masking = masking or MaskingPolicy()
        self._recommender = recommender
        self._escalation = escalation_policy

    def _correlate_txn_id(self, parsed) -> Optional[str]:
        """Conservative correlation. Returns txn_id or None if ambiguous."""
        # reference_id must be present to prove same deterministic id
        if not parsed.reference_id:
            # Even notes alone are not authoritative without reference_id proof
            return None

        # Prefer lossless notes txn_id + attempt when both present
        if parsed.notes_txn_id and parsed.notes_attempt is not None:
            try:
                candidate = parsed.notes_txn_id.strip()
                attempt = int(parsed.notes_attempt)
            except Exception:
                return None
            if not candidate:
                return None
            expected = _reference_id(candidate, attempt)
            if expected != parsed.reference_id:
                return None
            # Verify payment exists
            try:
                payment = self._payment_repo.get_payment_by_id(candidate)
            except Exception:
                return None
            if payment is None:
                return None
            return candidate

        # Notes incomplete or absent — try inference from reference_id
        # Never guess truncated: expected must equal reference_id exactly
        ref = parsed.reference_id.strip()
        if "_" not in ref:
            return None
        prefix, suffix = ref.rsplit("_", 1)
        try:
            attempt = int(suffix)
        except Exception:
            return None
        if not prefix:
            return None
        # Prefix is candidate txn_id (only valid when not truncated)
        expected = _reference_id(prefix, attempt)
        if expected != ref:
            return None
        try:
            payment = self._payment_repo.get_payment_by_id(prefix)
        except Exception:
            return None
        if payment is None:
            return None
        return prefix

    def ingest(
        self,
        *,
        raw_body: bytes,
        signature: str | None,
        event_id: str | None,
    ) -> WebhookIngestResult:
        if not event_id or not str(event_id).strip():
            raise MissingEventIdError("missing X-Razorpay-Event-Id")
        eid = str(event_id).strip()

        if not self._verifier.verify(raw_body, signature):
            raise InvalidSignatureError("invalid signature")

        parsed = parse_payment_link_payload(eid, raw_body)
        # parse may raise MalformedPayloadError — propagate, do not mark seen

        if parsed is None:
            # Unsupported / unrelated event
            if self._webhook_repo.exists(eid):
                return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
            inserted = self._webhook_repo.try_insert(eid, event_type="unsupported")
            if not inserted:
                return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
            return WebhookIngestResult(status=WebhookIngestStatus.IGNORED, event_id=eid)

        # Duplicate check before any side effects
        if self._webhook_repo.exists(eid):
            return WebhookIngestResult(
                status=WebhookIngestStatus.DUPLICATE, event_id=eid, event_type=parsed.event_type, payment_id=parsed.plink_id
            )

        # Handle statuses that must never become RECOVERED
        # paid -> RECOVERED only, failed/cancelled/expired -> FAILED, others IGNORED
        status = parsed.status
        if status in ("created", "partially_paid", "notified"):
            inserted = self._webhook_repo.try_insert(eid, event_type=parsed.event_type, payment_id=parsed.plink_id)
            if not inserted:
                return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
            return WebhookIngestResult(
                status=WebhookIngestStatus.IGNORED, event_id=eid, event_type=parsed.event_type, payment_id=parsed.plink_id
            )

        if status not in ("paid", "failed", "cancelled", "expired"):
            inserted = self._webhook_repo.try_insert(eid, event_type=parsed.event_type, payment_id=parsed.plink_id)
            if not inserted:
                return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
            return WebhookIngestResult(
                status=WebhookIngestStatus.IGNORED, event_id=eid, event_type=parsed.event_type, payment_id=parsed.plink_id
            )

        # paid / failed / cancelled / expired -> need exact correlation
        txn_id = self._correlate_txn_id(parsed)
        if txn_id is None:
            inserted = self._webhook_repo.try_insert(eid, event_type=parsed.event_type, payment_id=parsed.plink_id)
            if not inserted:
                return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
            return WebhookIngestResult(
                status=WebhookIngestStatus.IGNORED, event_id=eid, event_type=parsed.event_type, payment_id=parsed.plink_id
            )

        # Load payment entity for transition validation (hard-stop etc.)
        payment = self._payment_repo.get_payment_by_id(txn_id)
        if payment is None:
            inserted = self._webhook_repo.try_insert(eid, event_type=parsed.event_type, payment_id=parsed.plink_id)
            if not inserted:
                return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
            return WebhookIngestResult(
                status=WebhookIngestStatus.IGNORED, event_id=eid, event_type=parsed.event_type, payment_id=parsed.plink_id
            )

        # Use one SessionLocal transaction for lifecycle + attempt + audit
        # Webhook event insertion happens after successful commit for retry-friendliness
        # But we already checked exists; race is handled by try_insert after commit
        target: RecoveryState
        audit_outcome: AuditOutcome
        reason_code: ReasonCode
        from src.domain.models import Outcome as AttemptOutcome

        if status == "paid":
            target = RecoveryState.RECOVERED
            audit_outcome = AuditOutcome.RECOVERED
            reason_code = ReasonCode.RECOVERED
            attempt_outcome = AttemptOutcome.SUCCESS
        else:
            target = RecoveryState.FAILED
            audit_outcome = AuditOutcome.FAILED
            reason_code = ReasonCode.RAIL_DECLINED
            attempt_outcome = AttemptOutcome.FAILED

        # Determine action type
        raw_action = (parsed.notes_action_type or "").lower()
        if "dunning" in raw_action:
            action = ActionType.DUNNING
        elif "re-auth" in raw_action or "reauth" in raw_action:
            action = ActionType.REAUTH
        elif "refuse" in raw_action:
            action = ActionType.REFUSE
        else:
            action = ActionType.RETRY

        gateway_ref = parsed.short_url or parsed.plink_id or parsed.reference_id or txn_id
        rationale = f"Payment Link {status} for {txn_id}"

        # Atomic transaction
        with self._session_factory() as session:
            try:
                lc_row = session.get(RecoveryLifecycleModel, txn_id)
                if lc_row is None:
                    # Only EXECUTING can transition to terminal
                    session.rollback()
                    inserted = self._webhook_repo.try_insert(eid, event_type=parsed.event_type, payment_id=parsed.plink_id)
                    if not inserted:
                        return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
                    return WebhookIngestResult(
                        status=WebhookIngestStatus.IGNORED, event_id=eid, event_type=parsed.event_type, payment_id=parsed.plink_id
                    )
                current = RecoveryState(lc_row.state)
                if current != RecoveryState.EXECUTING:
                    session.rollback()
                    inserted = self._webhook_repo.try_insert(eid, event_type=parsed.event_type, payment_id=parsed.plink_id)
                    if not inserted:
                        return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
                    return WebhookIngestResult(
                        status=WebhookIngestStatus.IGNORED, event_id=eid, event_type=parsed.event_type, payment_id=parsed.plink_id
                    )

                lc = RecoveryLifecycle(txn_id=txn_id, state=current, updated_at=lc_row.updated_at, reason=lc_row.reason)
                try:
                    new_lc = transition(lc, target, payment, auto_eligible=False, reason=rationale)
                except (InvalidTransitionError, HardStopViolation):
                    session.rollback()
                    inserted = self._webhook_repo.try_insert(eid, event_type=parsed.event_type, payment_id=parsed.plink_id)
                    if not inserted:
                        return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
                    return WebhookIngestResult(
                        status=WebhookIngestStatus.IGNORED, event_id=eid, event_type=parsed.event_type, payment_id=parsed.plink_id
                    )

                # CAS update
                result = session.execute(
                    update(RecoveryLifecycleModel)
                    .where(
                        RecoveryLifecycleModel.txn_id == txn_id,
                        RecoveryLifecycleModel.state == current.value,
                    )
                    .values(
                        state=new_lc.state.value,
                        updated_at=new_lc.updated_at,
                        reason=new_lc.reason,
                        version=RecoveryLifecycleModel.version + 1,
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    inserted = self._webhook_repo.try_insert(eid, event_type=parsed.event_type, payment_id=parsed.plink_id)
                    if not inserted:
                        return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
                    return WebhookIngestResult(
                        status=WebhookIngestStatus.IGNORED, event_id=eid, event_type=parsed.event_type, payment_id=parsed.plink_id
                    )

                # Create RecoveryAttempt
                # attempt_number: use validated notes_attempt if present else next executed count +1
                if parsed.notes_attempt is not None:
                    attempt_number = int(parsed.notes_attempt)
                else:
                    # infer from suffix of reference_id
                    try:
                        attempt_number = int(parsed.reference_id.rsplit("_", 1)[-1])
                    except Exception:
                        attempt_number = payment.executed_retry_count + 1

                now = datetime.utcnow()
                from src.domain.models import Outcome as DomainOutcome

                # Map attempt_outcome (domain.models.Outcome) to DB string
                attempt_model = RecoveryAttemptModel(
                    txn_id=txn_id,
                    attempt_number=attempt_number,
                    outcome=attempt_outcome.value if hasattr(attempt_outcome, "value") else str(attempt_outcome),
                    reason=rationale,
                    action_type=action.value,
                    timestamp=now,
                )
                session.add(attempt_model)

                # Create audit event — same transaction
                masked = self._masking.mask_customer_ref(payment.customer_id)
                scrubbed_rationale = self._masking.scrub_text(rationale)
                tier = self._resolve_tier(payment, reason_code)
                audit_event = AuditEvent(
                    event_id=str(uuid.uuid4()),
                    txn_id=txn_id,
                    timestamp=now,
                    action=action,
                    decision_rationale=scrubbed_rationale,
                    outcome=audit_outcome,
                    reason_code=reason_code,
                    customer_ref_masked=masked,
                    tier=tier,
                )
                session.add(
                    OperatorAuditModel(
                        event_id=audit_event.event_id,
                        txn_id=audit_event.txn_id,
                        timestamp=audit_event.timestamp,
                        action=audit_event.action.value,
                        decision_rationale=audit_event.decision_rationale,
                        outcome=audit_event.outcome.value,
                        reason_code=audit_event.reason_code.value,
                        customer_ref_masked=audit_event.customer_ref_masked,
                        tier=audit_event.tier,
                    )
                )

                session.commit()

            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
                raise

        # Webhook replay protection — insert after successful commit
        inserted = self._webhook_repo.try_insert(eid, event_type=parsed.event_type, payment_id=parsed.plink_id)
        if not inserted:
            # Extremely rare race: lifecycle already committed but webhook duplicate
            # Do not rollback lifecycle; return DUPLICATE to signal replay
            return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)

        return WebhookIngestResult(
            status=WebhookIngestStatus.INGESTED, event_id=eid, event_type=parsed.event_type, payment_id=parsed.plink_id
        )

    def _resolve_tier(self, payment, reason_code: ReasonCode) -> str:
        # Derive tier from RecommendRecovery + EscalationPolicy.
        # No silent T1 fallback: any derivation failure must propagate so the
        # caller transaction rolls back instead of persisting a synthetic T1 audit.
        recommender = self._recommender or RecommendRecovery()
        _rec = recommender.recommend(payment, auto_eligible=False)
        policy = self._escalation or EscalationPolicy()
        return policy.assign_tier(
            reason_code=reason_code,
            amount=float(payment.amount),
            retry_count=int(payment.executed_retry_count),
        )
