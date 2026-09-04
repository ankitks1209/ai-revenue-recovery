from datetime import datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.database import Base, FailedPayment as FailedPaymentModel
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.domain.entities import RecoveryAttempt, Escalation
from src.domain.models import Outcome

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionTest = sessionmaker(bind=engine)
    session = SessionTest()
    try:
        yield session
    finally:
        session.close()

def test_repository_save_and_load(db_session):
    # Insert test failed payment
    db_p = FailedPaymentModel(
        txn_id="TXN_REPO_1",
        customer_id="CUST_1",
        amount=500.0,
        currency="INR",
        failure_code="ERR",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        payment_method="card"
    )
    db_session.add(db_p)
    db_session.commit()

    repo = SQLiteFailedPaymentRepository(session_factory=lambda: db_session)

    payments = repo.get_all_payments()
    assert len(payments) == 1
    p = payments[0]
    assert p.txn_id == "TXN_REPO_1"
    assert p.executed_retry_count == 0

    # Save a FAILED attempt
    att = RecoveryAttempt(
        txn_id="TXN_REPO_1",
        attempt_number=1,
        outcome=Outcome.FAILED,
        timestamp=datetime(2026, 1, 1, 11, 0, 0),
        reason="Network timeout",
        action_type="retry"
    )
    repo.save_attempt(att)

    # Reload and check retry count updated
    p_reloaded = repo.get_payment_by_id("TXN_REPO_1")
    assert p_reloaded.executed_retry_count == 1
    assert len(p_reloaded.attempts) == 1
    assert p_reloaded.attempts[0].outcome == Outcome.FAILED

    # Save an escalation
    esc = Escalation(
        txn_id="TXN_REPO_1",
        reason="Max retries exhausted",
        timestamp=datetime(2026, 1, 1, 12, 0, 0)
    )
    repo.save_escalation(esc)

    p_final = repo.get_payment_by_id("TXN_REPO_1")
    assert len(p_final.escalations) == 1
    assert p_final.is_terminal is True
