from typing import List, Dict, Any, Optional
from datetime import timedelta
import uuid
from src.domain.entities import RecoveryAttempt, Escalation
from src.domain.models import Outcome, RailResponse
from src.domain.retry_policy import RetryPolicy
from src.domain.escalation_service import EscalationService
from src.infrastructure.ports import FailedPaymentRepositoryPort, PaymentRailPort, ClockPort
from src.classifier import FailureClassifier
from src.policy_engine import PolicyEngine
from src.domain.audit import AuditEvent, ActionType, Outcome as AuditOutcome, ReasonCode
from src.domain.masking import MaskingPolicy
from src.domain.escalation import GracefulFailureHandler, EscalationPolicy
from src.domain.action_mapper import map_action
from src.infrastructure.audit_repository import AuditLogRepository
from src.infrastructure.structured_logger import StructuredLogger

class ExecuteRecoveryBatch:
    def __init__(
        self,
        repository: FailedPaymentRepositoryPort,
        payment_rail: PaymentRailPort,
        clock: ClockPort,
        policy_engine: PolicyEngine = None,
        retry_policy: RetryPolicy = None,
        escalation_service: EscalationService = None,
        audit_repository: AuditLogRepository = None,
        structured_logger: StructuredLogger = None,
        masking_policy: MaskingPolicy = None,
        escalation_policy: EscalationPolicy = None,
        graceful_failure_handler: GracefulFailureHandler = None,
        classifier: FailureClassifier = None
    ):
        self.classifier = classifier or FailureClassifier()
        self.repository = repository
        self.payment_rail = payment_rail
        self.clock = clock
        self.policy_engine = policy_engine or PolicyEngine()
        self.retry_policy = retry_policy or RetryPolicy()
        self.escalation_service = escalation_service or EscalationService()

        # NEW: Audit infrastructure with backward-compatible in-memory defaults
        # In-memory SQLite persists within this executor instance's lifetime
        self.audit_repo = audit_repository or AuditLogRepository(db_url="sqlite:///:memory:")
        self.logger = structured_logger or StructuredLogger()
        self.masking = masking_policy or MaskingPolicy()
        self.escalation_policy_service = escalation_policy or EscalationPolicy()
        self.graceful_failure = graceful_failure_handler or GracefulFailureHandler()

    def next_attempt_at(self, payment):
        category = self.classifier.classify_code(payment.failure_code)
        count = payment.total_retry_count
        delay = self.retry_policy.get_effective_delay_seconds(category, count) if count else 0
        return (payment.last_executed_attempt_at or payment.timestamp) + timedelta(seconds=delay)

    def execute(self) -> Dict[str, Any]:
        return self.execute_payments(self.repository.get_all_payments())

    def execute_payments(self, payments) -> Dict[str, Any]:
        now = self.clock.now()

        stats = {"executed_count": 0, "success_count": 0, "failed_count": 0, "skipped_count": 0, "escalated_count": 0}

        for payment in payments:
            if payment.is_terminal:
                continue

            category = self.classifier.classify_code(payment.failure_code)
            policy_info = self.policy_engine.get_action_for_category(category)
            chosen_action = policy_info["chosen_action"]
            policy_rule = self.retry_policy.get_rule(category)
            executed_retries = payment.total_retry_count

            if policy_rule.is_hard_stop:
                self._handle_hard_stop(payment, category, chosen_action, now, stats)
                continue

            failures = sum(a.outcome == Outcome.FAILED for a in payment.attempts)
            reason = self.escalation_policy_service.handoff_reason(payment.amount, failures)
            if self.retry_policy.window_expired(category, payment.first_recovery_attempt_at, now):
                reason = f"Recovery window of {policy_rule.window_days} days expired"
            if executed_retries >= policy_rule.max_retries:
                reason = f"Retry cap ({policy_rule.max_retries}) exhausted"
            if reason:
                self._handle_stop(payment, reason, chosen_action, now, stats)
                continue

            due = self.next_attempt_at(payment)
            if due > now:
                self._handle_skipped(payment, chosen_action, f"Next eligible at {due.isoformat()}", now, stats)
                continue

            self._execute_rail_attempt(payment, category, chosen_action, executed_retries, policy_rule, now, stats)

        return {"total_processed": len(payments), **stats}

    def _emit_audit(
        self,
        payment,
        action: ActionType,
        outcome,  # Phase 3 AuditOutcome (from src.domain.audit)
        reason_code,  # Phase 3 ReasonCode (from src.domain.audit)
        decision_rationale: str
    ) -> AuditEvent:
        """
        Single reusable helper for all audit emission paths.

        Handles:
        1. Masking customer_id before audit construction
        2. Generating fresh UUID4 event_id for every event
        3. Tier assignment via EscalationPolicy.assign_tier()
        4. Scrubbing decision_rationale before storage
        5. Appending to AuditLogRepository (already-masked event only)
        6. Emitting to StructuredLogger (already-masked event only)

        Observational only — no side effects on money-action logic.
        """
        # Step 1: Raw customer_id from payment
        raw_customer_id = payment.customer_id

        # Step 2: Mask it
        customer_ref_masked = self.masking.mask_customer_ref(raw_customer_id)

        # Step 3: Fresh UUID4 event_id
        event_id = str(uuid.uuid4())

        # Step 4: Scrub rationale
        rationale_scrubbed = self.masking.scrub_text(decision_rationale)

        # Step 5: Assign tier via policy (NEVER hard-code)
        tier = self.escalation_policy_service.assign_tier(
            reason_code=reason_code,
            amount=payment.amount,
            retry_count=payment.executed_retry_count
        )

        # Step 6: Construct already-masked AuditEvent
        event = AuditEvent(
            event_id=event_id,
            txn_id=payment.txn_id,
            timestamp=self.clock.now(),
            action=action,
            decision_rationale=rationale_scrubbed,
            outcome=outcome,
            reason_code=reason_code,
            customer_ref_masked=customer_ref_masked,
            tier=tier
        )

        # Step 7: Append already-masked event to repository
        try:
            self.audit_repo.append(event)
        except Exception:
            # Observational only — audit failure does NOT alter money-action
            pass

        # Step 8: Emit already-masked event to logger
        try:
            self.logger.emit(event)
        except Exception:
            # Observational only — logger failure does NOT alter money-action
            pass

        return event


    def _handle_hard_stop(self, payment, category, chosen_action, now, stats):
        reason = f"Hard stop policy guard for category: {category}"

        is_do_not_retry = category == "Hard Fraud / Do-Not-Retry"
        refusal = self.graceful_failure.evaluate(
            is_do_not_retry=is_do_not_retry,
            stopping_rule_tripped=False
        )

        if refusal:
            # Emit audit event for hard-stop refusal (hard fraud path)
            self._emit_audit(
                payment=payment,
                action=ActionType.REFUSE,
                outcome=AuditOutcome.ESCALATED,
                reason_code=refusal.reason_code,
                decision_rationale=refusal.rationale
            )
        else:
            # Unknown/unrecognized category forced to hard-stop by policy:
            # not hard-fraud, but still a policy refusal — must be audited.
            self._emit_audit(
                payment=payment,
                action=ActionType.REFUSE,
                outcome=AuditOutcome.ESCALATED,
                reason_code=ReasonCode.STOPPING_RULE_TRIP,
                decision_rationale=reason
            )

        # Existing Phase 2 logic (unchanged)
        esc = self.escalation_service.create_escalation(payment.txn_id, reason, now)
        self.repository.save_escalation(esc)
        att = RecoveryAttempt(
            txn_id=payment.txn_id, attempt_number=payment.executed_retry_count + 1,
            outcome=Outcome.ESCALATED, reason=reason, action_type=chosen_action, timestamp=now
        )
        self.repository.save_attempt(att)
        stats["escalated_count"] += 1

    def _handle_stop(self, payment, reason, chosen_action, now, stats):

        # NEW: Evaluate graceful failure for max-retries/stopping-rule case
        is_do_not_retry = False  # Not a hard-fraud case
        stopping_rule_tripped = True  # Retry cap is a stopping-rule trip
        refusal = self.graceful_failure.evaluate(
            is_do_not_retry=is_do_not_retry,
            stopping_rule_tripped=stopping_rule_tripped
        )

        if refusal:
            # NEW: Emit audit event for retry-cap refusal
            self._emit_audit(
                payment=payment,
                action=ActionType.REFUSE,
                outcome=AuditOutcome.ESCALATED,
                reason_code=refusal.reason_code,
                decision_rationale=reason
            )

        # Existing Phase 2 logic (unchanged)
        esc = self.escalation_service.create_escalation(payment.txn_id, reason, now)
        self.repository.save_escalation(esc)
        att = RecoveryAttempt(
            txn_id=payment.txn_id, attempt_number=payment.executed_retry_count + 1,
            outcome=Outcome.ESCALATED, reason=reason, action_type=chosen_action, timestamp=now
        )
        self.repository.save_attempt(att)
        stats["escalated_count"] += 1

    def _handle_skipped(self, payment, chosen_action, reason, now, stats):
        att = RecoveryAttempt(
            txn_id=payment.txn_id, attempt_number=payment.executed_retry_count + 1,
            outcome=Outcome.SKIPPED, reason=reason, action_type=chosen_action, timestamp=now
        )
        self.repository.save_attempt(att)
        stats["skipped_count"] += 1

        # NEW: Emit audit event for temporary backoff skip
        mapped_action = map_action(chosen_action)
        self._emit_audit(
            payment=payment,
            action=mapped_action,
            outcome=AuditOutcome.SKIPPED,
            reason_code=ReasonCode.BACKOFF,
            decision_rationale=reason
        )

    def _execute_rail_attempt(self, payment, category, chosen_action, executed_retries, policy_rule, now, stats):
        attempt_number = executed_retries + 1
        try:
            rail_response = self.payment_rail.execute_attempt(
                txn_id=payment.txn_id, amount=payment.amount, action_type=chosen_action, attempt_number=attempt_number
            )
        except Exception as e:
            rail_response = RailResponse(
                success=False,
                error_message=f"Unhandled Rail Exception: {str(e)}",
                gateway_reference="EXCEPTION"
            )
        stats["executed_count"] += 1

        if rail_response.success:
            att = RecoveryAttempt(
                txn_id=payment.txn_id, attempt_number=attempt_number,
                outcome=Outcome.SUCCESS, action_type=chosen_action, timestamp=now
            )
            self.repository.save_attempt(att)
            stats["success_count"] += 1

            # NEW: Emit audit event for successful recovery
            mapped_action = map_action(chosen_action)
            self._emit_audit(
                payment=payment,
                action=mapped_action,
                outcome=AuditOutcome.RECOVERED,
                reason_code=ReasonCode.RECOVERED,
                decision_rationale="Recovery attempt succeeded"
            )
        else:
            att = RecoveryAttempt(
                txn_id=payment.txn_id, attempt_number=attempt_number,
                outcome=Outcome.FAILED, reason=rail_response.error_message, action_type=chosen_action, timestamp=now
            )
            self.repository.save_attempt(att)
            stats["failed_count"] += 1

            # NEW: Emit audit event for failed recovery attempt
            mapped_action = map_action(chosen_action)
            self._emit_audit(
                payment=payment,
                action=mapped_action,
                outcome=AuditOutcome.FAILED,
                reason_code=ReasonCode.RAIL_DECLINED,
                decision_rationale=f"Rail declined: {rail_response.error_message}"
            )

            failures = 1 + sum(a.outcome == Outcome.FAILED for a in payment.attempts)
            reason = self.escalation_policy_service.handoff_reason(payment.amount, failures)
            if attempt_number >= policy_rule.max_retries:
                reason = f"Retry cap ({policy_rule.max_retries}) exhausted"
            if reason:
                esc = self.escalation_service.create_escalation(payment.txn_id, reason, now)
                self.repository.save_escalation(esc)
                self._emit_audit(payment, ActionType.REFUSE, AuditOutcome.ESCALATED,
                                 ReasonCode.STOPPING_RULE_TRIP, reason)
                stats["escalated_count"] += 1
