"""P5.5 operator decision orchestration."""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.application.recommend_recovery import RecommendRecovery
from src.database import (
    OperatorAuditModel, RecoveryLifecycleModel, SessionLocal,
)
from src.domain.audit import ActionType, AuditEvent, Outcome as AuditOutcome, ReasonCode
from src.domain.masking import MaskingPolicy
from src.domain.recovery_lifecycle import (
    HardStopViolation, InvalidTransitionError, RecoveryLifecycle, RecoveryState, derive_state, transition,
)
from src.domain.recovery_recommendation import RecommendationKind
from src.infrastructure.recovery_lifecycle_repository import RecoveryLifecycleRepository
from src.infrastructure.repository import SQLiteFailedPaymentRepository


class Decision(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"


OperatorDecision = Decision


@dataclass(frozen=True)
class DecideResult:
    txn_id: str
    decision: str
    lifecycle_state: RecoveryState
    audit_event: AuditEvent
    applied: bool
    reason: Optional[str] = None


_HARD_STOP_FORBIDDEN = {RecoveryState.AUTO_ELIGIBLE, RecoveryState.APPROVED, RecoveryState.EXECUTING, RecoveryState.RECOVERED}


class DecideRecovery:
    def __init__(self, payment_repository=None, lifecycle_repository=None, *, session_factory=SessionLocal,
                 recommender: RecommendRecovery | None = None, masking: MaskingPolicy | None = None) -> None:
        self.payment_repository = payment_repository or SQLiteFailedPaymentRepository(session_factory=session_factory)
        self.lifecycle_repository = lifecycle_repository or RecoveryLifecycleRepository(session_factory)
        self.session_factory = session_factory
        self.recommend = recommender or RecommendRecovery()
        self.masking = masking or MaskingPolicy()

    def _make_audit(self, txn_id: str, payment, now: datetime, action: ActionType, rationale: str, code: ReasonCode) -> AuditEvent:
        return AuditEvent(
            event_id=str(uuid.uuid4()),
            txn_id=txn_id,
            timestamp=now,
            action=action,
            decision_rationale=self.masking.scrub_text(rationale),
            outcome=AuditOutcome.SKIPPED,
            reason_code=code,
            customer_ref_masked=self.masking.mask_customer_ref(payment.customer_id),
            tier="T1",
        )

    def decide(self, txn_id: str, decision: str | Decision, reason: str = "", operator_reason: str | None = None) -> DecideResult:
        # Normalize decision
        if isinstance(decision, Decision):
            dec_str = decision.value
        else:
            try:
                dec_str = Decision(str(decision).lower()).value
            except ValueError:
                raise ValueError(f"Invalid decision: {decision}")

        raw_reason = operator_reason if operator_reason is not None else reason
        if raw_reason is None:
            raw_reason = ""
        if not isinstance(raw_reason, str):
            raw_reason = str(raw_reason)
        stripped = raw_reason.strip()
        if dec_str == Decision.REJECT.value and not stripped:
            raise ValueError("Rejection reason is required and cannot be blank")

        payment = self.payment_repository.get_payment_by_id(txn_id)
        if payment is None:
            raise ValueError(f"Unknown transaction: {txn_id}")

        rec = self.recommend.recommend(payment, auto_eligible=False)
        hard_stop = rec.kind == RecommendationKind.REFUSE
        now = datetime.utcnow()

        # Determine target, rationale, action, code before transaction for hard-stop check
        if dec_str == Decision.APPROVE.value:
            target = RecoveryState.APPROVED
            rationale = stripped if stripped else "Operator approved recovery"
            # action mapping for approve
            action = {
                RecommendationKind.DUNNING: ActionType.DUNNING,
                RecommendationKind.REAUTH: ActionType.REAUTH,
            }.get(rec.kind, ActionType.RETRY)
            # for hard_stop, rec.kind is REFUSE, so mapping gives RETRY but will be blocked by hard-stop check
            if hard_stop:
                action = ActionType.REFUSE
                code = ReasonCode.DO_NOT_RETRY
            else:
                code = ReasonCode.RAIL_DECLINED
        else:
            target = RecoveryState.REJECTED
            rationale = stripped  # validated not blank
            action = ActionType.REFUSE
            code = ReasonCode.DO_NOT_RETRY if hard_stop else ReasonCode.STOPPING_RULE_TRIP

        with self.session_factory() as session:
            existing = session.get(RecoveryLifecycleModel, txn_id)
            current = RecoveryState(existing.state) if existing is not None else derive_state(payment)
            # Hard-stop check BEFORE idempotency shortcut
            if hard_stop and target in _HARD_STOP_FORBIDDEN:
                raise HardStopViolation(f"Hard-stop payment {txn_id} cannot transition to {target.value}")

            # Idempotency shortcut
            if current == target:
                event = self._make_audit(txn_id, payment, now, action, rationale, code)
                return DecideResult(txn_id, dec_str, target, event, False, rationale)

            lifecycle = RecoveryLifecycle(txn_id, current, existing.updated_at if existing is not None else now,
                                          existing.reason if existing is not None else None)
            # Bootstrap and final transition through P5.1 graph
            try:
                if existing is None and current == RecoveryState.RECEIVED:
                    lifecycle = transition(lifecycle, RecoveryState.ANALYZED, payment, now=now, auto_eligible=False)
                    lifecycle = transition(lifecycle, RecoveryState.PENDING_APPROVAL, payment, now=now, auto_eligible=False)
                    lifecycle = transition(lifecycle, target, payment, reason=rationale, now=now, auto_eligible=False)
                else:
                    lifecycle = transition(lifecycle, target, payment, reason=rationale, now=now, auto_eligible=False)
            except (InvalidTransitionError, HardStopViolation):
                # Ensure no partial state leaked
                try:
                    session.rollback()
                except Exception:
                    pass
                raise

            # CAS persistence
            if existing is None:
                success = self.lifecycle_repository.compare_and_set(txn_id, None, lifecycle, session)
                expected = None
            else:
                success = self.lifecycle_repository.compare_and_set(txn_id, current, lifecycle, session)
                expected = current

            if not success:
                try:
                    session.rollback()
                except Exception:
                    pass
                # Re-read authoritative state
                # Use same session after rollback; need fresh read
                authoritative = None
                try:
                    authoritative = session.get(RecoveryLifecycleModel, txn_id)
                    if authoritative is None:
                        # try via repository with new session to be safe
                        fetched = self.lifecycle_repository.get(txn_id)
                        auth_state = fetched.state if fetched else None
                    else:
                        auth_state = RecoveryState(authoritative.state)
                except Exception:
                    fetched = self.lifecycle_repository.get(txn_id)
                    auth_state = fetched.state if fetched else None

                if auth_state == target:
                    event = self._make_audit(txn_id, payment, now, action, rationale, code)
                    return DecideResult(txn_id, dec_str, target, event, False, rationale)
                else:
                    raise ValueError(f"Conflict: CAS failed expected {expected} found {auth_state} for target {target}")

            # CAS succeeded, now audit in SAME transaction
            event = self._make_audit(txn_id, payment, now, action, rationale, code)
            try:
                session.add(OperatorAuditModel(
                    event_id=event.event_id,
                    txn_id=event.txn_id,
                    timestamp=event.timestamp,
                    action=event.action.value,
                    decision_rationale=event.decision_rationale,
                    outcome=event.outcome.value,
                    reason_code=event.reason_code.value,
                    customer_ref_masked=event.customer_ref_masked,
                    tier=event.tier,
                ))
                session.commit()
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
                raise

            return DecideResult(txn_id, dec_str, lifecycle.state, event, True, rationale)

    run = decide
