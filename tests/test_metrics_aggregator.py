"""T10.1 — MetricsAggregator pure-domain tests."""

from datetime import datetime, timedelta
import inspect

import pytest

from src.domain.audit import ActionType, AuditEvent, Outcome as AuditOutcome, ReasonCode
from src.domain.entities import Escalation, FailedPaymentEntity, RecoveryAttempt
from src.domain.metrics import DashboardMetrics, MetricsAggregator
from src.domain.models import Outcome


def _payment(
    txn_id: str,
    amount: float = 1000.0,
    recoverable: bool = True,
    root_cause_label: str = "Insufficient Funds",
    attempts: list[RecoveryAttempt] | None = None,
    escalations: list[Escalation] | None = None,
) -> FailedPaymentEntity:
    return FailedPaymentEntity(
        txn_id=txn_id,
        customer_id=f"C_{txn_id}",
        amount=amount,
        currency="INR",
        failure_code="F001",
        root_cause_label=root_cause_label,
        recoverable_flag=recoverable,
        retry_count=len(attempts) if attempts else 0,
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        payment_method="card",
        attempts=attempts or [],
        escalations=escalations or [],
    )


def _attempt(txn_id: str, outcome: Outcome, n: int = 1, reason: str | None = None) -> RecoveryAttempt:
    return RecoveryAttempt(
        txn_id=txn_id,
        attempt_number=n,
        outcome=outcome,
        timestamp=datetime(2026, 1, 1, 11, 0, 0) + timedelta(hours=n),
        reason=reason,
    )


def _audit(
    txn_id: str,
    action: ActionType = ActionType.RETRY,
    tier: str = "T1",
    reason_code: ReasonCode = ReasonCode.RECOVERED,
    outcome: AuditOutcome = AuditOutcome.RECOVERED,
    customer_ref_masked: str = "cust***masked",
    timestamp: datetime | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=f"evt-{txn_id}",
        txn_id=txn_id,
        timestamp=timestamp or datetime(2026, 1, 1, 12, 0, 0),
        action=action,
        decision_rationale="test rationale",
        outcome=outcome,
        reason_code=reason_code,
        customer_ref_masked=customer_ref_masked,
        tier=tier,
    )


def test_empty_inputs():
    agg = MetricsAggregator()
    m = agg.compute(audit_events=[], payments=[])
    assert m.total_at_risk == 0.0
    assert m.money_recoverable == 0.0
    assert m.money_recovered == 0.0
    assert m.recovery_rate == 0.0
    assert m.total_processed == 0
    assert m.total_events == 0
    assert m.exception_list == ()
    assert m.graceful_failure is None
    assert m.tier_breakdown == {"T1": 0, "T2": 0, "T3": 0}
    assert m.intervention_mix[ActionType.RETRY.value] == 0
    assert m.intervention_mix[ActionType.REFUSE.value] == 0


def test_all_unrecoverable_denominator_zero():
    payments = [
        _payment("TXN1", amount=500.0, recoverable=False, attempts=[_attempt("TXN1", Outcome.FAILED, reason="declined")]),
        _payment("TXN2", amount=700.0, recoverable=False),
    ]
    agg = MetricsAggregator()
    m = agg.compute(audit_events=[], payments=payments)
    assert m.money_recoverable == 0.0
    assert m.money_recovered == 0.0
    assert m.recovery_rate == 0.0
    assert m.total_at_risk == 1200.0


def test_zero_recoverable_denominator_safety_with_success_on_unrecoverable():
    # Even if an unrecoverable payment has SUCCESS (edge), denominator still 0 => rate 0
    payments = [
        _payment("TXN1", amount=100.0, recoverable=False, attempts=[_attempt("TXN1", Outcome.SUCCESS)]),
    ]
    agg = MetricsAggregator()
    m = agg.compute(audit_events=[], payments=payments)
    assert m.money_recoverable == 0.0
    assert m.money_recovered == 100.0
    assert m.recovery_rate == 0.0


def test_recovery_rate_uses_ground_truth_denominator():
    # ground truth: 4000 recoverable, only 1000 recovered => 25%
    payments = [
        _payment("T1", amount=1000.0, recoverable=True, attempts=[_attempt("T1", Outcome.SUCCESS)]),
        _payment("T2", amount=3000.0, recoverable=True, attempts=[_attempt("T2", Outcome.FAILED, reason="declined")]),
    ]
    events = [
        _audit("T1", outcome=AuditOutcome.RECOVERED),
        _audit("T2", outcome=AuditOutcome.FAILED, reason_code=ReasonCode.RAIL_DECLINED),
    ]
    agg = MetricsAggregator()
    m = agg.compute(audit_events=events, payments=payments)
    assert m.money_recovered == 1000.0
    assert m.money_recoverable == 4000.0
    assert m.recovery_rate == 25.0
    # Would be 50% if denominator were count-based; must be 25%


