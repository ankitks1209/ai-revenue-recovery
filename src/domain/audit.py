"""T6.1 — AuditEvent: immutable domain record of every money action."""
from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ReasonCode(str, enum.Enum):
    RECOVERED = "recovered"
    RETRIES_EXHAUSTED = "retries_exhausted"
    RAIL_DECLINED = "rail_declined"
    DO_NOT_RETRY = "do_not_retry"
    STOPPING_RULE_TRIP = "stopping_rule_trip"


class ActionType(str, enum.Enum):
    RETRY = "retry"
    DUNNING = "dunning"
    REAUTH = "re-auth"
    REFUSE = "refuse"  # graceful failure — a first-class audited outcome


class Outcome(str, enum.Enum):
    RECOVERED = "recovered"
    FAILED = "failed"
    SKIPPED = "skipped"
    ESCALATED = "escalated"


class AuditEvent(BaseModel):
    """Immutable (frozen) — an audit record can never be mutated after creation."""
    model_config = ConfigDict(frozen=True)

    event_id: str
    txn_id: str
    timestamp: datetime
    action: ActionType
    decision_rationale: str
    outcome: Outcome
    reason_code: ReasonCode
    customer_ref_masked: str  # already masked before construction
    tier: str                  # T1 / T2 / T3
