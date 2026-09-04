"""P5.2 — Application orchestration for recovery recommendation."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.classifier import FailureClassifier
from src.domain.entities import FailedPaymentEntity
from src.domain.escalation import GracefulFailureHandler
from src.domain.recovery_lifecycle import (
    RecoveryLifecycle,
    RecoveryState,
    transition,
)
from src.domain.recovery_recommendation import (
    RecommendationKind,
    RecoveryRecommendation,
    map_policy_to_kind,
)
from src.domain.retry_policy import RetryPolicy
from src.policy_engine import PolicyEngine


class RecommendRecovery:
    """Composes existing classifier/policy/retry/graceful to produce an advisory recommendation.

    Does NOT duplicate transition graph or hard-stop rules — delegation to
    recovery_lifecycle.transition() is the single source of truth.
    Never infers auto_eligible — caller supplies it explicitly.
    """

    def __init__(
        self,
        *,
        classifier: FailureClassifier | None = None,
        policy_engine: PolicyEngine | None = None,
        retry_policy: RetryPolicy | None = None,
        graceful_handler: GracefulFailureHandler | None = None,
    ) -> None:
        self._classifier = classifier
        self._policy_engine = policy_engine
        self._retry_policy = retry_policy or RetryPolicy()
        self._graceful = graceful_handler or GracefulFailureHandler()

    @property
    def classifier(self) -> FailureClassifier:
        if self._classifier is None:
            self._classifier = FailureClassifier()
        return self._classifier

    @property
    def policy_engine(self) -> PolicyEngine:
        if self._policy_engine is None:
            self._policy_engine = PolicyEngine()
        return self._policy_engine

    def _is_hard_stop(self, payment: FailedPaymentEntity, category: str) -> bool:
        if self._retry_policy.is_hard_stop(category):
            return True
        refusal = self._graceful.evaluate(
            is_do_not_retry=not payment.recoverable_flag,
            stopping_rule_tripped=False,
        )
        return refusal is not None

    def recommend(
        self,
        payment: FailedPaymentEntity,
        *,
        auto_eligible: bool = False,
    ) -> RecoveryRecommendation:
        category = self.classifier.classify_code(payment.failure_code)
        policy = self.policy_engine.get_action_for_category(category)
        chosen_action = policy["chosen_action"]
        bounds = policy["bounds"]

        is_hard = self._is_hard_stop(payment, category)

        if is_hard:
            return RecoveryRecommendation(
                txn_id=payment.txn_id,
                kind=RecommendationKind.REFUSE,
                suggested_next_state=RecoveryState.ESCALATED,
                chosen_action=chosen_action,
                bounds=bounds,
                rationale=f"Hard-stop / do-not-retry: {chosen_action}",
                provider_hint=None,
            )

        kind, hint = map_policy_to_kind(chosen_action)
        target = RecoveryState.AUTO_ELIGIBLE if auto_eligible else RecoveryState.PENDING_APPROVAL
        return RecoveryRecommendation(
            txn_id=payment.txn_id,
            kind=kind,
            suggested_next_state=target,
            chosen_action=chosen_action,
            bounds=bounds,
            rationale=chosen_action,
            provider_hint=hint,
        )

    def validated_transition(
        self,
        lifecycle: RecoveryLifecycle,
        payment: FailedPaymentEntity,
        *,
        auto_eligible: bool = False,
        now: Optional[datetime] = None,
        reason: Optional[str] = None,
    ) -> tuple[RecoveryRecommendation, RecoveryLifecycle]:
        rec = self.recommend(payment, auto_eligible=auto_eligible)
        new_lc = transition(
            lifecycle,
            rec.suggested_next_state,
            payment,
            retry_policy=self._retry_policy,
            graceful_handler=self._graceful,
            reason=reason,
            now=now,
            auto_eligible=auto_eligible,
        )
        return rec, new_lc
