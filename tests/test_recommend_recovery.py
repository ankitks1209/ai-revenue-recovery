"""P5.2 — Application recommendation tests. Pure, no DB."""

import copy
from datetime import datetime

import pytest

from src.domain.entities import FailedPaymentEntity, RecoveryAttempt, Escalation
from src.domain.models import Outcome
from src.domain.recovery_lifecycle import (
    HardStopViolation,
    InvalidTransitionError,
    RecoveryLifecycle,
    RecoveryState,
)
from src.domain.recovery_recommendation import RecommendationKind
from src.application.recommend_recovery import RecommendRecovery


def _payment(
    failure_code: str = "insufficient_funds",
    label: str = "Insufficient Funds",
    recoverable: bool = True,
    txn_id: str = "TXN1",
    amount: float = 1000.0,
    retry_count: int = 0,
) -> FailedPaymentEntity:
    return FailedPaymentEntity(
        txn_id=txn_id,
        customer_id="CUST1",
        amount=amount,
        currency="INR",
        failure_code=failure_code,
        root_cause_label=label,
        recoverable_flag=recoverable,
        retry_count=retry_count,
        timestamp=datetime(2026, 1, 1, 9, 0, 0),
        payment_method="card",
    )


def _lc(state: RecoveryState) -> RecoveryLifecycle:
    return RecoveryLifecycle(txn_id="TXN1", state=state, updated_at=datetime(2026, 1, 1, 10, 0, 0))


NOW = datetime(2026, 1, 1, 11, 0, 0)
SVC = RecommendRecovery()

# ── mapping ───────────────────────────────────────────────────────

def test_insufficient_funds_maps_to_retry_pending_approval():
    p = _payment(failure_code="insufficient_funds")
    rec = SVC.recommend(p)
    assert rec.kind == RecommendationKind.RETRY
    assert rec.suggested_next_state == RecoveryState.PENDING_APPROVAL
    assert rec.provider_hint is None


def test_transient_maps_to_retry_pending_approval():
    p = _payment(failure_code="gateway_timeout")
    rec = SVC.recommend(p)
    assert rec.kind == RecommendationKind.RETRY
    assert rec.suggested_next_state == RecoveryState.PENDING_APPROVAL


def test_mandate_lapse_maps_to_reauth():
    p = _payment(failure_code="mandate_revoked")
    rec = SVC.recommend(p)
    assert rec.kind == RecommendationKind.REAUTH
    assert rec.provider_hint == "reauth"
    assert rec.suggested_next_state == RecoveryState.PENDING_APPROVAL


def test_expired_card_maps_to_dunning_with_payment_link():
    p = _payment(failure_code="expired_card")
    rec = SVC.recommend(p)
    assert rec.kind == RecommendationKind.DUNNING
    assert rec.provider_hint == "payment_link"
    assert rec.suggested_next_state == RecoveryState.PENDING_APPROVAL


def test_hard_fraud_maps_to_refuse_escalated():
    p = _payment(failure_code="fraud_suspected", recoverable=False, label="Hard Fraud / Do-Not-Retry")
    # taxonomy maps fraud_suspected -> Hard Fraud; recoverable false also triggers graceful
    rec = SVC.recommend(p)
    assert rec.kind == RecommendationKind.REFUSE
    assert rec.suggested_next_state == RecoveryState.ESCALATED


def test_unknown_ambiguous_maps_to_refuse_escalated():
    p = _payment(failure_code="some_unknown_code_xyz")
    rec = SVC.recommend(p)
    assert rec.kind == RecommendationKind.REFUSE
    assert rec.suggested_next_state == RecoveryState.ESCALATED


def test_recoverable_false_transient_maps_to_refuse_escalated():
    p = _payment(failure_code="gateway_timeout", recoverable=False)
    rec = SVC.recommend(p)
    assert rec.kind == RecommendationKind.REFUSE
    assert rec.suggested_next_state == RecoveryState.ESCALATED


# ── auto_eligible gate (no inference) ─────────────────────────────

def test_auto_eligible_false_stays_pending_approval():
    p = _payment(failure_code="gateway_timeout")
    rec = SVC.recommend(p, auto_eligible=False)
    assert rec.suggested_next_state == RecoveryState.PENDING_APPROVAL


def test_auto_eligible_true_becomes_auto_eligible():
    p = _payment(failure_code="gateway_timeout")
    rec = SVC.recommend(p, auto_eligible=True)
    assert rec.suggested_next_state == RecoveryState.AUTO_ELIGIBLE


def test_hard_fraud_auto_eligible_true_still_escalated():
    p = _payment(failure_code="fraud_suspected", recoverable=False, label="Hard Fraud / Do-Not-Retry")
    rec = SVC.recommend(p, auto_eligible=True)
    assert rec.suggested_next_state == RecoveryState.ESCALATED


def test_unknown_auto_eligible_true_still_escalated():
    p = _payment(failure_code="unknown_xyz")
    rec = SVC.recommend(p, auto_eligible=True)
    assert rec.suggested_next_state == RecoveryState.ESCALATED


def test_graceful_do_not_retry_auto_eligible_true_still_escalated():
    p = _payment(failure_code="gateway_timeout", recoverable=False)
    rec = SVC.recommend(p, auto_eligible=True)
    assert rec.suggested_next_state == RecoveryState.ESCALATED


