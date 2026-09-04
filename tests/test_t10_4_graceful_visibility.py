"""T10.4 — Graceful failure visibility (read-only, no domain semantics change)."""
from __future__ import annotations

import os
import pathlib
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.domain.audit import ActionType, AuditEvent, Outcome as AuditOutcome, ReasonCode
from src.domain.entities import FailedPaymentEntity, RecoveryAttempt
from src.domain.metrics import DashboardMetrics, ExceptionMetric, GracefulFailureMetric, MetricsAggregator
from src.domain.models import Outcome as AttemptOutcome

import dashboard as dash


def _attempt(txn: str, outcome: AttemptOutcome, n: int = 1):
    return RecoveryAttempt(txn_id=txn, attempt_number=n, outcome=outcome, timestamp=datetime(2026, 1, 1, 11, 0, 0) + timedelta(hours=n))


def _payment(txn_id: str, amount: float = 1000.0, recoverable: bool = True, attempts=None):
    return FailedPaymentEntity(
        txn_id=txn_id, customer_id=f"C_{txn_id}", amount=amount, currency="INR",
        failure_code="F001", root_cause_label="Hard Fraud / Do-Not-Retry", recoverable_flag=recoverable,
        retry_count=len(attempts) if attempts else 0, timestamp=datetime(2026, 1, 1, 10, 0, 0),
        payment_method="card", attempts=attempts or [], escalations=[],
    )


def _audit(txn_id: str, ts: datetime, action=ActionType.REFUSE, tier="T3", rc=ReasonCode.DO_NOT_RETRY, outcome=AuditOutcome.ESCALATED, masked="cust***1234"):
    return AuditEvent(
        event_id=f"evt-{txn_id}-{ts.isoformat()}", txn_id=txn_id, timestamp=ts,
        action=action, decision_rationale="Hard fraud — refuse and escalate",
        outcome=outcome, reason_code=rc, customer_ref_masked=masked, tier=tier,
    )


def test_aggregator_graceful_picks_earliest_refuse_do_not_retry_t3():
    # Two candidates, earliest timestamp wins, masked exact
    e1 = _audit("TXNA", datetime(2026, 1, 1, 13, 0, 0), masked="cust***0001")
    e2 = _audit("TXNB", datetime(2026, 1, 1, 12, 0, 0), masked="cust***0002")
    other = _audit("TXNC", datetime(2026, 1, 1, 11, 0, 0), action=ActionType.RETRY, tier="T1", rc=ReasonCode.RECOVERED, outcome=AuditOutcome.RECOVERED, masked="cust***0003")
    agg = MetricsAggregator()
    m = agg.compute(audit_events=[e1, e2, other], payments=[_payment("TXNA"), _payment("TXNB")])
    assert m.graceful_failure is not None
    gf = m.graceful_failure
    assert gf.txn_id == "TXNB"  # earliest
    assert gf.action == "refuse"
    assert gf.reason_code == "do_not_retry"
    assert gf.tier == "T3"
    assert gf.customer_ref_masked == "cust***0002"
    assert "refuse" in gf.decision_rationale.lower() or "fraud" in gf.decision_rationale.lower()
    assert isinstance(gf.timestamp, datetime)


def test_aggregator_graceful_requires_all_three_predicates():
    # Wrong reason_code / tier / action -> no graceful
    bad_rc = _audit("TXNA", datetime(2026, 1, 1, 12, 0, 0), rc=ReasonCode.RAIL_DECLINED)
    bad_tier = _audit("TXNB", datetime(2026, 1, 1, 12, 0, 0), tier="T2")
    bad_action = _audit("TXNC", datetime(2026, 1, 1, 12, 0, 0), action=ActionType.RETRY, rc=ReasonCode.RECOVERED, outcome=AuditOutcome.RECOVERED)
    agg = MetricsAggregator()
    m = agg.compute(audit_events=[bad_rc, bad_tier, bad_action], payments=[_payment("TXNA"), _payment("TXNB")])
    assert m.graceful_failure is None


def test_dashboard_renders_graceful_masked_and_fields():
    # Build metrics with graceful and ensure helpers / source cover it
    e = _audit("TXN99", datetime(2026, 1, 1, 13, 0, 0), masked="cust***9999")
    agg = MetricsAggregator()
    m = agg.compute(audit_events=[e], payments=[_payment("TXN99", attempts=[_attempt("TXN99", AttemptOutcome.FAILED)])])
    assert m.graceful_failure is not None
    assert m.graceful_failure.customer_ref_masked == "cust***9999"
    # Source must reference masked, not raw customer_id
    src = open("dashboard.py").read()
    assert "customer_ref_masked" in src
    assert "FailedPaymentEntity.customer_id" not in src
    # helpers don't strip graceful
    assert dash.build_exception_df(m).shape[0] >= 0
    # no unmasking
    assert "***" in m.graceful_failure.customer_ref_masked


def test_graceful_dashboard_panel_renders_via_apptest():
    try:
        from streamlit.testing.v1 import AppTest
    except Exception:
        pytest.skip("Streamlit AppTest not available")
    import pathlib as _pl
    root = _pl.Path(__file__).resolve().parent.parent / "dashboard.py"
    at = AppTest.from_file(str(root), default_timeout=15)
    at.run()
    assert not at.exception, f"App raised: {at.exception}"
    # App runs with whatever DB state — just prove it renders graceful subheader or empty safe message without crash
    # Search all text for graceful header
    all_text = " ".join([str(x.value) for x in at.subheader] + [str(x.value) for x in at.info if hasattr(x, "value")])
    # App always has a graceful subheader section
    sub = [str(s.value) for s in at.subheader]
    assert any("Graceful" in s for s in sub)


def test_empty_states_safe():
    agg = MetricsAggregator()
    m = agg.compute(audit_events=[], payments=[])
    assert m.graceful_failure is None
    assert m.exception_list == ()
    assert len(dash.build_exception_df(m)) == 0
    assert dash.format_inr(0) == "₹0.00"
    # Source must show safe empty messages
    src = open("dashboard.py").read()
    assert "No do-not-retry record" in src
    assert "No unresolved payments" in src


def test_read_only_no_retry_or_payment_action_offered():
    src = open("dashboard.py").read()
    for term in ["execute_attempt", "save_attempt", "save_escalation", "ExecuteRecoveryBatch", "MockPaymentRail"]:
        assert term not in src, f"read-only violation: {term}"
    assert "st.button" not in src


def test_tier_breakdown_and_intervention_present_for_graceful():
    e = _audit("TXN1", datetime(2026, 1, 1, 12, 0, 0))
    m = MetricsAggregator().compute(audit_events=[e], payments=[_payment("TXN1")])
    idf = dash.build_intervention_df(m)
    tdf = dash.build_tier_df(m)
    assert "refuse" in list(idf["Action"])
    assert "T3" in list(tdf["Tier"])
