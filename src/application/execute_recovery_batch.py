from typing import List, Dict, Any
from src.domain.entities import RecoveryAttempt, Escalation
from src.domain.models import Outcome
from src.domain.retry_policy import RetryPolicy
from src.domain.escalation_service import EscalationService
from src.infrastructure.ports import FailedPaymentRepositoryPort, PaymentRailPort, ClockPort
from src.policy_engine import PolicyEngine

class ExecuteRecoveryBatch:
    def __init__(
        self,
        repository: FailedPaymentRepositoryPort,
        payment_rail: PaymentRailPort,
        clock: ClockPort,
        policy_engine: PolicyEngine = None,
        retry_policy: RetryPolicy = None,
        escalation_service: EscalationService = None
    ):
        self.repository = repository
        self.payment_rail = payment_rail
        self.clock = clock
        self.policy_engine = policy_engine or PolicyEngine()
        self.retry_policy = retry_policy or RetryPolicy()
        self.escalation_service = escalation_service or EscalationService()

    def execute(self) -> Dict[str, Any]:
        payments = self.repository.get_all_payments()
        now = self.clock.now()

        stats = {"executed_count": 0, "success_count": 0, "failed_count": 0, "skipped_count": 0, "escalated_count": 0}

        for payment in payments:
            if payment.is_terminal:
                continue

            category = payment.root_cause_label
            policy_info = self.policy_engine.get_action_for_category(category)
            chosen_action = policy_info["chosen_action"]
            policy_rule = self.retry_policy.get_rule(category)
            executed_retries = payment.executed_retry_count

            if policy_rule.is_hard_stop:
                self._handle_hard_stop(payment, category, chosen_action, now, stats)
                continue

            if executed_retries >= policy_rule.max_retries:
                self._handle_max_retries(payment, policy_rule.max_retries, chosen_action, now, stats)
                continue

            if executed_retries > 0:
                is_eligible, reason = self.retry_policy.is_eligible_for_attempt(
                    category, executed_retries, payment.last_executed_attempt_at, now
                )
                if not is_eligible:
                    self._handle_skipped(payment, chosen_action, reason, now, stats)
                    continue

            self._execute_rail_attempt(payment, category, chosen_action, executed_retries, policy_rule, now, stats)

        return {"total_processed": len(payments), **stats}

    def _handle_hard_stop(self, payment, category, chosen_action, now, stats):
        reason = f"Hard stop policy guard for category: {category}"
        esc = self.escalation_service.create_escalation(payment.txn_id, reason, now)
        self.repository.save_escalation(esc)
        att = RecoveryAttempt(
            txn_id=payment.txn_id, attempt_number=payment.executed_retry_count + 1,
            outcome=Outcome.ESCALATED, reason=reason, action_type=chosen_action, timestamp=now
        )
        self.repository.save_attempt(att)
        stats["escalated_count"] += 1

    def _handle_max_retries(self, payment, max_retries, chosen_action, now, stats):
        reason = f"Retry cap ({max_retries}) exhausted"
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

    def _execute_rail_attempt(self, payment, category, chosen_action, executed_retries, policy_rule, now, stats):
        attempt_number = executed_retries + 1
        rail_response = self.payment_rail.execute_attempt(
            txn_id=payment.txn_id, amount=payment.amount, action_type=chosen_action, attempt_number=attempt_number
        )
        stats["executed_count"] += 1

        if rail_response.success:
            att = RecoveryAttempt(
                txn_id=payment.txn_id, attempt_number=attempt_number,
                outcome=Outcome.SUCCESS, action_type=chosen_action, timestamp=now
            )
            self.repository.save_attempt(att)
            stats["success_count"] += 1
        else:
            att = RecoveryAttempt(
                txn_id=payment.txn_id, attempt_number=attempt_number,
                outcome=Outcome.FAILED, reason=rail_response.error_message, action_type=chosen_action, timestamp=now
            )
            self.repository.save_attempt(att)
            stats["failed_count"] += 1

            if category == "Mandate Lapse" or attempt_number >= policy_rule.max_retries:
                reason = f"Post-failure escalation trigger for {category} (attempt {attempt_number})"
                esc = self.escalation_service.create_escalation(payment.txn_id, reason, now)
                self.repository.save_escalation(esc)
                stats["escalated_count"] += 1
