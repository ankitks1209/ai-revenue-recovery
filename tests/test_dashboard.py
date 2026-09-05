"""T10.3 — Streamlit dashboard read-only tests."""

from __future__ import annotations

import importlib
import inspect
import sys
from datetime import datetime, timedelta
from unittest.mock import create_autospec

import pandas as pd
import pytest

from src.domain.audit import ActionType, AuditEvent, Outcome as AuditOutcome, ReasonCode
from src.domain.entities import FailedPaymentEntity, RecoveryAttempt
from src.domain.metrics import DashboardMetrics, ExceptionMetric, GracefulFailureMetric, MetricsAggregator
from src.domain.models import Outcome

import dashboard as dash


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


def _audit(txn_id: str, action: ActionType = ActionType.RETRY, tier: str = "T1", reason_code: ReasonCode = ReasonCode.RECOVERED):
    return AuditEvent(
        event_id=f"evt-{txn_id}",
        txn_id=txn_id,
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        action=action,
        decision_rationale="test rationale",
        outcome=AuditOutcome.RECOVERED,
        reason_code=reason_code,
        customer_ref_masked="cust***1234",
        tier=tier,
    )


def _sample_metrics() -> DashboardMetrics:
    payments = [
        _payment("TXN1", amount=12345.67, recoverable=True, attempts=[_attempt("TXN1", Outcome.SUCCESS)]),
        _payment("TXN2", amount=5000.0, recoverable=True, attempts=[_attempt("TXN2", Outcome.FAILED)]),
    ]
    events = [
        _audit("TXN1", action=ActionType.RETRY, tier="T1", reason_code=ReasonCode.RECOVERED),
        AuditEvent(
            event_id="evt-TXN99",
            txn_id="TXN99",
            timestamp=datetime(2026, 1, 1, 13, 0, 0),
            action=ActionType.REFUSE,
            decision_rationale="Hard fraud — refuse and escalate",
            outcome=AuditOutcome.ESCALATED,
            reason_code=ReasonCode.DO_NOT_RETRY,
            customer_ref_masked="cust***9999",
            tier="T3",
        ),
    ]
    # Use MetricsAggregator to produce realistic metrics then override for determinism
    agg = MetricsAggregator()
    base = agg.compute(audit_events=events, payments=payments)
    # Build explicit metrics for deterministic checks
    return DashboardMetrics(
        total_at_risk=17345.67,
        money_recoverable=17345.67,
        money_recovered=12345.67,
        recovery_rate=71.17,
        intervention_mix={ActionType.RETRY.value: 1, ActionType.DUNNING.value: 0, ActionType.REAUTH.value: 0, ActionType.REFUSE.value: 1},
        tier_breakdown={"T1": 1, "T2": 0, "T3": 1},
        exception_list=(
            ExceptionMetric(txn_id="TXN2", amount=5000.0, root_cause_label="Insufficient Funds", status="FAILED", reason="Payment rail declined", tier="T1", reason_code="rail_declined"),
        ),
        graceful_failure=GracefulFailureMetric(
            txn_id="TXN99",
            tier="T3",
            reason_code="do_not_retry",
            customer_ref_masked="cust***9999",
            decision_rationale="Hard fraud — refuse and escalate",
            action="refuse",
            timestamp=datetime(2026, 1, 1, 13, 0, 0),
        ),
        total_processed=2,
        total_events=2,
    )


# 1 — import without side effects
def test_dashboard_imports_without_executing_payment_actions():
    # Re-import should not raise nor trigger rail/mutation
    mod = importlib.import_module("dashboard")
    assert hasattr(mod, "load_metrics")
    assert hasattr(mod, "format_inr")
    assert hasattr(mod, "build_intervention_df")
    # Importing should not have called load_metrics side-effect (guarded)
    # Verify no payment rail in module
    src = open("dashboard.py").read()
    assert "execute_attempt" not in src or "payment_rail.execute_attempt" not in src.replace(" ", "") is False
    # Stronger: ensure no top-level load_metrics call without guard
    assert "_is_streamlit_running" in src


# 2 — consumes real DashboardMetrics
def test_dashboard_consumes_real_dashboard_metrics():
    m = _sample_metrics()
    # helpers consume DashboardMetrics
    df = dash.build_intervention_df(m)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["Action", "Count"]
    tdf = dash.build_tier_df(m)
    assert list(tdf.columns) == ["Tier", "Count"]
    edf = dash.build_exception_df(m)
    assert "Transaction" in edf.columns
    assert "Amount" in edf.columns
    # format helpers
    assert dash.format_inr(m.money_recovered) == "₹12,345.67"
    assert dash.format_pct(m.recovery_rate) == "71.17%"


