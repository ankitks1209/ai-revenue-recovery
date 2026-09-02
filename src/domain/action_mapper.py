"""Action mapper: PolicyEngine chosen_action strings -> Phase 3 ActionType enum."""
from src.domain.audit import ActionType


def map_action(chosen_action: str) -> ActionType:
    """Map PolicyEngine string to Phase 3 ActionType. Does not alter PolicyEngine output."""
    lower = chosen_action.lower()
    if "dunning" in lower:
        return ActionType.DUNNING
    if "re-authorization" in lower or "reauthorization" in lower or "re-auth" in lower:
        return ActionType.REAUTH
    return ActionType.RETRY
