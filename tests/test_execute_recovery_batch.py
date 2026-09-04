from datetime import datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.database import Base, FailedPayment as FailedPaymentModel
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.infrastructure.mock_payment_rail import MockPaymentRail
from src.infrastructure.ports import PaymentRailPort
from src.infrastructure.clock import SimulatedClock
from src.application.execute_recovery_batch import ExecuteRecoveryBatch
from src.domain.models import Outcome, RailResponse
from src.domain.audit import ActionType, Outcome as AuditOutcome, ReasonCode

@pytest.fixture
def batch_env():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionTest = sessionmaker(bind=engine)
    session = SessionTest()
    
    repo = SQLiteFailedPaymentRepository(session_factory=lambda: session)
    rail = MockPaymentRail()
    clock = SimulatedClock(datetime(2026, 1, 1, 10, 0, 0))
    
    yield session, repo, rail, clock
    session.close()

def test_batch_hard_fraud_never_reaches_rail(batch_env):
    session, repo, rail, clock = batch_env
    # Insert hard fraud record
    db_p = FailedPaymentModel(
        txn_id="TXN_FRAUD",
        customer_id="C1",
        amount=1000.0,
        currency="INR",
        failure_code="FRAUD",
        root_cause_label="Hard Fraud / Do-Not-Retry",
        recoverable_flag=False,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )
    session.add(db_p)
    session.commit()

    rail.force_all(True) # Even if rail is forced success, hard fraud must never call rail
    executor = ExecuteRecoveryBatch(repo, rail, clock)
    stats = executor.execute()

    assert stats["escalated_count"] == 1
    assert stats["executed_count"] == 0

    p = repo.get_payment_by_id("TXN_FRAUD")
    assert p.executed_retry_count == 0
    assert len(p.escalations) == 1
    assert len(p.attempts) == 1
    assert p.attempts[0].outcome == Outcome.ESCALATED

def test_batch_mandate_lapse_single_attempt_and_escalate(batch_env):
    session, repo, rail, clock = batch_env
    db_p = FailedPaymentModel(
        txn_id="TXN_MANDATE",
        customer_id="C2",
        amount=500.0,
        currency="INR",
        failure_code="MANDATE_FAIL",
        root_cause_label="Mandate Lapse",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="mandate"
    )
    session.add(db_p)
    session.commit()

    rail.set_fixture("TXN_MANDATE", False) # Force rail failure
    executor = ExecuteRecoveryBatch(repo, rail, clock)
    stats = executor.execute()

    assert stats["executed_count"] == 1
    assert stats["failed_count"] == 1
    assert stats["escalated_count"] == 1

    p = repo.get_payment_by_id("TXN_MANDATE")
    assert p.executed_retry_count == 1
    assert len(p.attempts) == 1
    assert p.attempts[0].outcome == Outcome.FAILED
    assert len(p.escalations) == 1

    # Running batch again should skip / not re-execute due to terminal state (escalated)
    stats2 = executor.execute()
    assert stats2["executed_count"] == 0

def test_batch_transient_backoff_and_timing(batch_env):
    session, repo, rail, clock = batch_env
    db_p = FailedPaymentModel(
        txn_id="TXN_TRANSIENT",
        customer_id="C3",
        amount=200.0,
        currency="INR",
        failure_code="TIMEOUT",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="upi"
    )
    session.add(db_p)
    session.commit()

    rail.set_fixture("TXN_TRANSIENT", False) # First attempt fails
    executor = ExecuteRecoveryBatch(repo, rail, clock)
    
    # 1st run: First attempt executes and fails
    stats1 = executor.execute()
    assert stats1["executed_count"] == 1
    assert stats1["failed_count"] == 1

    p = repo.get_payment_by_id("TXN_TRANSIENT")
    assert p.executed_retry_count == 1

    # 2nd run immediately (0 time elapsed): should be SKIPPED due to backoff (1h required for retry index 1)
    stats2 = executor.execute()
    assert stats2["skipped_count"] == 1
    assert stats2["executed_count"] == 0

    # Advance clock by 2 hours
    clock.advance(hours=2)
    rail.set_fixture("TXN_TRANSIENT", True) # Second attempt succeeds

    # 3rd run after time advance: should execute 2nd attempt and succeed
    stats3 = executor.execute()
    assert stats3["executed_count"] == 1
    assert stats3["success_count"] == 1

    p_final = repo.get_payment_by_id("TXN_TRANSIENT")
    assert p_final.executed_retry_count == 2
    assert p_final.is_terminal is True
    assert p_final.attempts[-1].outcome == Outcome.SUCCESS


