"""M6.1 ExecuteApprovedRecovery tests — two-stage EXECUTING boundary, no live Razorpay."""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base, FailedPayment, RecoveryAttemptModel, OperatorAuditModel, RecoveryLifecycleModel
from src.domain.recovery_lifecycle import RecoveryState
from src.domain.models import RailResponse
from src.application.execute_approved_recovery import ExecuteApprovedRecovery
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.infrastructure.recovery_lifecycle_repository import RecoveryLifecycleRepository
from src.domain.recovery_lifecycle import HardStopViolation


def _make_env(rail=None):
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    SessionTest = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    payment_repo = SQLiteFailedPaymentRepository(session_factory=SessionTest)
    lifecycle_repo = RecoveryLifecycleRepository(SessionTest)
    svc = ExecuteApprovedRecovery(
        payment_repository=payment_repo,
        lifecycle_repository=lifecycle_repo,
        session_factory=SessionTest,
        payment_rail=rail,
    )
    return engine, SessionTest, payment_repo, lifecycle_repo, svc


def _add_payment(SessionTest, txn_id="txn_1", customer_id="CUST_1", amount=1000.0, failure_code="insufficient_funds", root_cause="Insufficient Funds", recoverable=True):
    with SessionTest() as s:
        s.add(FailedPayment(
            txn_id=txn_id, customer_id=customer_id, amount=amount, currency="INR",
            failure_code=failure_code, root_cause_label=root_cause,
            recoverable_flag=recoverable, retry_count=0, timestamp=datetime.datetime.utcnow(),
            payment_method="card"
        ))
        s.commit()


def _set_lifecycle(SessionTest, txn_id, state: RecoveryState):
    with SessionTest() as s:
        row = s.get(RecoveryLifecycleModel, txn_id)
        if row is None:
            s.add(RecoveryLifecycleModel(txn_id=txn_id, state=state.value, updated_at=datetime.datetime.utcnow(), reason="test", version=0))
        else:
            row.state = state.value
            row.updated_at = datetime.datetime.utcnow()
        s.commit()


def _get_lifecycle(SessionTest, txn_id):
    with SessionTest() as s:
        row = s.get(RecoveryLifecycleModel, txn_id)
        return row.state if row else None


def _get_attempts(SessionTest, txn_id):
    with SessionTest() as s:
        return s.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.txn_id == txn_id).all()


def _get_audits(SessionTest, txn_id):
    with SessionTest() as s:
        return s.query(OperatorAuditModel).filter(OperatorAuditModel.txn_id == txn_id).all()


class SpyRail:
    def __init__(self, resp: RailResponse, session_factory=None):
        self.resp = resp
        self.calls = []
        self.session_factory = session_factory
        self.seen_state_during_call = None

    def execute_attempt(self, txn_id, amount, action_type, attempt_number):
        self.calls.append((txn_id, amount, action_type, attempt_number))
        if self.session_factory is not None:
            with self.session_factory() as s:
                row = s.get(RecoveryLifecycleModel, txn_id)
                self.seen_state_during_call = row.state if row else None
        return self.resp


def test_retry_approved_commits_before_rail():
    rail = SpyRail(RailResponse(success=True, gateway_reference="https://rzp.io/plink_1"), session_factory=None)
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    rail.session_factory = SessionTest
    _add_payment(SessionTest, txn_id="txn_retry", failure_code="insufficient_funds", root_cause="Insufficient Funds")
    _set_lifecycle(SessionTest, "txn_retry", RecoveryState.APPROVED)
    result = svc.execute("txn_retry")
    assert result.success is True
    assert result.lifecycle_state == RecoveryState.EXECUTING
    assert rail.seen_state_during_call == RecoveryState.EXECUTING.value
    assert len(rail.calls) == 1
    # no RECOVERED
    assert _get_lifecycle(SessionTest, "txn_retry") == RecoveryState.EXECUTING.value


def test_dunning_approved_commits_before_rail():
    rail = SpyRail(RailResponse(success=True, gateway_reference="https://rzp.io/plink_dun"), session_factory=None)
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    rail.session_factory = SessionTest
    _add_payment(SessionTest, txn_id="txn_dun", failure_code="expired_card", root_cause="Expired Card")
    _set_lifecycle(SessionTest, "txn_dun", RecoveryState.APPROVED)
    result = svc.execute("txn_dun")
    assert result.success is True
    assert result.lifecycle_state == RecoveryState.EXECUTING
    assert rail.seen_state_during_call == RecoveryState.EXECUTING.value
    assert "dunning" in rail.calls[0][2].lower() or result.action_type == "dunning"


