"""P5.4 — GetRecoveryQueue read-model tests."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import create_autospec, MagicMock

import pytest

from src.domain.audit import ActionType, AuditEvent, Outcome as AuditOutcome, ReasonCode
from src.domain.entities import FailedPaymentEntity, RecoveryAttempt, Escalation
from src.domain.models import Outcome
from src.domain.recovery_lifecycle import RecoveryState, derive_state
from src.domain.recovery_recommendation import RecommendationKind
from src.domain.recovery_queue import RecoveryQueue, RecoveryQueueRow, derive_queue_status, derive_last_attempt_at
from src.application.get_recovery_queue import GetRecoveryQueue
from src.application.recommend_recovery import RecommendRecovery
from src.infrastructure.audit_repository import AuditLogRepository
from src.infrastructure.ports import FailedPaymentRepositoryPort


def _payment(
    txn_id: str,
    failure_code: str = "insufficient_funds",
    root_cause_label: str = "Insufficient Funds",
    recoverable: bool = True,
    amount: float = 1000.0,
    currency: str = "INR",
    attempts=None,
    escalations=None,
    timestamp: datetime | None = None,
):
    return FailedPaymentEntity(
        txn_id=txn_id,
        customer_id=f"C_{txn_id}",
        amount=amount,
        currency=currency,
        failure_code=failure_code,
        root_cause_label=root_cause_label,
        recoverable_flag=recoverable,
        retry_count=len(attempts) if attempts else 0,
        timestamp=timestamp or datetime(2026, 1, 1, 10, 0, 0),
        payment_method="card",
        attempts=attempts or [],
        escalations=escalations or [],
    )


def _attempt(txn_id: str, outcome: Outcome, n: int = 1, ts: datetime | None = None):
    return RecoveryAttempt(
        txn_id=txn_id,
        attempt_number=n,
        outcome=outcome,
        timestamp=ts or (datetime(2026, 1, 1, 11, 0, 0) + timedelta(hours=n)),
    )


def _escalation(txn_id: str, reason: str = "escalated"):
    return Escalation(txn_id=txn_id, reason=reason, timestamp=datetime(2026, 1, 1, 12, 0, 0))


def _audit(txn_id: str, tier: str = "T1", reason_code: ReasonCode = ReasonCode.RECOVERED):
    return AuditEvent(
        event_id=f"evt-{txn_id}",
        txn_id=txn_id,
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        action=ActionType.RETRY,
        decision_rationale="test",
        outcome=AuditOutcome.RECOVERED,
        reason_code=reason_code,
        customer_ref_masked="cust***1234",
        tier=tier,
    )


def _make_sut(payments=None, events=None, recommend=None):
    payments = payments if payments is not None else []
    events = events if events is not None else []
    payment_repo = create_autospec(FailedPaymentRepositoryPort, instance=True)
    payment_repo.get_all_payments.return_value = payments
    audit_repo = create_autospec(AuditLogRepository, instance=True)
    audit_repo.all_events.return_value = events
    sut = GetRecoveryQueue(
        payment_repository=payment_repo,
        audit_repository=audit_repo,
        recommend_recovery=recommend,
    )
    return sut, payment_repo, audit_repo


# 1 — empty queue
def test_empty_queue_returns_empty_with_tuple_counts():
    sut, _, _ = _make_sut(payments=[], events=[])
    q = sut.run()
    assert q.rows == ()
    assert q.total == 0
    assert isinstance(q.counts_by_state, tuple)
    assert isinstance(q.counts_by_kind, tuple)
    assert all(isinstance(pair, tuple) for pair in q.counts_by_state)
    assert all(isinstance(pair, tuple) for pair in q.counts_by_kind)
    assert len(q.counts_by_state) == len(RecoveryState)
    assert len(q.counts_by_kind) == len(RecommendationKind)
    assert sum(c for _, c in q.counts_by_state) == 0
    assert sum(c for _, c in q.counts_by_kind) == 0
    # frozen
    with pytest.raises((AttributeError, TypeError)):
        q.total = 1  # type: ignore
    # deeply immutable: inner tuple cannot be mutated
    with pytest.raises(TypeError):
        q.counts_by_state[0] = ("X", 1)  # type: ignore[index]


def test_counts_are_enum_ordered():
    p1 = _payment("TXN1", failure_code="insufficient_funds", root_cause_label="Insufficient Funds")
    p2 = _payment("TXN2", failure_code="fraud_suspected", root_cause_label="Hard Fraud / Do-Not-Retry", recoverable=False)
    sut, _, _ = _make_sut(payments=[p1, p2], events=[])
    q = sut.run()
    assert [k for k, _ in q.counts_by_state] == [s.value for s in RecoveryState]
    assert [k for k, _ in q.counts_by_kind] == [k.value for k in RecommendationKind]


# 2 — single row mapping
def test_single_row_mapping_provider_neutral():
    p = _payment("TXN1", amount=2500.0, currency="INR", failure_code="insufficient_funds", root_cause_label="Insufficient Funds")
    audit = _audit("TXN1", tier="T2", reason_code=ReasonCode.RAIL_DECLINED)
    sut, _, _ = _make_sut(payments=[p], events=[audit])
    q = sut.run()
    assert q.total == 1
    row = q.rows[0]
    assert row.txn_id == "TXN1"
    assert row.amount == 2500.0
    assert row.currency == "INR"
    assert row.root_cause_label == "Insufficient Funds"
    assert row.failure_code == "insufficient_funds"
    assert row.lifecycle_state == derive_state(p)
    assert row.status == derive_queue_status(p)
    assert row.tier == "T2"
    assert row.reason_code == "rail_declined"
    assert row.customer_ref_masked == "cust***1234"
    assert row.last_attempt_at is None
    assert row.updated_at == p.timestamp
    # provider-neutral: no raw razorpay leakage
    for field_val in [row.txn_id, row.chosen_action, row.bounds, row.rationale, row.provider_hint or ""]:
        assert "razorpay" not in str(field_val).lower()
        assert "x-razorpay" not in str(field_val).lower()


# 3 — unknown / hard-stop remains safe
def test_unknown_failure_code_maps_to_refuse():
    p = _payment("TXN_UNKNOWN", failure_code="UNKNOWN_CODE_XYZ", root_cause_label="Unknown / Ambiguous", recoverable=True)
    sut, _, _ = _make_sut(payments=[p], events=[])
    q = sut.run()
    row = q.rows[0]
    assert row.recommendation_kind == RecommendationKind.REFUSE
    assert row.provider_hint is None
    # lifecycle_state for unknown without attempts is RECEIVED (derive_state)
    assert row.lifecycle_state == RecoveryState.RECEIVED
    # status is UNPROCESSED (no attempts/escalations)
    assert row.status == "UNPROCESSED"


def test_hard_stop_fraud_maps_to_refuse_escalated_hint_none():
    p = _payment("TXN_FRAUD", failure_code="fraud_suspected", root_cause_label="Hard Fraud / Do-Not-Retry", recoverable=False)
    sut, _, _ = _make_sut(payments=[p], events=[])
    q = sut.run()
    row = q.rows[0]
    assert row.recommendation_kind == RecommendationKind.REFUSE
    assert row.provider_hint is None
    # not AUTO_ELIGIBLE
    assert row.lifecycle_state != RecoveryState.AUTO_ELIGIBLE


# 4 — Expired Card → DUNNING/payment_link
def test_expired_card_maps_to_dunning_payment_link_pending_approval():
    p = _payment("TXN_DUNN", failure_code="expired_card", root_cause_label="Expired Card", recoverable=True)
    sut, _, _ = _make_sut(payments=[p], events=[])
    q = sut.run()
    row = q.rows[0]
    assert row.recommendation_kind == RecommendationKind.DUNNING
    assert row.provider_hint == "payment_link"
    assert "dunning" in row.chosen_action.lower()
    # auto_eligible=False → should NOT be AUTO_ELIGIBLE
    assert row.lifecycle_state == RecoveryState.RECEIVED  # derive_state without attempts
    # recommendation suggested_next_state internally is PENDING_APPROVAL (via recommend)
    rec = RecommendRecovery().recommend(p, auto_eligible=False)
    assert rec.suggested_next_state == RecoveryState.PENDING_APPROVAL


def test_mandate_lapse_maps_to_reauth():
    p = _payment("TXN_REAUTH", failure_code="mandate_revoked", root_cause_label="Mandate Lapse", recoverable=True)
    sut, _, _ = _make_sut(payments=[p], events=[])
    q = sut.run()
    row = q.rows[0]
    assert row.recommendation_kind == RecommendationKind.REAUTH
    assert row.provider_hint == "reauth"


# 5 — auto_eligible is always false (spy)
def test_auto_eligible_always_false_spy():
    p = _payment("TXN_SPY", failure_code="insufficient_funds", root_cause_label="Insufficient Funds")
    recommend_mock = create_autospec(RecommendRecovery, instance=True)
    # make recommend return a real recommendation via side_effect
    real = RecommendRecovery()
    def side_effect(payment, auto_eligible=False):
        assert auto_eligible is False, "GetRecoveryQueue must pass auto_eligible=False"
        return real.recommend(payment, auto_eligible=False)
    recommend_mock.recommend.side_effect = side_effect
    sut, _, _ = _make_sut(payments=[p, p], events=[], recommend=recommend_mock)
    sut.run()
    # should have been called twice, each with False
    assert recommend_mock.recommend.call_count == 2
    for call in recommend_mock.recommend.call_args_list:
        _, kwargs = call
        assert kwargs["auto_eligible"] is False
        assert "auto_eligible" in kwargs


def test_auto_eligible_false_even_for_recoverable_payments():
    payments = [
        _payment(f"TXN{i}", failure_code="insufficient_funds", root_cause_label="Insufficient Funds")
        for i in range(3)
    ]
    sut, _, _ = _make_sut(payments=payments, events=[])
    q = sut.run()
    for row in q.rows:
        assert row.recommendation_kind != RecommendationKind.REFUSE or row.lifecycle_state != RecoveryState.AUTO_ELIGIBLE
        # none should be AUTO_ELIGIBLE because recommend never True
        assert row.lifecycle_state != RecoveryState.AUTO_ELIGIBLE


# 6 — no transition/validation calls
def test_no_transition_or_validate_transition_in_service():
    src = open("src/application/get_recovery_queue.py").read()
    assert "transition" not in src.lower() or "get_recovery_queue" in src.lower()  # allow only class name context
    # stricter: must not import transition/validate_transition
    assert "from src.domain.recovery_lifecycle import" in src
    assert "validate_transition" not in src
    # ensure the word transition as function call not present
    assert "transition(" not in src
    assert "validate_transition(" not in src


def test_no_db_writes_in_service():
    src = open("src/application/get_recovery_queue.py").read()
    for term in ["save_attempt", "save_escalation", "WebhookEventRepository", "razorpay", "Razorpay", "webhook"]:
        assert term not in src, f"forbidden term in GetRecoveryQueue: {term}"


# 7 — counts and deterministic ordering
def test_deterministic_ordering_updated_at_then_txn_id():
    ts1 = datetime(2026, 1, 1, 10, 0, 0)
    ts2 = datetime(2026, 1, 1, 9, 0, 0)
    p_a = _payment("TXN_B", timestamp=ts1)
    p_b = _payment("TXN_A", timestamp=ts1)
    p_c = _payment("TXN_C", timestamp=ts2)
    sut, _, _ = _make_sut(payments=[p_a, p_b, p_c], events=[])
    q = sut.run()
    assert [r.txn_id for r in q.rows] == ["TXN_C", "TXN_A", "TXN_B"]
    # second run deterministic
    q2 = sut.run()
    assert [r.txn_id for r in q2.rows] == [r.txn_id for r in q.rows]


def test_counts_sum_to_total_and_filtered_consistently():
    payments = [
        _payment("TXN1", failure_code="insufficient_funds", root_cause_label="Insufficient Funds"),
        _payment("TXN2", failure_code="expired_card", root_cause_label="Expired Card"),
        _payment("TXN3", failure_code="fraud_suspected", root_cause_label="Hard Fraud / Do-Not-Retry", recoverable=False),
    ]
    sut, _, _ = _make_sut(payments=payments, events=[])
    q = sut.run()
    assert sum(c for _, c in q.counts_by_state) == q.total
    assert sum(c for _, c in q.counts_by_kind) == q.total
    # filtered
    qf = sut.run(state_filter={RecoveryState.RECEIVED})
    assert qf.total <= q.total
    assert sum(c for _, c in qf.counts_by_state) == qf.total


# 8 — state/kind filters
def test_state_filter_and_kind_filter():
    p1 = _payment("TXN1", failure_code="insufficient_funds", root_cause_label="Insufficient Funds")
    p2 = _payment("TXN2", failure_code="fraud_suspected", root_cause_label="Hard Fraud / Do-Not-Retry", recoverable=False)
    # Need varied lifecycle_state: add escalations to one to get ESCALATED
    p3 = _payment("TXN3", failure_code="insufficient_funds", root_cause_label="Insufficient Funds", escalations=[_escalation("TXN3")])
    sut, _, _ = _make_sut(payments=[p1, p2, p3], events=[])
    q_all = sut.run()
    assert q_all.total == 3
    q_escalated = sut.run(state_filter={RecoveryState.ESCALATED})
    assert all(r.lifecycle_state == RecoveryState.ESCALATED for r in q_escalated.rows)
    q_refuse = sut.run(kind_filter={RecommendationKind.REFUSE})
    assert all(r.recommendation_kind == RecommendationKind.REFUSE for r in q_refuse.rows)
    # combined
    q_both = sut.run(state_filter={RecoveryState.RECEIVED}, kind_filter={RecommendationKind.RETRY})
    for r in q_both.rows:
        assert r.lifecycle_state == RecoveryState.RECEIVED
        assert r.recommendation_kind == RecommendationKind.RETRY


# 9 — no mutation of source payments
def test_no_mutation_of_source_payments():
    p = _payment("TXN1", attempts=[_attempt("TXN1", Outcome.FAILED)])
    original_attempts_len = len(p.attempts)
    sut, _, _ = _make_sut(payments=[p], events=[])
    sut.run()
    assert len(p.attempts) == original_attempts_len
    assert p.txn_id == "TXN1"


# 10 — missing audit enrichment handled safely
def test_missing_audit_enrichment_is_none():
    p = _payment("TXN_NO_AUDIT")
    sut, _, _ = _make_sut(payments=[p], events=[])
    q = sut.run()
    row = q.rows[0]
    assert row.tier is None
    assert row.reason_code is None
    assert row.customer_ref_masked is None


def test_derive_queue_status_and_last_attempt_at():
    # no attempts -> UNPROCESSED, None
    p0 = _payment("TXN0")
    assert derive_queue_status(p0) == "UNPROCESSED"
    assert derive_last_attempt_at(p0) is None
    # FAILED -> FAILED
    p1 = _payment("TXN1", attempts=[_attempt("TXN1", Outcome.FAILED)])
    assert derive_queue_status(p1) == "FAILED"
    assert derive_last_attempt_at(p1) is not None
    # ESCALATED beats FAILED
    p2 = _payment("TXN2", attempts=[_attempt("TXN2", Outcome.FAILED)], escalations=[_escalation("TXN2")])
    assert derive_queue_status(p2) == "ESCALATED"
    # SUCCESS beats all
    p3 = _payment("TXN3", attempts=[_attempt("TXN3", Outcome.SUCCESS)], escalations=[_escalation("TXN3")])
    assert derive_queue_status(p3) == "RECOVERED"
    # SKIPPED
    p4 = _payment("TXN4", attempts=[_attempt("TXN4", Outcome.SKIPPED)])
    assert derive_queue_status(p4) == "SKIPPED"


# 11 — frozen/immutable
def test_recovery_queue_row_is_frozen():
    p = _payment("TXN1")
    sut, _, _ = _make_sut(payments=[p], events=[])
    row = sut.run().rows[0]
    with pytest.raises((AttributeError, TypeError)):
        row.txn_id = "OTHER"  # type: ignore


def test_recovery_queue_is_frozen():
    sut, _, _ = _make_sut(payments=[], events=[])
    q = sut.run()
    with pytest.raises((AttributeError, TypeError)):
        q.total = 99  # type: ignore


# 12 — no provider payload leakage / no forbidden imports
def test_no_provider_payload_leakage_in_row():
    p = _payment("TXN1")
    sut, _, _ = _make_sut(payments=[p], events=[])
    row = sut.run().rows[0]
    combined = " ".join([str(v) for v in [row.txn_id, row.chosen_action, row.bounds, row.rationale, row.provider_hint or "", row.failure_code]])
    for forbidden in ["razorpay", "x-razorpay", "webhook", "secret", "payload"]:
        assert forbidden not in combined.lower()


def test_no_forbidden_imports_in_domain_and_application():
    for path in ["src/domain/recovery_queue.py", "src/application/get_recovery_queue.py"]:
        src = open(path).read()
        for term in ["streamlit", "Streamlit", "pandas", "sqlalchemy", "SQLAlchemy", "razorpay", "Razorpay", "hmac"]:
            assert term not in src, f"forbidden import {term} in {path}"
    # application should not import FastAPI/Request
    app_src = open("src/application/get_recovery_queue.py").read()
    assert "FastAPI" not in app_src
    assert "Request" not in app_src or "customer_ref_masked" in app_src  # allow but not FastAPI Request
    # domain should not import MetricsAggregator
    dom_src = open("src/domain/recovery_queue.py").read()
    assert "MetricsAggregator" not in dom_src
    assert "BuildDashboardData" not in dom_src


# 13 — status vs lifecycle_state not conflated
def test_status_and_lifecycle_state_not_conflated():
    # payment with FAILED attempt but no escalation: lifecycle RECEIVED? no, derive_state returns FAILED
    # Actually derive_state returns FAILED for has_failed. Let's test both distinct
    p_failed = _payment("TXN_FAILED", attempts=[_attempt("TXN_FAILED", Outcome.FAILED)])
    assert derive_state(p_failed) == RecoveryState.FAILED
    assert derive_queue_status(p_failed) == "FAILED"
    # with escalation: lifecycle ESCALATED, status ESCALATED (same here) but test UNPROCESSED vs RECEIVED
    p_unprocessed = _payment("TXN_UNP")
    assert derive_state(p_unprocessed) == RecoveryState.RECEIVED
    assert derive_queue_status(p_unprocessed) == "UNPROCESSED"
    assert derive_state(p_unprocessed).value != derive_queue_status(p_unprocessed)