# ---------------------------------------------------------------------------
# T9.4 Hardening Tests
# ---------------------------------------------------------------------------

class _ExplodingRail(PaymentRailPort):
    """Test-local stub: always raises an unexpected exception."""

    def execute_attempt(self, txn_id, amount, action_type, attempt_number):
        raise RuntimeError("Simulated gateway timeout")


class _ExplodingFirstRail(PaymentRailPort):
    """Test-local stub: raises for txn_id 'TXN_A', succeeds for everything else."""

    def execute_attempt(self, txn_id, amount, action_type, attempt_number):
        if txn_id == "TXN_A":
            raise RuntimeError("Simulated crash for TXN_A")
        return RailResponse(success=True, gateway_reference="GW_OK")


@pytest.fixture
def fresh_env():
    """Minimal isolated DB env reusable by T9.4 tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    repo = SQLiteFailedPaymentRepository(session_factory=lambda: session)
    clock = SimulatedClock(datetime(2026, 1, 1, 10, 0, 0))
    yield session, repo, clock
    session.close()


def _make_payment(txn_id, root_cause_label, recoverable_flag=True, **kwargs):
    defaults = dict(
        customer_id="CUST_X",
        amount=500.0,
        currency="INR",
        failure_code="GENERIC",
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card",
    )
    defaults.update(kwargs)
    return FailedPaymentModel(
        txn_id=txn_id,
        root_cause_label=root_cause_label,
        recoverable_flag=recoverable_flag,
        **defaults,
    )


# T9.4.1 — Rail exception containment ----------------------------------------

def test_rail_exception_does_not_crash_batch(fresh_env):
    """An unexpected exception from the payment rail must not propagate out of execute()."""
    session, repo, clock = fresh_env
    session.add(_make_payment("TXN_CRASH", "Transient/Network"))
    session.commit()

    executor = ExecuteRecoveryBatch(repo, _ExplodingRail(), clock)
    stats = executor.execute()

    assert stats["executed_count"] == 1
    assert stats["failed_count"] == 1
    assert stats["success_count"] == 0


def test_rail_exception_records_failed_attempt(fresh_env):
    """Rail exception must persist a FAILED RecoveryAttempt with the exception message."""
    session, repo, clock = fresh_env
    session.add(_make_payment("TXN_CRASH2", "Transient/Network"))
    session.commit()

    executor = ExecuteRecoveryBatch(repo, _ExplodingRail(), clock)
    executor.execute()

    p = repo.get_payment_by_id("TXN_CRASH2")
    assert len(p.attempts) == 1
    assert p.attempts[0].outcome == Outcome.FAILED
    assert "Unhandled Rail Exception" in (p.attempts[0].reason or "")


def test_rail_exception_emits_audit_event(fresh_env):
    """Rail exception outcome must appear in the Phase 3 audit trail."""
    session, repo, clock = fresh_env
    session.add(_make_payment("TXN_CRASH3", "Transient/Network"))
    session.commit()

    executor = ExecuteRecoveryBatch(repo, _ExplodingRail(), clock)
    executor.execute()

    events = executor.audit_repo.all_events()
    assert len(events) == 1
    assert events[0].outcome == AuditOutcome.FAILED
    assert events[0].reason_code == ReasonCode.RAIL_DECLINED


# T9.4.2 — Batch continuation ------------------------------------------------

def test_batch_continues_after_rail_crash_on_first_record(fresh_env):
    """If record N raises an exception on the rail, record N+1 must still be processed."""
    session, repo, clock = fresh_env
    session.add(_make_payment("TXN_A", "Transient/Network"))
    session.add(_make_payment("TXN_B", "Transient/Network"))
    session.commit()

    executor = ExecuteRecoveryBatch(repo, _ExplodingFirstRail(), clock)
    stats = executor.execute()

    assert stats["executed_count"] == 2
    assert stats["failed_count"] == 1
    assert stats["success_count"] == 1
    assert stats["failed_count"] + stats["success_count"] == stats["executed_count"]

    p_a = repo.get_payment_by_id("TXN_A")
    p_b = repo.get_payment_by_id("TXN_B")
    assert p_a.attempts[0].outcome == Outcome.FAILED
    assert p_b.attempts[0].outcome == Outcome.SUCCESS

    # Both records leave audit evidence
    events = executor.audit_repo.all_events()
    audited_txns = {e.txn_id for e in events}
    assert "TXN_A" in audited_txns
    assert "TXN_B" in audited_txns


# T9.4.3 — Unknown failure code audit regression -----------------------------

def test_unknown_failure_code_handled_safely(fresh_env):
    """Unknown root_cause_label must be escalated safely without crashing."""
    session, repo, clock = fresh_env
    # recoverable_flag=True means it is NOT hard fraud — it is genuinely unknown
    session.add(_make_payment("TXN_UNK", "Totally Unknown Category", recoverable_flag=True))
    session.commit()

    executor = ExecuteRecoveryBatch(repo, MockPaymentRail(), clock)
    stats = executor.execute()

    assert stats["escalated_count"] == 1
    assert stats["executed_count"] == 0


def test_unknown_failure_code_emits_audit_event(fresh_env):
    """Unknown failure codes routed to hard-stop must emit a Phase 3 audit event."""
    session, repo, clock = fresh_env
    session.add(_make_payment("TXN_UNK2", "Totally Unknown Category", recoverable_flag=True))
    session.commit()

    executor = ExecuteRecoveryBatch(repo, MockPaymentRail(), clock)
    executor.execute()

    events = executor.audit_repo.all_events()
    assert len(events) == 1
    evt = events[0]
    assert evt.txn_id == "TXN_UNK2"
    assert evt.outcome == AuditOutcome.ESCALATED
    assert evt.action == ActionType.REFUSE
    assert evt.reason_code == ReasonCode.STOPPING_RULE_TRIP


def test_unknown_failure_code_does_not_alter_hard_fraud_semantics(fresh_env):
    """Hard fraud (recoverable_flag=False) must still use DO_NOT_RETRY, not STOPPING_RULE_TRIP."""
    session, repo, clock = fresh_env
    session.add(_make_payment(
        "TXN_FRAUD2", "Hard Fraud / Do-Not-Retry", recoverable_flag=False
    ))
    session.commit()

    executor = ExecuteRecoveryBatch(repo, MockPaymentRail(), clock)
    executor.execute()

    events = executor.audit_repo.all_events()
    assert len(events) == 1
    assert events[0].reason_code == ReasonCode.DO_NOT_RETRY


# T9.4.4 — Empty batch -------------------------------------------------------

def test_empty_batch_does_not_raise(fresh_env):
    """An empty repository must return zero-valued statistics without raising."""
    _, repo, clock = fresh_env
    # No payments inserted

    executor = ExecuteRecoveryBatch(repo, MockPaymentRail(), clock)
    stats = executor.execute()

    assert stats["total_processed"] == 0
    assert stats["executed_count"] == 0
    assert stats["success_count"] == 0
    assert stats["failed_count"] == 0
    assert stats["skipped_count"] == 0
    assert stats["escalated_count"] == 0
