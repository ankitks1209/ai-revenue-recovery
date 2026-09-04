"""P5.2 — Recovery recommendation domain. Pure, deterministic, no I/O."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from src.domain.recovery_lifecycle import RecoveryState


class RecommendationKind(str, enum.Enum):
    RETRY = "RETRY"
    DUNNING = "DUNNING"
    REAUTH = "REAUTH"
    REFUSE = "REFUSE"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class RecoveryRecommendation:
    txn_id: str
    kind: RecommendationKind
    suggested_next_state: RecoveryState
    chosen_action: str
    bounds: str
    rationale: str
    provider_hint: Optional[str] = None


def map_policy_to_kind(chosen_action: str) -> tuple[RecommendationKind, Optional[str]]:
    """Pure mapping from policy chosen_action string to kind and provider hint.

    No threshold logic; inspects only the chosen_action string.
    """
    lower = chosen_action.lower()
    if "dunning" in lower:
        return RecommendationKind.DUNNING, "payment_link"
    if "re-authorization" in lower or "reauthorization" in lower or "re-auth" in lower:
        return RecommendationKind.REAUTH, "reauth"
    return RecommendationKind.RETRY, None
