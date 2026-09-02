"""T7.1–T7.3, T8.1–T8.2 — Escalation and Graceful Failure Domain Logic."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.domain.audit import ActionType, Outcome, ReasonCode

HIGH_VALUE_THRESHOLD = 10000.0  # INR


@dataclass(frozen=True)
class RefusalDecision:
    """A refusal is a first-class, audited outcome — never a silent skip."""

    reason_code: ReasonCode
    rationale: str
    action: ActionType = ActionType.REFUSE
    outcome: Outcome = Outcome.ESCALATED


class GracefulFailureHandler:
    """Intercepts do-not-retry (hard fraud) and stopping-rule trips."""

    def evaluate(
        self,
        *,
        is_do_not_retry: bool,
        stopping_rule_tripped: bool,
    ) -> Optional[RefusalDecision]:
        if is_do_not_retry:
            return RefusalDecision(
                action=ActionType.REFUSE,
                outcome=Outcome.ESCALATED,
                reason_code=ReasonCode.DO_NOT_RETRY,
                rationale="Hard-fraud / do-not-retry code: agent refuses automated action.",
            )

        if stopping_rule_tripped:
            return RefusalDecision(
                action=ActionType.REFUSE,
                outcome=Outcome.ESCALATED,
                reason_code=ReasonCode.STOPPING_RULE_TRIP,
                rationale="Stopping rule tripped (retries/interval exhausted): refuse + escalate.",
            )

        return None  # no refusal — proceed with bounded action


class EscalationPolicy:
    """Tier 1 automated retry → Tier 2 dunning → Tier 3 human handoff."""

    def assign_tier(
        self,
        *,
        reason_code: ReasonCode,
        amount: float,
        retry_count: int,
    ) -> str:
        # Tier 3: hard-fraud, stopping-rule trips, high-value, or repeated failures.
        if reason_code in (
            ReasonCode.DO_NOT_RETRY,
            ReasonCode.STOPPING_RULE_TRIP,
        ):
            return "T3"

        if amount >= HIGH_VALUE_THRESHOLD or retry_count >= 2:
            return "T3"

        # Tier 2: dunning / re-auth situations.
        if reason_code in (
            ReasonCode.RETRIES_EXHAUSTED,
            ReasonCode.RAIL_DECLINED,
        ):
            return "T2"

        # Tier 1: automated bounded retry.
        return "T1"
