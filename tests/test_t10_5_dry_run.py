"""T10.5 — Reproducible dry-run: FullBatchReplay -> demo DB -> BuildDashboardData -> dashboard (read-only)."""
from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

from src.application.build_dashboard_data import BuildDashboardData
from src.application.full_batch_replay import FullBatchReplay
from src.domain.metrics import MetricsAggregator
from src.infrastructure.audit_repository import AuditLogRepository
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import dashboard as dash


def test_fullbatch_replay_reproducibility():
    a = FullBatchReplay(seed=42, count=60).execute()
    b = FullBatchReplay(seed=42, count=60).execute()
    assert a == b
    # full fingerprint field check
    assert a.seed == 42
    assert a.batch_size == 60
    assert a.total_processed == 60
    assert a.money_recovered == b.money_recovered
    assert a.recoverable_denominator == b.recoverable_denominator
    assert a.audit_event_count == b.audit_event_count
    assert a.tier_counts == b.tier_counts
    assert a.outcome_counts == b.outcome_counts


def test_demo_db_reproducibility_via_build_dashboard_data():
    # Use temp demo DBs isolated from repo demo_* files
    tmpdir = tempfile.mkdtemp()
    p_url = f"sqlite:///{tmpdir}/t105_payments.db"
    a_url = f"sqlite:///{tmpdir}/t105_audit.db"

    # Seed via FullBatchReplay file-backed mode
    fp1 = FullBatchReplay(seed=42, count=60, payments_db_url=p_url, audit_db_url=a_url).execute()
    # Build metrics twice
    p_engine = create_engine(p_url, echo=False)
    PSess = sessionmaker(bind=p_engine, autoflush=False, autocommit=False)
    pay_repo = SQLiteFailedPaymentRepository(session_factory=PSess)
    aud_repo = AuditLogRepository(db_url=a_url)
    agg = MetricsAggregator()
    b1 = BuildDashboardData(pay_repo, aud_repo, agg).run()
    b2 = BuildDashboardData(pay_repo, aud_repo, agg).run()
    assert b1 == b2

    # Re-seed and verify identical (fresh state, not appended)
    fp2 = FullBatchReplay(seed=42, count=60, payments_db_url=p_url, audit_db_url=a_url).execute()
    assert fp1 == fp2
    # Reload after reseed still identical
    p_engine2 = create_engine(p_url, echo=False)
    PSess2 = sessionmaker(bind=p_engine2, autoflush=False, autocommit=False)
    pay_repo2 = SQLiteFailedPaymentRepository(session_factory=PSess2)
    aud_repo2 = AuditLogRepository(db_url=a_url)
    b3 = BuildDashboardData(pay_repo2, aud_repo2, MetricsAggregator()).run()
    assert b1 == b3
    assert b1.total_processed == 60


def test_reconciliation_between_fingerprint_and_dashboard_metrics():
    tmpdir = tempfile.mkdtemp()
    p_url = f"sqlite:///{tmpdir}/t105_rec_pay.db"
    a_url = f"sqlite:///{tmpdir}/t105_rec_aud.db"
    fp = FullBatchReplay(seed=42, count=60, payments_db_url=p_url, audit_db_url=a_url).execute()
    p_engine = create_engine(p_url, echo=False)
    PSess = sessionmaker(bind=p_engine, autoflush=False, autocommit=False)
    pay_repo = SQLiteFailedPaymentRepository(session_factory=PSess)
    aud_repo = AuditLogRepository(db_url=a_url)
    m = BuildDashboardData(pay_repo, aud_repo, MetricsAggregator()).run()
    assert m.money_recovered == fp.money_recovered
    assert m.money_recoverable == fp.recoverable_denominator
    assert m.total_processed == fp.total_processed
    assert m.total_events == fp.audit_event_count
    assert m.tier_breakdown == fp.tier_counts


def test_dashboard_source_is_read_only():
    src = pathlib.Path("dashboard.py").read_text()
    for term in ["execute_attempt", "save_attempt", "save_escalation", "ExecuteRecoveryBatch", "MockPaymentRail"]:
        assert term not in src, f"read-only violation: {term}"
    # No DB mutation primitives in dashboard
    assert "drop_all" not in src
    # P5.5 operator decision control is the only approved write primitive
    assert 'st.button("Submit decision"' in src or "st.button('Submit decision'" in src
    # DEMO_MODE must only select data source, not seed
    assert "FullBatchReplay" not in src
    assert "seed_demo" not in src.lower()


def test_dashboard_starts_normal_mode():
    try:
        from streamlit.testing.v1 import AppTest
    except Exception:
        pytest.skip("Streamlit AppTest not available")
    # Ensure DEMO_MODE not set
    os.environ.pop("DEMO_MODE", None)
    import importlib
    importlib.reload(dash)
    root = pathlib.Path(__file__).resolve().parent.parent / "dashboard.py"
    at = AppTest.from_file(str(root), default_timeout=15)
    at.run()
    assert not at.exception, f"normal mode raised: {at.exception}"
    assert len(at.metric) >= 4
    titles = [str(t.value) for t in at.title]
    assert any("AI Revenue Recovery" in t for t in titles)


def test_dashboard_starts_demo_mode():
    try:
        from streamlit.testing.v1 import AppTest
    except Exception:
        pytest.skip("Streamlit AppTest not available")
    # Ensure demo DBs exist deterministically
    FullBatchReplay(seed=42, count=60, payments_db_url="sqlite:///demo_failed_payments.db", audit_db_url="sqlite:///demo_audit_log.db").execute()
    os.environ["DEMO_MODE"] = "1"
    import importlib
    importlib.reload(dash)
    root = pathlib.Path(__file__).resolve().parent.parent / "dashboard.py"
    at = AppTest.from_file(str(root), default_timeout=15)
    try:
        at.run()
        assert not at.exception, f"demo mode raised: {at.exception}"
        assert len(at.metric) >= 4
    finally:
        os.environ.pop("DEMO_MODE", None)
        importlib.reload(dash)


def test_seed_demo_requires_confirm_and_demo_urls(tmp_path=None):
    import subprocess
    import sys
    # without --confirm must exit 2
    r = subprocess.run([sys.executable, "scripts/seed_demo.py", "--seed", "42", "--count", "60"], capture_output=True, text=True)
    assert r.returncode == 2
    # with wrong URL must exit 2
    r2 = subprocess.run([sys.executable, "scripts/seed_demo.py", "--seed", "42", "--count", "60", "--confirm", "--payments-db-url", "sqlite:///failed_payments.db"], capture_output=True, text=True)
    assert r2.returncode == 2
    r3 = subprocess.run([sys.executable, "scripts/seed_demo.py", "--seed", "42", "--count", "60", "--confirm", "--audit-db-url", "sqlite:///audit_log.db"], capture_output=True, text=True)
    assert r3.returncode == 2
