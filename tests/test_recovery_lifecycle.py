"""P5.1 — Pure domain lifecycle tests. No I/O, no DB, no rail."""

import copy
from datetime import datetime

import pytest

from src.domain.entities import FailedPaymentEntity, RecoveryAttempt, Escalation
from src.domain.models import Outcome
from src.domain.recovery_lifecycle import (
    ALLOWED_TRANSITIONS,
    HardStopViolation,
    InvalidTransitionError,
    RecoveryLifecycle,
    RecoveryState,
    derive_initial_state,
    derive_state,
    transition,
    validate_transition,
)


def _payment(
    label: str = "Transient/Network",
    recoverable: bool = True,
    txn_id: str = "TXN1",
    attempts=None,
    escalations=None,
) -> FailedPaymentEntity:
    return FailedPaymentEntity(
        txn_id=txn_id,
        customer_id="CUST1",
        amount=1000.0,
        currency="INR",
        failure_code="ERR01",
        root_cause_label=label,
        recoverable_flag=recoverable,
        retry_count=len([a for a in (attempts or []) if a.outcome in (Outcome.SUCCESS, Outcome.FAILED)]),
        timestamp=datetime(2026, 1, 1, 9, 0, 0),
        payment_method="card",
        attempts=list(attempts or []),
        escalations=list(escalations or []),
    )


def _lc(state: RecoveryState, txn_id: str = "TXN1") -> RecoveryLifecycle:
    return RecoveryLifecycle(
        txn_id=txn_id, state=state, updated_at=datetime(2026, 1, 1, 10, 0, 0)
    )


NOW = datetime(2026, 1, 1, 11, 0, 0)

# ── valid transitions ──────────────────────────────────────────────

def test_valid_received_to_analyzed():
    lc = _lc(RecoveryState.RECEIVED)
    nxt = transition(lc, RecoveryState.ANALYZED, _payment(), now=NOW)
    assert nxt.state == RecoveryState.ANALYZED
    assert nxt.txn_id == lc.txn_id


def test_valid_analyzed_to_pending_approval():
    lc = _lc(RecoveryState.ANALYZED)
    nxt = transition(lc, RecoveryState.PENDING_APPROVAL, _payment(), now=NOW)
    assert nxt.state == RecoveryState.PENDING_APPROVAL


def test_valid_analyzed_to_auto_eligible_non_hard_stop():
    lc = _lc(RecoveryState.ANALYZED)
    nxt = transition(lc, RecoveryState.AUTO_ELIGIBLE, _payment("Transient/Network"), now=NOW, auto_eligible=True)
    assert nxt.state == RecoveryState.AUTO_ELIGIBLE


def test_analyzed_to_auto_eligible_requires_explicit_flag():
    lc = _lc(RecoveryState.ANALYZED)
    p = _payment("Transient/Network")
    with pytest.raises(InvalidTransitionError):
        transition(lc, RecoveryState.AUTO_ELIGIBLE, p, now=NOW)
    with pytest.raises(InvalidTransitionError):
        transition(lc, RecoveryState.AUTO_ELIGIBLE, p, now=NOW, auto_eligible=False)
    # validate_transition parity
    with pytest.raises(InvalidTransitionError):
        validate_transition(RecoveryState.ANALYZED, RecoveryState.AUTO_ELIGIBLE, p)
    with pytest.raises(InvalidTransitionError):
        validate_transition(RecoveryState.ANALYZED, RecoveryState.AUTO_ELIGIBLE, p, auto_eligible=False)


def test_hard_stop_auto_eligible_true_still_hard_stop():
    p = _payment(label="Hard Fraud / Do-Not-Retry", recoverable=False)
    lc = _lc(RecoveryState.ANALYZED)
    with pytest.raises(HardStopViolation):
        transition(lc, RecoveryState.AUTO_ELIGIBLE, p, now=NOW, auto_eligible=True)
    with pytest.raises(HardStopViolation):
        validate_transition(RecoveryState.ANALYZED, RecoveryState.AUTO_ELIGIBLE, p, auto_eligible=True)


