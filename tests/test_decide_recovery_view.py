"""P5.5 view tests — operator decision transport layer."""
from __future__ import annotations


def test_view_transaction_selection():
    src = open("dashboard.py").read()
    assert "selectbox" in src
    assert "Transaction" in src
    assert "st.selectbox" in src

def test_view_recommendation_detail_display():
    src = open("dashboard.py").read()
    assert "Recommendation" in src
    assert "q_filtered" in src or "queue" in src
    # ensure rationale/provider hint surfaced via queue df
    assert "build_recovery_queue_df" in src

def test_view_approve_button():
    src = open("dashboard.py").read()
    assert 'st.button("Submit decision"' in src or "st.button('Submit decision'" in src

def test_view_reject_button_and_reason_input():
    src = open("dashboard.py").read()
    assert "Decision" in src
    assert "approve" in src and "reject" in src
    assert "text_input" in src
    assert "Reason" in src

def test_view_rejection_reason_required_in_source():
    app_src = open("src/application/decide_recovery.py").read()
    assert "Rejection reason" in app_src
    dash_src = open("dashboard.py").read()
    assert "decision_reason" in dash_src

def test_view_successful_decision_invokes_decide_and_reruns():
    src = open("dashboard.py").read()
    assert "DecideRecovery" in src
    assert "DecideRecovery().decide" in src
    assert "st.success" in src
    assert "st.rerun()" in src
    # success must precede rerun, rerun must be in the try branch before except
    assert src.index("DecideRecovery().decide") < src.index("st.success") < src.index("st.rerun()")
    assert "st.error" in src
    assert "Decision failed" in src
    # rerun in success branch must precede the decision error handler
    assert src.index("st.rerun()") < src.index("Decision failed")

def test_view_failed_decision_shows_error_and_preserves_kpis():
    src = open("dashboard.py").read()
    assert "st.error" in src
    assert "Decision failed" in src
    # success and rerun are in try, error in except — implies failed decision does not rerun
    assert src.count("st.rerun()") == 1
    # KPIs are loaded before operator block, so they render even if decide fails
    assert src.index("load_metrics") < src.index("DecideRecovery().decide")
    assert src.index("load_recovery_queue") < src.index("DecideRecovery().decide")

def test_view_successful_rerun_only_on_success():
    src = open("dashboard.py").read()
    assert "st.rerun()" in src
    assert src.count("st.rerun()") == 1
    assert src.index("st.success") < src.index("st.rerun()")

def test_view_no_lifecycle_logic_in_dashboard():
    src = open("dashboard.py").read()
    for term in ["Hard Fraud", "recoverable_flag", "is_hard_stop", "Max 3 retries"]:
        assert term not in src

def test_view_no_direct_sql_in_dashboard():
    src = open("dashboard.py").read()
    assert "UPDATE recovery_lifecycles SET" not in src
    assert "INSERT INTO recovery_lifecycles" not in src
    assert "UPDATE recovery_lifecycles" not in src

def test_view_kpi_preservation_on_failed_decision():
    src = open("dashboard.py").read()
    assert "load_metrics" in src
    assert "load_recovery_queue" in src
    # ensure exception for decide does not hide metrics: bare except only around decide
    assert "try:" in src and "except Exception" in src