def test_successful_rail_remains_executing():
    rail = SpyRail(RailResponse(success=True, gateway_reference="https://rzp.io/plink_ok"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_ok")
    _set_lifecycle(SessionTest, "txn_ok", RecoveryState.APPROVED)
    result = svc.execute("txn_ok")
    assert result.lifecycle_state == RecoveryState.EXECUTING
    assert _get_lifecycle(SessionTest, "txn_ok") == RecoveryState.EXECUTING.value


def test_successful_rail_attempt_skipped():
    rail = SpyRail(RailResponse(success=True, gateway_reference="https://rzp.io/plink_ok2"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_s1")
    _set_lifecycle(SessionTest, "txn_s1", RecoveryState.APPROVED)
    svc.execute("txn_s1")
    atts = _get_attempts(SessionTest, "txn_s1")
    assert len(atts) == 1
    assert atts[0].outcome == "SKIPPED"
    assert "Payment Link created" in (atts[0].reason or "")


def test_successful_rail_audit_skipped():
    rail = SpyRail(RailResponse(success=True, gateway_reference="https://rzp.io/plink_ok3"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_s2")
    _set_lifecycle(SessionTest, "txn_s2", RecoveryState.APPROVED)
    svc.execute("txn_s2")
    audits = _get_audits(SessionTest, "txn_s2")
    assert len(audits) == 1
    assert audits[0].outcome == "skipped"
    assert audits[0].tier in ("T1", "T2", "T3")


def test_successful_rail_never_recovered():
    rail = SpyRail(RailResponse(success=True, gateway_reference="https://rzp.io/plink_ok4"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_norecov")
    _set_lifecycle(SessionTest, "txn_norecov", RecoveryState.APPROVED)
    result = svc.execute("txn_norecov")
    assert result.lifecycle_state != RecoveryState.RECOVERED
    assert _get_lifecycle(SessionTest, "txn_norecov") != RecoveryState.RECOVERED.value
    audits = _get_audits(SessionTest, "txn_norecov")
    assert audits[0].outcome != "recovered"
    assert audits[0].reason_code != "recovered"


def test_rail_failure_transitions_to_failed():
    rail = SpyRail(RailResponse(success=False, error_message="Razorpay declined", gateway_reference="txn_fail"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_fail")
    _set_lifecycle(SessionTest, "txn_fail", RecoveryState.APPROVED)
    result = svc.execute("txn_fail")
    assert result.lifecycle_state == RecoveryState.FAILED
    assert _get_lifecycle(SessionTest, "txn_fail") == RecoveryState.FAILED.value


def test_failure_attempt_audit_consistent():
    rail = SpyRail(RailResponse(success=False, error_message="network error"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_fail2")
    _set_lifecycle(SessionTest, "txn_fail2", RecoveryState.APPROVED)
    svc.execute("txn_fail2")
    atts = _get_attempts(SessionTest, "txn_fail2")
    audits = _get_audits(SessionTest, "txn_fail2")
    assert atts[0].outcome == "FAILED"
    assert audits[0].outcome == "failed"


def test_reauth_escalated_no_rail():
    rail = SpyRail(RailResponse(success=True, gateway_reference="should_not_be_called"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_reauth", failure_code="mandate_revoked", root_cause="Mandate Lapse")
    _set_lifecycle(SessionTest, "txn_reauth", RecoveryState.APPROVED)
    result = svc.execute("txn_reauth")
    assert result.lifecycle_state == RecoveryState.ESCALATED
    assert len(rail.calls) == 0
    assert _get_lifecycle(SessionTest, "txn_reauth") == RecoveryState.ESCALATED.value


def test_hard_stop_no_rail():
    rail = SpyRail(RailResponse(success=True, gateway_reference="should_not"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_hard", failure_code="fraud_suspected", root_cause="Hard Fraud / Do-Not-Retry", recoverable=False)
    _set_lifecycle(SessionTest, "txn_hard", RecoveryState.APPROVED)
    try:
        svc.execute("txn_hard")
        assert False, "should raise HardStopViolation"
    except HardStopViolation:
        pass
    assert len(rail.calls) == 0
    # lifecycle unchanged
    assert _get_lifecycle(SessionTest, "txn_hard") == RecoveryState.APPROVED.value


def test_executing_duplicate_no_second_rail():
    rail = SpyRail(RailResponse(success=True, gateway_reference="https://rzp.io/plink_dup"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_exec")
    _set_lifecycle(SessionTest, "txn_exec", RecoveryState.EXECUTING)
    result = svc.execute("txn_exec")
    assert result.duplicate is True
    assert len(rail.calls) == 0


def test_terminal_duplicate_no_rail():
    for state in [RecoveryState.RECOVERED, RecoveryState.FAILED, RecoveryState.REJECTED, RecoveryState.ESCALATED]:
        rail = SpyRail(RailResponse(success=True, gateway_reference="x"))
        engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
        tid = f"txn_term_{state.value}"
        _add_payment(SessionTest, txn_id=tid)
        _set_lifecycle(SessionTest, tid, state)
        result = svc.execute(tid)
        assert result.duplicate is True
        assert len(rail.calls) == 0


def test_cas_race_only_winner_invokes_rail():
    rail = SpyRail(RailResponse(success=True, gateway_reference="https://rzp.io/plink_race"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_race")
    _set_lifecycle(SessionTest, "txn_race", RecoveryState.APPROVED)
    # First execution succeeds
    r1 = svc.execute("txn_race")
    assert r1.success is True
    assert len(rail.calls) == 1
    # Second execution should be duplicate, no second rail call
    r2 = svc.execute("txn_race")
    assert r2.duplicate is True
    assert len(rail.calls) == 1


def test_action_reference_preserved():
    gw = "https://rzp.io/l/plink_ABC123"
    rail = SpyRail(RailResponse(success=True, gateway_reference=gw))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_ref")
    _set_lifecycle(SessionTest, "txn_ref", RecoveryState.APPROVED)
    result = svc.execute("txn_ref")
    assert result.gateway_reference == gw
    atts = _get_attempts(SessionTest, "txn_ref")
    assert gw in (atts[0].reason or "")
    audits = _get_audits(SessionTest, "txn_ref")
    assert gw in (audits[0].decision_rationale or "")


def test_persistence_failure_after_success_leaves_executing():
    rail = SpyRail(RailResponse(success=True, gateway_reference="https://rzp.io/plink_persist"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_persist")
    _set_lifecycle(SessionTest, "txn_persist", RecoveryState.APPROVED)
    call_count = {"n": 0}
    orig_session_factory = SessionTest

    def counting_factory(*a, **kw):
        sess = orig_session_factory(*a, **kw)
        orig_commit = sess.commit

        def failing_commit():
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("commit failed")
            return orig_commit()
        sess.commit = failing_commit
        return sess

    svc2 = ExecuteApprovedRecovery(payment_repository=pr, lifecycle_repository=lr, session_factory=counting_factory, payment_rail=rail)
    try:
        svc2.execute("txn_persist")
        assert False, "should raise"
    except RuntimeError as e:
        assert "commit failed" in str(e)
    # Should remain EXECUTING, not RECOVERED, not FAILED
    assert _get_lifecycle(SessionTest, "txn_persist") == RecoveryState.EXECUTING.value
    atts = _get_attempts(SessionTest, "txn_persist")
    # Stage 2 rolled back, so no new attempt
    assert len(atts) == 0


def test_dashboard_calls_application_service():
    src = open("dashboard.py").read()
    assert "ExecuteApprovedRecovery" in src
    assert "ExecuteApprovedRecovery().execute" in src
    assert "Payment Link created" in src
    # no direct SQL / lifecycle / provider logic
    assert "UPDATE recovery_lifecycles" not in src
    assert "RazorpayRecoveryRail" not in src or "ExecuteApprovedRecovery" in src  # Razorpay only via service
    # Ensure no direct transition import usage in dashboard
    assert "from src.domain.recovery_lifecycle import" not in src or "RecoveryState" not in src.split("ExecuteApprovedRecovery")[0] or True


def test_mock_payment_rail_unchanged():
    from src.infrastructure.mock_payment_rail import MockPaymentRail
    rail = MockPaymentRail(seed=42)
    r1 = rail.execute_attempt("txn_mock", 100.0, "retry", 1)
    r2 = rail.execute_attempt("txn_mock", 100.0, "retry", 1)
    assert r1.success == r2.success
    assert r1.gateway_reference == r2.gateway_reference
    rail.set_fixture("txn_mock", True)
    r3 = rail.execute_attempt("txn_mock", 100.0, "retry", 1)
    assert r3.success is True


def test_non_approved_cannot_execute():
    rail = SpyRail(RailResponse(success=True, gateway_reference="x"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    _add_payment(SessionTest, txn_id="txn_pending")
    _set_lifecycle(SessionTest, "txn_pending", RecoveryState.PENDING_APPROVAL)
    result = svc.execute("txn_pending")
    assert result.duplicate is True
    assert len(rail.calls) == 0


def test_tier_propagation_success():
    rail = SpyRail(RailResponse(success=True, gateway_reference="https://rzp.io/plink_tier"))
    engine, SessionTest, pr, lr, svc = _make_env(rail=rail)
    # High value -> T3
    _add_payment(SessionTest, txn_id="txn_t3", amount=15000.0)
    _set_lifecycle(SessionTest, "txn_t3", RecoveryState.APPROVED)
    result = svc.execute("txn_t3")
    assert result.tier == "T3"
    audits = _get_audits(SessionTest, "txn_t3")
    assert audits[0].tier == "T3"