def test_valid_analyzed_to_escalated():
    lc = _lc(RecoveryState.ANALYZED)
    nxt = transition(lc, RecoveryState.ESCALATED, _payment(), now=NOW)
    assert nxt.state == RecoveryState.ESCALATED


def test_valid_pending_approval_to_approved():
    lc = _lc(RecoveryState.PENDING_APPROVAL)
    nxt = transition(lc, RecoveryState.APPROVED, _payment(), now=NOW)
    assert nxt.state == RecoveryState.APPROVED


def test_valid_pending_approval_to_rejected():
    lc = _lc(RecoveryState.PENDING_APPROVAL)
    nxt = transition(lc, RecoveryState.REJECTED, _payment(), now=NOW)
    assert nxt.state == RecoveryState.REJECTED


def test_valid_auto_eligible_to_approved():
    lc = _lc(RecoveryState.AUTO_ELIGIBLE)
    nxt = transition(lc, RecoveryState.APPROVED, _payment(), now=NOW)
    assert nxt.state == RecoveryState.APPROVED


def test_valid_auto_eligible_to_escalated():
    lc = _lc(RecoveryState.AUTO_ELIGIBLE)
    nxt = transition(lc, RecoveryState.ESCALATED, _payment(), now=NOW)
    assert nxt.state == RecoveryState.ESCALATED


def test_valid_approved_to_executing():
    lc = _lc(RecoveryState.APPROVED)
    nxt = transition(lc, RecoveryState.EXECUTING, _payment(), now=NOW)
    assert nxt.state == RecoveryState.EXECUTING


def test_valid_executing_to_recovered():
    lc = _lc(RecoveryState.EXECUTING)
    nxt = transition(lc, RecoveryState.RECOVERED, _payment(), now=NOW)
    assert nxt.state == RecoveryState.RECOVERED


def test_valid_executing_to_failed():
    lc = _lc(RecoveryState.EXECUTING)
    nxt = transition(lc, RecoveryState.FAILED, _payment(), now=NOW)
    assert nxt.state == RecoveryState.FAILED


# ── invalid transitions ────────────────────────────────────────────

def test_invalid_received_cannot_jump_to_approved():
    lc = _lc(RecoveryState.RECEIVED)
    with pytest.raises(InvalidTransitionError):
        transition(lc, RecoveryState.APPROVED, _payment(), now=NOW)


def test_invalid_analyzed_cannot_go_to_recovered():
    lc = _lc(RecoveryState.ANALYZED)
    with pytest.raises(InvalidTransitionError):
        transition(lc, RecoveryState.RECOVERED, _payment(), now=NOW)


def test_invalid_approved_cannot_go_to_recovered_directly():
    lc = _lc(RecoveryState.APPROVED)
    with pytest.raises(InvalidTransitionError):
        transition(lc, RecoveryState.RECOVERED, _payment(), now=NOW)


# ── terminal states ────────────────────────────────────────────────

@pytest.mark.parametrize("terminal", [RecoveryState.REJECTED, RecoveryState.ESCALATED, RecoveryState.RECOVERED, RecoveryState.FAILED])
def test_terminal_states_have_no_outgoing(terminal):
    assert ALLOWED_TRANSITIONS[terminal] == set()
    lc = _lc(terminal)
    for target in RecoveryState:
        with pytest.raises(InvalidTransitionError):
            transition(lc, target, _payment(), now=NOW)


# ── auto path cannot reject ────────────────────────────────────────

def test_auto_eligible_cannot_reject():
    lc = _lc(RecoveryState.AUTO_ELIGIBLE)
    with pytest.raises(InvalidTransitionError):
        transition(lc, RecoveryState.REJECTED, _payment(), now=NOW)


# ── hard-stop invariants ───────────────────────────────────────────

