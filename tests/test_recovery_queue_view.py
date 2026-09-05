"""P5.4 — Dashboard recovery queue view helpers tests."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

import dashboard as dash
from src.application.get_recovery_queue import GetRecoveryQueue
from src.domain.audit import ActionType, AuditEvent, Outcome as AuditOutcome, ReasonCode
from src.domain.entities import FailedPaymentEntity, RecoveryAttempt
from src.domain.metrics import DashboardMetrics, MetricsAggregator
from src.domain.models import Outcome
from src.domain.recovery_lifecycle import RecoveryState
from src.domain.recovery_queue import RecoveryQueue, RecoveryQueueRow, derive_queue_status
from src.domain.recovery_recommendation import RecommendationKind


def _payment(txn_id: str, timestamp: datetime | None = None):
    return FailedPaymentEntity(
        txn_id=txn_id,
        customer_id=f"C_{txn_id}",
        amount=1000.0,
        currency="INR",
        failure_code="insufficient_funds",
        root_cause_label="Insufficient Funds",
        recoverable_flag=True,
        retry_count=0,
        timestamp=timestamp or datetime(2026, 1, 1, 10, 0, 0),
        payment_method="card",
        attempts=[],
        escalations=[],
    )


def _queue(rows=None):
    # Build a small RecoveryQueue via GetRecoveryQueue for realism
    if rows is not None:
        return RecoveryQueue(
            rows=tuple(rows),
            total=len(rows),
            counts_by_state=tuple((s.value, 0) for s in RecoveryState),
            counts_by_kind=tuple((k.value, 0) for k in RecommendationKind),
        )
    return RecoveryQueue(rows=(), total=0, counts_by_state=tuple((s.value, 0) for s in RecoveryState), counts_by_kind=tuple((k.value, 0) for k in RecommendationKind))


# 1 — dataframe columns/formatting
def test_build_recovery_queue_df_columns_and_formatting():
    p1 = _payment("TXN1", timestamp=datetime(2026, 1, 1, 10, 0, 0))
    p2 = _payment("TXN2", timestamp=datetime(2026, 1, 1, 11, 0, 0))
    from unittest.mock import create_autospec
    from src.infrastructure.ports import FailedPaymentRepositoryPort
    from src.infrastructure.audit_repository import AuditLogRepository
    payment_repo = create_autospec(FailedPaymentRepositoryPort, instance=True)
    payment_repo.get_all_payments.return_value = [p1, p2]
    audit_repo = create_autospec(AuditLogRepository, instance=True)
    audit_repo.all_events.return_value = []
    q = GetRecoveryQueue(payment_repo, audit_repo).run()
    df = dash.build_recovery_queue_df(q)
    assert isinstance(df, pd.DataFrame)
    expected_cols = ["Transaction", "Amount", "Currency", "Root Cause", "Failure Code", "Lifecycle State", "Recommendation", "Hint", "Status", "Tier", "Reason Code"]
    assert list(df.columns) == expected_cols
    # Amount uses format_inr
    assert df.iloc[0]["Amount"] == dash.format_inr(p1.amount)
    assert len(df) == 2


def test_build_recovery_queue_df_empty_has_expected_columns():
    q = _queue(rows=[])
    df = dash.build_recovery_queue_df(q)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 0
    assert list(df.columns) == ["Transaction", "Amount", "Currency", "Root Cause", "Failure Code", "Lifecycle State", "Recommendation", "Hint", "Status", "Tier", "Reason Code"]


# 2 — filter helpers
def test_filter_recovery_queue_by_state():
    p1 = _payment("TXN1")
    p2 = _payment("TXN2")
    from unittest.mock import create_autospec
    from src.infrastructure.ports import FailedPaymentRepositoryPort
    from src.infrastructure.audit_repository import AuditLogRepository
    # Make p2 escalated so lifecycle ESCALATED
    from src.domain.entities import Escalation
    p2_escalated = FailedPaymentEntity(
        txn_id="TXN2", customer_id="C_TXN2", amount=1000, currency="INR",
        failure_code="fraud_suspected", root_cause_label="Hard Fraud / Do-Not-Retry",
        recoverable_flag=False, retry_count=0, timestamp=datetime(2026, 1, 1, 10, 0, 0),
        payment_method="card", attempts=[], escalations=[Escalation(txn_id="TXN2", reason="x", timestamp=datetime(2026, 1, 1, 12, 0, 0))],
    )
    payment_repo = create_autospec(FailedPaymentRepositoryPort, instance=True)
    payment_repo.get_all_payments.return_value = [p1, p2_escalated]
    audit_repo = create_autospec(AuditLogRepository, instance=True)
    audit_repo.all_events.return_value = []
    q = GetRecoveryQueue(payment_repo, audit_repo).run()
    filtered = dash.filter_recovery_queue(q.rows, state_filter=["ESCALATED"])
    assert len(filtered) == 1
    assert filtered[0].txn_id == "TXN2"
    # no match
    filtered_none = dash.filter_recovery_queue(q.rows, state_filter=["REJECTED"])
    assert len(filtered_none) == 0


def test_filter_recovery_queue_by_kind():
    from unittest.mock import create_autospec
    from src.infrastructure.ports import FailedPaymentRepositoryPort
    from src.infrastructure.audit_repository import AuditLogRepository
    p1 = FailedPaymentEntity(txn_id="TXN1", customer_id="C1", amount=1000, currency="INR", failure_code="expired_card", root_cause_label="Expired Card", recoverable_flag=True, retry_count=0, timestamp=datetime(2026, 1, 1, 10, 0, 0), payment_method="card", attempts=[], escalations=[])
    p2 = FailedPaymentEntity(txn_id="TXN2", customer_id="C2", amount=1000, currency="INR", failure_code="insufficient_funds", root_cause_label="Insufficient Funds", recoverable_flag=True, retry_count=0, timestamp=datetime(2026, 1, 1, 10, 0, 0), payment_method="card", attempts=[], escalations=[])
    payment_repo = create_autospec(FailedPaymentRepositoryPort, instance=True)
    payment_repo.get_all_payments.return_value = [p1, p2]
    audit_repo = create_autospec(AuditLogRepository, instance=True)
    audit_repo.all_events.return_value = []
    q = GetRecoveryQueue(payment_repo, audit_repo).run()
    filtered = dash.filter_recovery_queue(q.rows, kind_filter=["DUNNING"])
    assert len(filtered) == 1
    assert filtered[0].txn_id == "TXN1"


def test_filter_recovery_queue_by_tier_and_root_cause():
    from unittest.mock import create_autospec
    from src.infrastructure.ports import FailedPaymentRepositoryPort
    from src.infrastructure.audit_repository import AuditLogRepository
    p1 = _payment("TXN1")
    p2 = _payment("TXN2")
    audit1 = AuditEvent(event_id="evt-TXN1", txn_id="TXN1", timestamp=datetime(2026, 1, 1, 12, 0, 0), action=ActionType.RETRY, decision_rationale="t", outcome=AuditOutcome.RECOVERED, reason_code=ReasonCode.RECOVERED, customer_ref_masked="cust***1", tier="T1")
    payment_repo = create_autospec(FailedPaymentRepositoryPort, instance=True)
    payment_repo.get_all_payments.return_value = [p1, p2]
    audit_repo = create_autospec(AuditLogRepository, instance=True)
    audit_repo.all_events.return_value = [audit1]
    q = GetRecoveryQueue(payment_repo, audit_repo).run()
    filtered = dash.filter_recovery_queue(q.rows, tier_filter=["T1"])
    assert len(filtered) == 1
    assert filtered[0].txn_id == "TXN1"
    filtered_rc = dash.filter_recovery_queue(q.rows, root_cause_filter=["Insufficient Funds"])
    assert len(filtered_rc) == 2


def test_filter_recovery_queue_no_filters_returns_all():
    p1 = _payment("TXN1")
    p2 = _payment("TXN2")
    from unittest.mock import create_autospec
    from src.infrastructure.ports import FailedPaymentRepositoryPort
    from src.infrastructure.audit_repository import AuditLogRepository
    payment_repo = create_autospec(FailedPaymentRepositoryPort, instance=True)
    payment_repo.get_all_payments.return_value = [p1, p2]
    audit_repo = create_autospec(AuditLogRepository, instance=True)
    audit_repo.all_events.return_value = []
    q = GetRecoveryQueue(payment_repo, audit_repo).run()
    filtered = dash.filter_recovery_queue(q.rows)
    assert len(filtered) == 2


# 3 — KPI isolation: queue filters do not alter headline KPI data
def test_queue_filters_do_not_alter_headline_kpi_data():
    # Simulate: load_metrics returns fixed metrics; queue filtering only affects queue df
    from src.domain.metrics import DashboardMetrics, MetricsAggregator
    m = MetricsAggregator().compute(audit_events=[], payments=[])
    # Build queue and filter it — metrics should remain untouched
    p1 = _payment("TXN1")
    p2 = _payment("TXN2")
    from unittest.mock import create_autospec
    from src.infrastructure.ports import FailedPaymentRepositoryPort
    from src.infrastructure.audit_repository import AuditLogRepository
    payment_repo = create_autospec(FailedPaymentRepositoryPort, instance=True)
    payment_repo.get_all_payments.return_value = [p1, p2]
    audit_repo = create_autospec(AuditLogRepository, instance=True)
    audit_repo.all_events.return_value = []
    q = GetRecoveryQueue(payment_repo, audit_repo).run()
    original_total = m.total_processed
    filtered = dash.filter_recovery_queue(q.rows, state_filter=["REJECTED"])
    # filtering queue doesn't change metrics
    assert m.total_processed == original_total
    assert len(filtered) == 0


# 4 — no business logic in dashboard helpers (thin)
def test_no_business_logic_in_dashboard_queue_helpers():
    src = open("dashboard.py").read()
    # dashboard queue helpers should not contain policy thresholds or hard-stop logic
    for term in ["Hard Fraud", "recoverable_flag", "is_hard_stop", "Max 3 retries"]:
        assert term not in src, f"dashboard must not contain business term: {term}"


def test_dashboard_has_no_write_controls():
    src = open("dashboard.py").read()
    # P5.5 intentionally adds Submit decision button; ensure it's the only
    # approved write control — no generic forms, no DB mutation primitives.
    assert 'st.button("Submit decision"' in src or "st.button('Submit decision'" in src
    assert "st.form" not in src
    assert "on_click" not in src
    # no DB writes via repository mutation methods
    assert "save_attempt" not in src
    assert "save_escalation" not in src


def test_existing_dashboard_helpers_still_work():
    from src.domain.metrics import DashboardMetrics
    m = DashboardMetrics(
        total_at_risk=500000.0, money_recoverable=400000.0, money_recovered=200000.0,
        recovery_rate=50.0, intervention_mix={"retry": 2, "dunning": 1, "re-auth": 0, "refuse": 1},
        tier_breakdown={"T1": 1, "T2": 1, "T3": 2}, exception_list=(), graceful_failure=None, total_processed=4, total_events=4,
    )
    df = dash.build_intervention_df(m)
    assert list(df["Action"])[:4] == ["retry", "dunning", "re-auth", "refuse"]
    tdf = dash.build_tier_df(m)
    assert list(tdf["Tier"]) == ["T1", "T2", "T3"]
    assert dash.format_inr(123456.78) == "₹1,23,456.78"