def test_successful_recovery():
    payments = [
        _payment("TXN_OK", amount=1000.0, recoverable=True, attempts=[_attempt("TXN_OK", Outcome.SUCCESS)]),
        _payment("TXN_FAIL", amount=500.0, recoverable=True, attempts=[_attempt("TXN_FAIL", Outcome.FAILED, reason="declined")]),
    ]
    agg = MetricsAggregator()
    m = agg.compute(audit_events=[], payments=payments)
    assert m.money_recovered == 1000.0
    assert m.money_recoverable == 1500.0
    assert m.total_at_risk == 1500.0
    assert m.recovery_rate == round(1000 / 1500 * 100, 2)


def test_duplicate_success_counted_once():
    payments = [
        _payment(
            "TXN_MULTI",
            amount=1000.0,
            recoverable=True,
            attempts=[
                _attempt("TXN_MULTI", Outcome.SUCCESS, n=1),
                _attempt("TXN_MULTI", Outcome.SUCCESS, n=2),
            ],
        ),
    ]
    agg = MetricsAggregator()
    m = agg.compute(audit_events=[], payments=payments)
    assert m.money_recovered == 1000.0


def test_failed_and_escalated_in_exception_list():
    esc = Escalation(txn_id="TXN_ESC", reason="human review", timestamp=datetime(2026, 1, 1, 13, 0, 0))
    payments = [
        _payment("TXN_OK", amount=100.0, recoverable=True, attempts=[_attempt("TXN_OK", Outcome.SUCCESS)]),
        _payment("TXN_FAIL", amount=200.0, recoverable=True, attempts=[_attempt("TXN_FAIL", Outcome.FAILED, reason="rail declined")]),
        _payment("TXN_ESC", amount=300.0, recoverable=True, attempts=[_attempt("TXN_ESC", Outcome.FAILED, reason="declined")], escalations=[esc]),
        _payment("TXN_SKIP", amount=400.0, recoverable=True, attempts=[_attempt("TXN_SKIP", Outcome.SKIPPED, reason="hold")]),
    ]
    events = [
        _audit("TXN_FAIL", tier="T2", reason_code=ReasonCode.RAIL_DECLINED, outcome=AuditOutcome.FAILED),
        _audit("TXN_ESC", tier="T3", reason_code=ReasonCode.STOPPING_RULE_TRIP, outcome=AuditOutcome.ESCALATED),
        _audit("TXN_SKIP", tier="T2", reason_code=ReasonCode.RETRIES_EXHAUSTED, outcome=AuditOutcome.SKIPPED),
    ]
    agg = MetricsAggregator()
    m = agg.compute(audit_events=events, payments=payments)
    txn_ids = [e.txn_id for e in m.exception_list]
    assert "TXN_OK" not in txn_ids
    assert "TXN_FAIL" in txn_ids
    assert "TXN_ESC" in txn_ids
    assert "TXN_SKIP" in txn_ids
    # Ordering deterministic — input order preserved for exceptions
    assert txn_ids == ["TXN_FAIL", "TXN_ESC", "TXN_SKIP"]
    # Status mapping
    by_txn = {e.txn_id: e for e in m.exception_list}
    assert by_txn["TXN_FAIL"].status == "FAILED"
    assert by_txn["TXN_ESC"].status == "ESCALATED"
    assert by_txn["TXN_SKIP"].status == "SKIPPED"
    # Tier/reason_code enrichment from audit
    assert by_txn["TXN_FAIL"].tier == "T2"
    assert by_txn["TXN_FAIL"].reason_code == ReasonCode.RAIL_DECLINED.value


def test_intervention_mix():
    events = [
        _audit("T1", action=ActionType.RETRY),
        _audit("T2", action=ActionType.RETRY),
        _audit("T3", action=ActionType.DUNNING),
        _audit("T4", action=ActionType.REAUTH),
        _audit("T5", action=ActionType.REFUSE, tier="T3", reason_code=ReasonCode.DO_NOT_RETRY, outcome=AuditOutcome.ESCALATED),
    ]
    agg = MetricsAggregator()
    m = agg.compute(audit_events=events, payments=[])
    assert m.intervention_mix[ActionType.RETRY.value] == 2
    assert m.intervention_mix[ActionType.DUNNING.value] == 1
    assert m.intervention_mix[ActionType.REAUTH.value] == 1
    assert m.intervention_mix[ActionType.REFUSE.value] == 1
    # All expected keys present
    for k in [ActionType.RETRY.value, ActionType.DUNNING.value, ActionType.REAUTH.value, ActionType.REFUSE.value]:
        assert k in m.intervention_mix


def test_tier_breakdown():
    events = [
        _audit("T1", tier="T1"),
        _audit("T2", tier="T1"),
        _audit("T3", tier="T2"),
        _audit("T4", tier="T3"),
        _audit("T5", tier="T3"),
    ]
    agg = MetricsAggregator()
    m = agg.compute(audit_events=events, payments=[])
    assert m.tier_breakdown == {"T1": 2, "T2": 1, "T3": 2}
    # Zero-fill when no events for a tier
    m2 = MetricsAggregator().compute(audit_events=[_audit("X", tier="T1")], payments=[])
    assert m2.tier_breakdown == {"T1": 1, "T2": 0, "T3": 0}