def test_hard_fraud_cannot_become_auto_eligible():
    p = _payment(label="Hard Fraud / Do-Not-Retry", recoverable=False)
    lc = _lc(RecoveryState.ANALYZED)
    with pytest.raises(HardStopViolation):
        transition(lc, RecoveryState.AUTO_ELIGIBLE, p, now=NOW)


def test_hard_fraud_cannot_become_approved():
    p = _payment(label="Hard Fraud / Do-Not-Retry", recoverable=False)
    lc = _lc(RecoveryState.PENDING_APPROVAL)
    with pytest.raises(HardStopViolation):
        transition(lc, RecoveryState.APPROVED, p, now=NOW)


def test_hard_fraud_cannot_become_executing():
    p = _payment(label="Hard Fraud / Do-Not-Retry", recoverable=False)
    lc = _lc(RecoveryState.APPROVED)
    # Even though APPROVED->EXECUTING is structurally allowed, hard-stop blocks it.
    # However APPROVED itself is unreachable for hard-stop; we test the guard directly
    # by attempting EXECUTING from APPROVED with hard-stop payment.
    with pytest.raises(HardStopViolation):
        transition(lc, RecoveryState.EXECUTING, p, now=NOW)


def test_hard_fraud_cannot_become_recovered():
    p = _payment(label="Hard Fraud / Do-Not-Retry", recoverable=False)
    lc = _lc(RecoveryState.EXECUTING)
    with pytest.raises(HardStopViolation):
        transition(lc, RecoveryState.RECOVERED, p, now=NOW)


def test_hard_fraud_can_escalate():
    p = _payment(label="Hard Fraud / Do-Not-Retry", recoverable=False)
    lc = _lc(RecoveryState.ANALYZED)
    nxt = transition(lc, RecoveryState.ESCALATED, p, now=NOW)
    assert nxt.state == RecoveryState.ESCALATED

    lc2 = _lc(RecoveryState.PENDING_APPROVAL)
    nxt2 = transition(lc2, RecoveryState.ESCALATED, p, now=NOW)
    assert nxt2.state == RecoveryState.ESCALATED


def test_unknown_ambiguous_is_hard_stop():
    p = _payment(label="Unknown / Ambiguous", recoverable=True)
    lc = _lc(RecoveryState.ANALYZED)
    with pytest.raises(HardStopViolation):
        transition(lc, RecoveryState.AUTO_ELIGIBLE, p, now=NOW)
    with pytest.raises(HardStopViolation):
        transition(_lc(RecoveryState.PENDING_APPROVAL), RecoveryState.APPROVED, p, now=NOW)
    # Unknown fallback category also hard-stop
    p2 = _payment(label="TotallyNewCategory", recoverable=True)
    with pytest.raises(HardStopViolation):
        transition(lc, RecoveryState.AUTO_ELIGIBLE, p2, now=NOW)


def test_graceful_do_not_retry_remains_escalated():
    # Non-hard-stop label but recoverable_flag=False triggers GracefulFailureHandler DO_NOT_RETRY
    p = _payment(label="Transient/Network", recoverable=False)
    lc = _lc(RecoveryState.ANALYZED)
    with pytest.raises(HardStopViolation):
        transition(lc, RecoveryState.AUTO_ELIGIBLE, p, now=NOW)
    with pytest.raises(HardStopViolation):
        transition(_lc(RecoveryState.PENDING_APPROVAL), RecoveryState.APPROVED, p, now=NOW)
    with pytest.raises(HardStopViolation):
        transition(_lc(RecoveryState.APPROVED), RecoveryState.EXECUTING, p, now=NOW)
    with pytest.raises(HardStopViolation):
        transition(_lc(RecoveryState.EXECUTING), RecoveryState.RECOVERED, p, now=NOW)
    # Must still allow escalation
    nxt = transition(lc, RecoveryState.ESCALATED, p, now=NOW)
    assert nxt.state == RecoveryState.ESCALATED


