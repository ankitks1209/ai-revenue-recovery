"""P5.2 — Domain recommendation tests. Pure, no DB, no network."""

import pytest
from src.domain.recovery_recommendation import (
    RecommendationKind,
    RecoveryRecommendation,
    map_policy_to_kind,
)
from src.domain.recovery_lifecycle import RecoveryState


def test_recommendation_kind_is_string_enum():
    assert RecommendationKind.RETRY.value == "RETRY"
    assert RecommendationKind.DUNNING.value == "DUNNING"
    assert RecommendationKind.REAUTH.value == "REAUTH"
    assert RecommendationKind.REFUSE.value == "REFUSE"
    assert RecommendationKind.ESCALATE.value == "ESCALATE"
    assert isinstance(RecommendationKind.RETRY, str)


def test_map_policy_to_kind_dunning():
    kind, hint = map_policy_to_kind("Trigger dunning message + request card re-authorization")
    assert kind == RecommendationKind.DUNNING
    assert hint == "payment_link"


def test_map_policy_to_kind_reauth():
    kind, hint = map_policy_to_kind("Request mandate re-authorization from customer")
    assert kind == RecommendationKind.REAUTH
    assert hint == "reauth"


def test_map_policy_to_kind_reauth_alt_variants():
    k1, h1 = map_policy_to_kind("re-authorization required")
    assert k1 == RecommendationKind.REAUTH
    k2, h2 = map_policy_to_kind("reauthorization required")
    assert k2 == RecommendationKind.REAUTH
    k3, h3 = map_policy_to_kind("re-auth needed")
    assert k3 == RecommendationKind.REAUTH


def test_map_policy_to_kind_retry_default():
    kind, hint = map_policy_to_kind("Smart retry scheduled on payday/salary-credit window")
    assert kind == RecommendationKind.RETRY
    assert hint is None
    kind2, hint2 = map_policy_to_kind("Immediate smart retry with exponential backoff")
    assert kind2 == RecommendationKind.RETRY
    assert hint2 is None


def test_map_policy_to_kind_escalation_string_maps_to_retry():
    # Pure mapper does not know hard-stop; hard-stop is handled by RecommendRecovery.
    # PolicyEngine returns "Escalate to human review" for unknown — mapper returns RETRY.
    kind, hint = map_policy_to_kind("Escalate to human review")
    assert kind == RecommendationKind.RETRY
    assert hint is None


def test_recovery_recommendation_is_frozen():
    rec = RecoveryRecommendation(
        txn_id="TXN1",
        kind=RecommendationKind.RETRY,
        suggested_next_state=RecoveryState.PENDING_APPROVAL,
        chosen_action="retry",
        bounds="Max 3",
        rationale="retry",
    )
    with pytest.raises(Exception):
        rec.kind = RecommendationKind.DUNNING  # type: ignore


def test_recovery_recommendation_deterministic():
    r1 = RecoveryRecommendation(
        txn_id="TXN1",
        kind=RecommendationKind.RETRY,
        suggested_next_state=RecoveryState.PENDING_APPROVAL,
        chosen_action="a",
        bounds="b",
        rationale="a",
        provider_hint=None,
    )
    r2 = RecoveryRecommendation(
        txn_id="TXN1",
        kind=RecommendationKind.RETRY,
        suggested_next_state=RecoveryState.PENDING_APPROVAL,
        chosen_action="a",
        bounds="b",
        rationale="a",
        provider_hint=None,
    )
    assert r1 == r2


def test_domain_module_contains_no_forbidden_imports_or_thresholds():
    import pathlib
    src = pathlib.Path("src/domain/recovery_recommendation.py").read_text()
    assert "ALLOWED_TRANSITIONS" not in src
    assert "_HARD_STOP_FORBIDDEN" not in src
    assert "HIGH_VALUE" not in src
    assert "retry_count" not in src
    assert " amount" not in src.lower() or "amount" not in src  # no amount logic
    assert "pandas" not in src
    assert "streamlit" not in src.lower()
    assert "razorpay" not in src.lower()
    assert "sqlalchemy" not in src.lower()
    assert "import copy" not in src