def test_deterministic_repeated_computation():
    payments = [
        _payment("T1", amount=1000.0, recoverable=True, attempts=[_attempt("T1", Outcome.SUCCESS)]),
        _payment("T2", amount=2000.0, recoverable=True, attempts=[_attempt("T2", Outcome.FAILED, reason="x")]),
    ]
    events = [_audit("T1"), _audit("T2", tier="T2", reason_code=ReasonCode.RAIL_DECLINED, outcome=AuditOutcome.FAILED)]
    agg = MetricsAggregator()
    m1 = agg.compute(audit_events=events, payments=payments)
    m2 = agg.compute(audit_events=events, payments=payments)
    assert m1 == m2
    assert isinstance(m1, DashboardMetrics)
    assert isinstance(m1.exception_list, tuple)


def test_graceful_failure_extraction():
    t_early = datetime(2026, 1, 1, 9, 0, 0)
    t_late = datetime(2026, 1, 1, 10, 0, 0)
    events = [
        _audit("TXN_LATE", action=ActionType.REFUSE, tier="T3", reason_code=ReasonCode.DO_NOT_RETRY, outcome=AuditOutcome.ESCALATED, customer_ref_masked="cust***111", timestamp=t_late),
        _audit("TXN_EARLY", action=ActionType.REFUSE, tier="T3", reason_code=ReasonCode.DO_NOT_RETRY, outcome=AuditOutcome.ESCALATED, customer_ref_masked="cust***999", timestamp=t_early),
        _audit("TXN_OTHER", action=ActionType.RETRY, tier="T1"),
    ]
    agg = MetricsAggregator()
    m = agg.compute(audit_events=events, payments=[])
    assert m.graceful_failure is not None
    # Deterministic: earliest timestamp wins
    assert m.graceful_failure.txn_id == "TXN_EARLY"
    assert m.graceful_failure.tier == "T3"
    assert m.graceful_failure.reason_code == ReasonCode.DO_NOT_RETRY.value
    assert m.graceful_failure.customer_ref_masked == "cust***999"
    assert m.graceful_failure.action == ActionType.REFUSE.value
    # No matching event => None
    m2 = agg.compute(audit_events=[_audit("X", action=ActionType.RETRY)], payments=[])
    assert m2.graceful_failure is None


def test_consistency_with_generate_recovery_report():
    """MetricsAggregator money/recovery metrics must match GenerateRecoveryReport."""

    class _FakeRepo:
        def __init__(self, payments):
            self._payments = payments

        def get_all_payments(self):
            return self._payments

    payments = [
        _payment("A", amount=1000.0, recoverable=True, attempts=[_attempt("A", Outcome.SUCCESS)]),
        _payment("B", amount=2000.0, recoverable=True, attempts=[_attempt("B", Outcome.FAILED, reason="declined")]),
        _payment("C", amount=500.0, recoverable=False, attempts=[_attempt("C", Outcome.FAILED, reason="fraud")]),
    ]

    from src.application.generate_recovery_report import GenerateRecoveryReport

    fake_repo = _FakeRepo(payments)
    report = GenerateRecoveryReport(repository=fake_repo).generate_report()

    agg = MetricsAggregator()
    m = agg.compute(audit_events=[], payments=payments)

    assert m.money_recovered == report["money_recovered"]
    assert m.money_recoverable == report["recoverable_denominator"]
    assert m.recovery_rate == report["recovery_rate"]
    assert m.total_at_risk == report["total_at_risk"]
    assert m.total_processed == report["total_processed"]


def test_no_infrastructure_dependency():
    src = inspect.getsource(MetricsAggregator) + open("src/domain/metrics.py").read()
    # Domain file must not import infra stack
    forbidden = ["sqlalchemy", "Session", "Engine", "Streamlit", "streamlit", "plotly", "pandas", "AuditLogRepository", "FailedPaymentRepository"]
    for term in forbidden:
        # pandas/plotly/streamlit/sqlalchemy are hard forbids; Session/Engine checked via import
        if term in ("pandas", "plotly", "streamlit", "sqlalchemy", "Streamlit", "AuditLogRepository", "FailedPaymentRepository"):
            assert term not in open("src/domain/metrics.py").read(), f"forbidden import: {term}"
    # Also ensure no filesystem/network/logging side effects in compute
    assert "open(" not in inspect.getsource(MetricsAggregator.compute) or "open(" in open("src/domain/metrics.py").read() and False  # trivial check


def test_result_types_frozen():
    agg = MetricsAggregator()
    m = agg.compute(audit_events=[], payments=[])
    with pytest.raises(Exception):
        m.money_recovered = 999  # type: ignore
    # exception_list is tuple
    assert isinstance(m.exception_list, tuple)