def test_hard_stop_violation_is_subclass():
    assert issubclass(HardStopViolation, InvalidTransitionError)


# ── derive state ───────────────────────────────────────────────────

def test_derive_existing_success_to_recovered():
    now = datetime(2026, 1, 1, 10, 0, 0)
    p = _payment(attempts=[RecoveryAttempt(txn_id="TXN1", attempt_number=1, outcome=Outcome.SUCCESS, timestamp=now)])
    assert derive_state(p) == RecoveryState.RECOVERED
    assert derive_initial_state(p) == RecoveryState.RECOVERED


def test_derive_existing_escalation_to_escalated():
    now = datetime(2026, 1, 1, 10, 0, 0)
    p = _payment(escalations=[Escalation(txn_id="TXN1", reason="hard stop", timestamp=now)])
    assert derive_state(p) == RecoveryState.ESCALATED


def test_derive_no_attempts_to_received():
    p = _payment()
    assert derive_state(p) == RecoveryState.RECEIVED


def test_derive_failed_attempt_to_failed():
    now = datetime(2026, 1, 1, 10, 0, 0)
    p = _payment(attempts=[RecoveryAttempt(txn_id="TXN1", attempt_number=1, outcome=Outcome.FAILED, timestamp=now, reason="declined")])
    assert derive_state(p) == RecoveryState.FAILED


def test_derive_success_takes_precedence_over_escalation():
    now = datetime(2026, 1, 1, 10, 0, 0)
    p = _payment(
        attempts=[RecoveryAttempt(txn_id="TXN1", attempt_number=1, outcome=Outcome.SUCCESS, timestamp=now)],
        escalations=[Escalation(txn_id="TXN1", reason="late", timestamp=now)],
    )
    # Success => RECOVERED even if escalation exists (recovered is terminal success)
    assert derive_state(p) == RecoveryState.RECOVERED


# ── deterministic / pure ───────────────────────────────────────────

def test_deterministic_pure_behavior():
    p = _payment()
    lc = _lc(RecoveryState.RECEIVED)
    nxt1 = transition(lc, RecoveryState.ANALYZED, p, now=NOW)
    nxt2 = transition(lc, RecoveryState.ANALYZED, p, now=NOW)
    assert nxt1 == nxt2
    # Separate now produces different updated_at but same state
    later = datetime(2026, 1, 1, 12, 0, 0)
    nxt3 = transition(lc, RecoveryState.ANALYZED, p, now=later)
    assert nxt3.state == nxt1.state
    assert nxt3.updated_at == later
    assert nxt1.updated_at == NOW


def test_input_payment_not_mutated():
    p = _payment(label="Transient/Network", recoverable=True)
    before = copy.deepcopy(p)
    lc = _lc(RecoveryState.RECEIVED)
    transition(lc, RecoveryState.ANALYZED, p, now=NOW)
    assert p == before


def test_input_lifecycle_not_mutated():
    p = _payment()
    lc = _lc(RecoveryState.RECEIVED)
    original_state = lc.state
    nxt = transition(lc, RecoveryState.ANALYZED, p, now=NOW)
    assert lc.state == original_state
    assert nxt.state == RecoveryState.ANALYZED


def test_lifecycle_is_frozen():
    lc = _lc(RecoveryState.RECEIVED)
    with pytest.raises(Exception):
        lc.state = RecoveryState.ANALYZED  # type: ignore


def test_transition_reason_preserved():
    p = _payment()
    lc = _lc(RecoveryState.PENDING_APPROVAL)
    nxt = transition(lc, RecoveryState.APPROVED, p, reason="operator approved", now=NOW)
    assert nxt.reason == "operator approved"


def test_recovery_state_is_string_enum():
    assert RecoveryState.RECEIVED.value == "RECEIVED"
    assert isinstance(RecoveryState.RECEIVED, str)
    assert RecoveryState("RECEIVED") == RecoveryState.RECEIVED
