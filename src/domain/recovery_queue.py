"""P5.4 — Recovery queue read-model. Pure domain, no I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.domain.entities import FailedPaymentEntity
from src.domain.models import Outcome
from src.domain.recovery_lifecycle import RecoveryState
from src.domain.recovery_recommendation import RecommendationKind


@dataclass(frozen=True)
class RecoveryQueueRow:
    txn_id: str
    amount: float
    currency: str
    root_cause_label: str
    failure_code: str
    lifecycle_state: RecoveryState
    recommendation_kind: RecommendationKind
    provider_hint: Optional[str]
    chosen_action: str
    bounds: str
    rationale: str
    status: str
    tier: Optional[str]
    reason_code: Optional[str]
    customer_ref_masked: Optional[str]
    last_attempt_at: Optional[datetime]
    updated_at: datetime


@dataclass(frozen=True)
class RecoveryQueue:
    rows: tuple[RecoveryQueueRow, ...]
    total: int
    counts_by_state: tuple[tuple[str, int], ...]
    counts_by_kind: tuple[tuple[str, int], ...]


def derive_queue_status(payment: FailedPaymentEntity) -> str:
    """Minimal provider-neutral status for queue display."""
    if any(a.outcome == Outcome.SUCCESS for a in payment.attempts):
        return "RECOVERED"
    if payment.escalations:
        return "ESCALATED"
    if any(a.outcome == Outcome.FAILED for a in payment.attempts):
        return "FAILED"
    if any(a.outcome == Outcome.SKIPPED for a in payment.attempts):
        return "SKIPPED"
    return "UNPROCESSED"


def derive_last_attempt_at(payment: FailedPaymentEntity) -> Optional[datetime]:
    executed = [a for a in payment.attempts if a.outcome in (Outcome.SUCCESS, Outcome.FAILED)]
    if not executed:
        return None
    return max(a.timestamp for a in executed)