def test_auto_eligible_not_inferred_from_amount_or_retry():
    p_high_retry = _payment(failure_code="gateway_timeout", amount=50000.0, retry_count=5)
    rec_false = SVC.recommend(p_high_retry, auto_eligible=False)
    assert rec_false.suggested_next_state == RecoveryState.PENDING_APPROVAL
    rec_true = SVC.recommend(p_high_retry, auto_eligible=True)
    assert rec_true.suggested_next_state == RecoveryState.AUTO_ELIGIBLE
    # Same for low amount: flag controls, not amount
    p_low = _payment(failure_code="gateway_timeout", amount=10.0, retry_count=0)
    assert SVC.recommend(p_low, auto_eligible=False).suggested_next_state == RecoveryState.PENDING_APPROVAL
    assert SVC.recommend(p_low, auto_eligible=True).suggested_next_state == RecoveryState.AUTO_ELIGIBLE


# ── validated_transition delegates to P5.1 ────────────────────────

def test_validated_transition_pending_approval_succeeds():
    p = _payment(failure_code="gateway_timeout")
    lc = _lc(RecoveryState.ANALYZED)
    rec, new_lc = SVC.validated_transition(lc, p, auto_eligible=False, now=NOW)
    assert rec.suggested_next_state == RecoveryState.PENDING_APPROVAL
    assert new_lc.state == RecoveryState.PENDING_APPROVAL


def test_validated_transition_auto_eligible_requires_flag():
    p = _payment(failure_code="gateway_timeout")
    lc = _lc(RecoveryState.ANALYZED)
    rec, new_lc = SVC.validated_transition(lc, p, auto_eligible=True, now=NOW)
    assert new_lc.state == RecoveryState.AUTO_ELIGIBLE
    # Without flag but forcing transition to AUTO_ELIGIBLE via P5.1 must fail
    from src.domain.recovery_lifecycle import transition
    with pytest.raises(InvalidTransitionError):
        transition(lc, RecoveryState.AUTO_ELIGIBLE, p, auto_eligible=False)


def test_validated_transition_hard_stop_raises():
    # ANALYZED -> ESCALATED is valid even for hard-stop; the hard-stop violation
    # is that hard-stop cannot go to AUTO_ELIGIBLE/APPROVED/EXECUTING/RECOVERED.
    # So validated_transition for hard-stop returns ESCALATED and succeeds.
    p = _payment(failure_code="fraud_suspected", recoverable=False, label="Hard Fraud / Do-Not-Retry")
    lc = _lc(RecoveryState.ANALYZED)
    rec, new_lc = SVC.validated_transition(lc, p, auto_eligible=False, now=NOW)
    assert rec.suggested_next_state == RecoveryState.ESCALATED
    assert new_lc.state == RecoveryState.ESCALATED
    # But if we forge hard-stop -> APPROVED, P5.1 must raise HardStopViolation
    from src.domain.recovery_lifecycle import transition
    with pytest.raises(HardStopViolation):
        transition(_lc(RecoveryState.PENDING_APPROVAL), RecoveryState.APPROVED, p)


def test_validated_transition_delegates_graph():
    # RECEIVED cannot go directly to PENDING_APPROVAL — graph violation via P5.1
    p = _payment(failure_code="gateway_timeout")
    lc = _lc(RecoveryState.RECEIVED)
    # recommend suggests PENDING_APPROVAL, but transition from RECEIVED is invalid
    with pytest.raises(InvalidTransitionError):
        SVC.validated_transition(lc, p, auto_eligible=False, now=NOW)


def test_validated_transition_passes_reason_and_now():
    p = _payment(failure_code="gateway_timeout")
    lc = _lc(RecoveryState.ANALYZED)
    rec, new_lc = SVC.validated_transition(lc, p, auto_eligible=False, now=NOW, reason="test reason")
    assert new_lc.reason == "test reason"
    assert new_lc.updated_at == NOW


# ── no graph duplication ──────────────────────────────────────────

def test_no_duplicate_graph_in_p52():
    import pathlib
    app_src = pathlib.Path("src/application/recommend_recovery.py").read_text()
    domain_src = pathlib.Path("src/domain/recovery_recommendation.py").read_text()
    assert "ALLOWED_TRANSITIONS" not in app_src
    assert "ALLOWED_TRANSITIONS" not in domain_src
    assert "_HARD_STOP_FORBIDDEN" not in app_src
    assert "_HARD_STOP_FORBIDDEN" not in domain_src


def test_no_amount_threshold_logic_in_p52():
    import pathlib
    combined = pathlib.Path("src/application/recommend_recovery.py").read_text() + pathlib.Path("src/domain/recovery_recommendation.py").read_text()
    assert "HIGH_VALUE" not in combined
    assert "10000" not in combined
    assert "retry_count" not in combined  # P5.2 must not use retry_count
    assert "amount" not in combined.lower().replace("txn_id", "").replace("chosen_action", "") or combined.lower().count("amount") == 0
    # More precise: ensure no self.payment.amount pattern
    assert "payment.amount" not in combined
    assert "p.amount" not in combined


# ── purity ────────────────────────────────────────────────────────

def test_recommend_does_not_mutate_payment():
    p = _payment(failure_code="gateway_timeout")
    before = copy.deepcopy(p)
    SVC.recommend(p, auto_eligible=True)
    assert p == before


def test_recommend_is_deterministic():
    p = _payment(failure_code="mandate_revoked")
    assert SVC.recommend(p, auto_eligible=False) == SVC.recommend(p, auto_eligible=False)
    assert SVC.recommend(p, auto_eligible=True) == SVC.recommend(p, auto_eligible=True)


def test_escalated_is_terminal_via_validated_transition():
    p = _payment(failure_code="gateway_timeout")
    lc = _lc(RecoveryState.ESCALATED)
    # recommend would suggest PENDING_APPROVAL/AUTO_ELIGIBLE, but ESCALATED has no outgoing
    with pytest.raises(InvalidTransitionError):
        SVC.validated_transition(lc, p, auto_eligible=False, now=NOW)
