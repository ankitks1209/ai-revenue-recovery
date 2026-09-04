"""T10.2 — BuildDashboardData orchestration tests."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, create_autospec

from src.domain.audit import ActionType, AuditEvent, Outcome as AuditOutcome, ReasonCode
from src.domain.entities import FailedPaymentEntity, RecoveryAttempt
from src.domain.metrics import DashboardMetrics, MetricsAggregator
from src.domain.models import Outcome
from src.application.build_dashboard_data import BuildDashboardData
from src.infrastructure.audit_repository import AuditLogRepository
from src.infrastructure.ports import FailedPaymentRepositoryPort


def _payment(txn_id: str, amount: float = 1000.0, recoverable: bool = True, attempts=None):
    return FailedPaymentEntity(
        txn_id=txn_id,
        customer_id=f"C_{txn_id}",
        amount=amount,
        currency="INR",
        failure_code="F001",
        root_cause_label="Insufficient Funds",
        recoverable_flag=recoverable,
        retry_count=len(attempts) if attempts else 0,
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        payment_method="card",
        attempts=attempts or [],
        escalations=[],
    )


def _attempt(txn_id: str, outcome: Outcome, n: int = 1):
    return RecoveryAttempt(
        txn_id=txn_id,
        attempt_number=n,
        outcome=outcome,
        timestamp=datetime(2026, 1, 1, 11, 0, 0) + timedelta(hours=n),
    )


def _audit(txn_id: str, action: ActionType = ActionType.RETRY, tier: str = "T1"):
    return AuditEvent(
        event_id=f"evt-{txn_id}",
        txn_id=txn_id,
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        action=action,
        decision_rationale="test",
        outcome=AuditOutcome.RECOVERED,
        reason_code=ReasonCode.RECOVERED,
        customer_ref_masked="cust***masked",
        tier=tier,
    )


def _make_sut(payments=None, events=None):
    payments = payments if payments is not None else []
    events = events if events is not None else []
    payment_repo = create_autospec(FailedPaymentRepositoryPort, instance=True)
    payment_repo.get_all_payments.return_value = payments
    audit_repo = create_autospec(AuditLogRepository, instance=True)
    audit_repo.all_events.return_value = events
    aggregator = create_autospec(MetricsAggregator, instance=True)
    # default aggregator return
    expected = MetricsAggregator().compute(audit_events=events, payments=payments)
    aggregator.compute.return_value = expected
    sut = BuildDashboardData(
        payment_repository=payment_repo,
        audit_repository=audit_repo,
        metrics_aggregator=aggregator,
    )
    return sut, payment_repo, audit_repo, aggregator, expected


def test_payment_repository_is_called():
    sut, payment_repo, _, _, _ = _make_sut()
    sut.run()
    payment_repo.get_all_payments.assert_called_once_with()


def test_audit_repository_is_called():
    sut, _, audit_repo, _, _ = _make_sut()
    sut.run()
    audit_repo.all_events.assert_called_once_with()


def test_actual_repository_signatures_respected():
    # get_all_payments takes no args, all_events takes no required args
    import inspect
    assert inspect.signature(FailedPaymentRepositoryPort.get_all_payments).parameters.keys() == {"self"}
    # all_events has optional since
    sig = inspect.signature(AuditLogRepository.all_events)
    assert "self" in sig.parameters
    # ensure our sut does not pass unexpected args
    sut, payment_repo, audit_repo, _, _ = _make_sut()
    sut.run()
    # no args beyond self
    assert payment_repo.get_all_payments.call_args == ((), {})
    assert audit_repo.all_events.call_args == ((), {})


def test_metrics_aggregator_delegated_exactly_once():
    payments = [_payment("T1")]
    events = [_audit("T1")]
    sut, _, _, aggregator, _ = _make_sut(payments=payments, events=events)
    sut.run()
    aggregator.compute.assert_called_once()
    # second call should be exactly once per run
    sut.run()
    assert aggregator.compute.call_count == 2


def test_exact_loaded_payments_passed_through():
    payments = [_payment("T1", amount=100), _payment("T2", amount=200)]
    events = []
    sut, _, _, aggregator, _ = _make_sut(payments=payments, events=events)
    sut.run()
    _, kwargs = aggregator.compute.call_args
    assert kwargs["payments"] is payments
    # also verify payments values
    assert kwargs["payments"][0].txn_id == "T1"


def test_exact_loaded_audit_events_passed_through():
    payments = []
    events = [_audit("E1", tier="T2"), _audit("E2", tier="T3")]
    sut, _, _, aggregator, _ = _make_sut(payments=payments, events=events)
    sut.run()
    _, kwargs = aggregator.compute.call_args
    assert kwargs["audit_events"] is events
    assert kwargs["audit_events"][1].tier == "T3"


def test_returned_dashboard_metrics_exposed_unchanged():
    payments = [_payment("T1", amount=1000, recoverable=True, attempts=[_attempt("T1", Outcome.SUCCESS)])]
    events = [_audit("T1")]
    sut, _, _, aggregator, expected = _make_sut(payments=payments, events=events)
    # override expected to a distinct instance
    distinct = DashboardMetrics(
        total_at_risk=1000.0,
        money_recoverable=1000.0,
        money_recovered=1000.0,
        recovery_rate=100.0,
        intervention_mix={ActionType.RETRY.value: 1, ActionType.DUNNING.value: 0, ActionType.REAUTH.value: 0, ActionType.REFUSE.value: 0},
        tier_breakdown={"T1": 1, "T2": 0, "T3": 0},
        exception_list=(),
        graceful_failure=None,
        total_processed=1,
        total_events=1,
    )
    aggregator.compute.return_value = distinct
    result = sut.run()
    assert result is distinct


def test_empty_repository_data_is_safe():
    sut, _, _, aggregator, expected = _make_sut(payments=[], events=[])
    result = sut.run()
    assert result == expected
    assert result.total_processed == 0
    assert result.total_events == 0
    assert result.recovery_rate == 0.0
    assert result.tier_breakdown == {"T1": 0, "T2": 0, "T3": 0}


def test_no_presentation_dependencies():
    src = open("src/application/build_dashboard_data.py").read()
    for term in ["streamlit", "Streamlit", "plotly", "Plotly", "pandas", "jinja", "html"]:
        assert term not in src, f"forbidden presentation term: {term}"
    # also ensure no SQLAlchemy query text in this file
    for term in ["create_engine", "Session", "select(", "query("]:
        assert term not in src, f"forbidden SQL term in BuildDashboardData: {term}"
    # no metric calculations duplicated
    assert "money_recovered" not in src
    assert "recovery_rate" not in src


def test_no_metric_calculation_in_build_dashboard_data():
    src = open("src/application/build_dashboard_data.py").read()
    # file should be thin: only get_all_payments, all_events, compute
    assert "get_all_payments" in src
    assert "all_events" in src
    assert "compute" in src
    # should not contain sum/round/Counter logic
    assert "Counter" not in src
    assert "round(" not in src


def test_existing_reporting_behavior_unchanged():
    # MetricsAggregator via BuildDashboardData must match direct computation
    payments = [
        _payment("A", amount=1000, recoverable=True, attempts=[_attempt("A", Outcome.SUCCESS)]),
        _payment("B", amount=2000, recoverable=True, attempts=[_attempt("B", Outcome.FAILED)]),
    ]
    events = [_audit("A"), _audit("B", tier="T2")]
    real_agg = MetricsAggregator()
    direct = real_agg.compute(audit_events=events, payments=payments)

    payment_repo = MagicMock(spec=FailedPaymentRepositoryPort)
    payment_repo.get_all_payments.return_value = payments
    audit_repo = MagicMock(spec=AuditLogRepository)
    audit_repo.all_events.return_value = events
    sut = BuildDashboardData(
        payment_repository=payment_repo,
        audit_repository=audit_repo,
        metrics_aggregator=real_agg,
    )
    via_sut = sut.run()
    assert via_sut == direct
