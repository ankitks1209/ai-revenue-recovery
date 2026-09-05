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

# M6.1 targeted correction: approve must decide first, then execute only on applied APPROVED

def test_view_approve_calls_decide_before_execute():
    src = open("dashboard.py").read()
    assert "DecideRecovery().decide" in src
    assert "ExecuteApprovedRecovery().execute" in src
    assert src.index("DecideRecovery().decide") < src.index("ExecuteApprovedRecovery().execute")
    approve_start = src.index('if decision == "approve":')
    else_pos = src.index("else:", approve_start)
    approve_block = src[approve_start:else_pos]
    assert "DecideRecovery().decide" in approve_block
    assert "ExecuteApprovedRecovery().execute" in approve_block
    assert approve_block.index("DecideRecovery().decide") < approve_block.index("ExecuteApprovedRecovery().execute")

def test_view_execute_guarded_by_successful_approval():
    src = open("dashboard.py").read()
    assert "decide_result.applied" in src
    assert "APPROVED" in src
    decide_idx = src.index("decide_result = DecideRecovery().decide")
    guard_idx = src.index("decide_result.applied")
    exec_idx = src.index("ExecuteApprovedRecovery().execute")
    assert decide_idx < guard_idx < exec_idx
    approve_start = src.index('if decision == "approve":')
    else_pos = src.index("else:", approve_start)
    approve_block = src[approve_start:else_pos]
    assert "if decide_result.applied" in approve_block
    assert approve_block.index("if decide_result.applied") < approve_block.index("ExecuteApprovedRecovery().execute")

def test_view_approval_failure_does_not_execute():
    src = open("dashboard.py").read()
    submit_idx = src.index('st.button("Submit decision"')
    try_idx = src.index("try:", submit_idx)
    except_idx = src.index("except Exception", try_idx)
    try_block = src[try_idx:except_idx]
    assert "DecideRecovery().decide" in try_block
    assert "ExecuteApprovedRecovery().execute" in try_block
    assert try_block.index("DecideRecovery().decide") < try_block.index("ExecuteApprovedRecovery().execute")
    assert "decide_result.applied" in try_block

def test_view_reject_only_calls_decide():
    src = open("dashboard.py").read()
    approve_start = src.index('if decision == "approve":')
    else_idx = src.index("else:", approve_start)
    rerun_idx = src.index("st.rerun()", else_idx)
    else_block = src[else_idx:rerun_idx]
    assert "DecideRecovery().decide" in else_block
    assert "ExecuteApprovedRecovery" not in else_block

def test_view_no_sql_provider_lifecycle_in_dashboard():
    src = open("dashboard.py").read()
    # no direct SQL
    assert "UPDATE recovery_lifecycles SET" not in src
    assert "INSERT INTO recovery_lifecycles" not in src
    assert "UPDATE recovery_lifecycles" not in src
    # no direct lifecycle transition
    assert "transition(" not in src
    assert "RecoveryLifecycle(" not in src
    # no direct Razorpay HTTP/provider logic
    assert "httpx" not in src
    assert "requests.post" not in src
    assert "payment_links" not in src
    # dashboard must not import Razorpay rails directly beyond ExecuteApprovedRecovery
    assert "razorpay_recovery_rail" not in src.lower()
    assert "razorpay_payment_rail" not in src.lower()

def test_view_successful_approve_shows_payment_link_not_recovered():
    src = open("dashboard.py").read()
    assert "Payment Link created" in src
    # approve success must not claim RECOVERED
    # check the approve block does not contain RECOVERED
    approve_block = src[src.index('if decision == "approve"'): src.index('else:')]
    assert "RECOVERED" not in approve_block
    assert "recovered" not in approve_block.lower() or "Payment Link created" in approve_block