# 3 — KPI values come directly from DashboardMetrics
def test_kpi_values_come_directly_from_dashboard_metrics():
    m = DashboardMetrics(
        total_at_risk=500000.0,
        money_recoverable=400000.0,
        money_recovered=200000.0,
        recovery_rate=50.0,
        intervention_mix={"retry": 2, "dunning": 1, "re-auth": 0, "refuse": 1},
        tier_breakdown={"T1": 1, "T2": 1, "T3": 2},
        exception_list=(),
        graceful_failure=None,
        total_processed=4,
        total_events=4,
    )
    assert dash.format_inr(m.money_recovered) == "₹2,00,000.00"
    assert dash.format_inr(m.total_at_risk) == "₹5,00,000.00"
    assert dash.format_inr(m.money_recoverable) == "₹4,00,000.00"
    assert dash.format_pct(m.recovery_rate) == "50.00%"
    # Indian grouping check
    assert dash.format_inr(123456.78) == "₹1,23,456.78"
    assert dash.format_inr(0) == "₹0.00"


# 4 — no metric recalculation in dashboard code
def test_no_metric_recalculation_in_dashboard():
    src = open("dashboard.py").read()
    forbidden = [
        "recoverable_flag",
        "recoverable_denominator",
        "Outcome.SUCCESS",
        "sum(float(p.amount",
        "Counter(",
        "recovery_rate =",
        "money_recovered =",
    ]
    for term in forbidden:
        assert term not in src, f"dashboard must not contain business calculation: {term}"


# 5 — intervention panel renders
def test_intervention_panel_renders():
    m = _sample_metrics()
    df = dash.build_intervention_df(m)
    # Preserve order RETRY, DUNNING, REAUTH, REFUSE
    assert list(df["Action"])[:4] == ["retry", "dunning", "re-auth", "refuse"]
    # Counts match metrics
    assert df.set_index("Action").loc["retry", "Count"] == 1
    assert df.set_index("Action").loc["refuse", "Count"] == 1


# 6 — tier panel renders
def test_tier_panel_renders():
    m = _sample_metrics()
    df = dash.build_tier_df(m)
    assert list(df["Tier"]) == ["T1", "T2", "T3"]
    assert df.set_index("Tier").loc["T1", "Count"] == 1
    assert df.set_index("Tier").loc["T3", "Count"] == 1


# 7 — exception table renders
def test_exception_table_renders():
    m = _sample_metrics()
    df = dash.build_exception_df(m)
    assert len(df) == 1
    assert df.iloc[0]["Transaction"] == "TXN2"
    assert df.iloc[0]["Amount"] == 5000.0
    assert df.iloc[0]["Status"] == "FAILED"
    assert df.iloc[0]["Tier"] == "T1"
    assert "customer_id" not in " ".join(df.columns).lower()
    # No raw customer_id in source
    src = open("dashboard.py").read()
    # Exception columns must include required headers
    assert "Transaction" in src
    assert "Root Cause" in src
    assert "Reason Code" in src
    # Filter helper preserves tier/status/root cause
    filtered = dash.filter_exceptions(m.exception_list, tier_filter=["T1"])
    assert len(filtered) == 1
    filtered_empty = dash.filter_exceptions(m.exception_list, tier_filter=["T2"])
    assert len(filtered_empty) == 0


# 8 — graceful-failure panel renders masked reference
def test_graceful_failure_panel_renders_masked_reference():
    m = _sample_metrics()
    gf = m.graceful_failure
    assert gf is not None
    assert gf.customer_ref_masked == "cust***9999"
    assert "***" in gf.customer_ref_masked
    # Dashboard source must reference masked field verbatim, not customer_id
    src = open("dashboard.py").read()
    assert "customer_ref_masked" in src
    # Ensure dashboard does not unmask or show raw customer_id
    assert "FailedPaymentEntity.customer_id" not in src
    # Filter does not affect graceful — ensure helper exists
    assert "graceful_failure" in src


