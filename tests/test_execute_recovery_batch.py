from datetime import datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.database import Base, FailedPayment as FailedPaymentModel
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.infrastructure.mock_payment_rail import MockPaymentRail
from src.infrastructure.clock import SimulatedClock
from src.application.execute_recovery_batch import ExecuteRecoveryBatch
from src.domain.models import Outcome

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