# 9 — empty state renders safely
def test_empty_state_renders_safely():
    empty = MetricsAggregator().compute(audit_events=[], payments=[])
    assert empty.total_at_risk == 0.0
    assert empty.recovery_rate == 0.0
    assert dash.format_inr(empty.money_recovered) == "₹0.00"
    assert dash.format_pct(empty.recovery_rate) == "0.00%"
    idf = dash.build_intervention_df(empty)
    assert (idf["Count"] == 0).all()
    tdf = dash.build_tier_df(empty)
    assert (tdf["Count"] == 0).all()
    edf = dash.build_exception_df(empty)
    assert len(edf) == 0
    assert list(edf.columns) == ["Transaction", "Amount", "Root Cause", "Status", "Tier", "Reason Code", "Reason"]
    # No division by zero
    filtered = dash.filter_exceptions(empty.exception_list)
    assert filtered == []
    assert empty.graceful_failure is None


# 10 — read-only behavior — no rail execution / mutation controls
def test_read_only_no_mutation_controls():
    src = open("dashboard.py").read()
    forbidden = [
        "save_attempt",
        "save_escalation",
        "ExecuteRecoveryBatch",
        "MockPaymentRail",
        "load_failed_payments_to_db",
    ]
    for term in forbidden:
        assert term not in src, f"dashboard must not contain {term} (read-only)"
    # rows.append for DataFrame building is allowed — audit mutation is forbidden separately
    # P5.5 intentionally introduces the operator decision button; legacy read-only
    # assertion on st.button is relaxed — the button must be the approved submit control.
    assert "Submit decision" in src
    assert 'st.button("Submit decision"' in src or "st.button('Submit decision'" in src
    # Ensure no execute_attempt call
    assert "execute_attempt" not in src
    # No database mutation via Session/commit
    # Allow SQLiteFailedPaymentRepository read-only construction
    assert "st.cache_resource" not in src  # spec says do NOT use cache_resource in first impl


# 11 — no Plotly dependency
def test_no_plotly_dependency():
    src = open("dashboard.py").read()
    assert "plotly" not in src.lower()
    assert "import plotly" not in src
    req = open("requirements.txt").read()
    assert "plotly" not in req.lower()
    assert "streamlit==1.63.0" in req
    # Dashboard uses native charts only
    assert "st.bar_chart" in src or "bar_chart" in src
    assert "st.dataframe" in src


# 12 — deterministic supplied data produces deterministic displayed values
def test_deterministic_supplied_data_produces_deterministic_display():
    m = _sample_metrics()
    # Call twice
    a1 = dash.format_inr(m.money_recovered)
    a2 = dash.format_inr(m.money_recovered)
    assert a1 == a2 == "₹12,345.67"
    df1 = dash.build_intervention_df(m)
    df2 = dash.build_intervention_df(m)
    pd.testing.assert_frame_equal(df1, df2)
    edf1 = dash.build_exception_df(m)
    edf2 = dash.build_exception_df(m)
    pd.testing.assert_frame_equal(edf1, edf2)
    f1 = dash.filter_exceptions(m.exception_list, tier_filter=["T1"])
    f2 = dash.filter_exceptions(m.exception_list, tier_filter=["T1"])
    assert f1 == f2


def test_load_metrics_returns_dashboard_metrics():
    # load_metrics is simple read-only flow; verify it returns DashboardMetrics
    # Use real file DBs — should not crash, may be zero or seeded
    result = dash.load_metrics()
    assert isinstance(result, DashboardMetrics)
    # All money fields are numeric, rate is 0-100
    assert isinstance(result.money_recovered, (int, float))
    assert isinstance(result.total_at_risk, (int, float))
    assert 0 <= result.recovery_rate <= 100 or result.recovery_rate == 0.0


def test_dashboard_app_test_smoke():
    """Streamlit AppTest smoke — dashboard starts without crash."""
    try:
        from streamlit.testing.v1 import AppTest
    except Exception:
        pytest.skip("Streamlit AppTest not available")
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "dashboard.py"
    at = AppTest.from_file(str(root), default_timeout=15)
    at.run()
    # App should not raise exception
    assert not at.exception, f"App raised: {at.exception}"
    # Title should be present
    titles = [str(t.value) for t in at.title]
    assert any("AI Revenue Recovery" in t for t in titles)
    # Metrics should be present (at least 4)
    assert len(at.metric) >= 4
    # Sidebar header present
    sidebar_headers = [str(h.value) for h in at.sidebar.header]
    assert any("View filters" in h for h in sidebar_headers)
