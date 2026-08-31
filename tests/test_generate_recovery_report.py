from datetime import datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.database import Base, FailedPayment as FailedPaymentModel
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.application.generate_recovery_report import GenerateRecoveryReport
from src.domain.entities import RecoveryAttempt, Escalation
from src.domain.models import Outcome

@pytest.fixture
def report_repo():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionTest = sessionmaker(bind=engine)
    session = SessionTest()
    repo = SQLiteFailedPaymentRepository(session_factory=lambda: session)
    yield session, repo
    session.close()

def test_report_zero_denominator_safety(report_repo):
    session, repo = report_repo
    p = FailedPaymentModel(
        txn_id="TXN_UNREC",
        customer_id="C1",
        amount=500.0,
        currency="INR",
        failure_code="FRAUD",
        root_cause_label="Hard Fraud / Do-Not-Retry",
        recoverable_flag=False,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        payment_method="card"
    )
    session.add(p)
    session.commit()

    report_gen = GenerateRecoveryReport(repository=repo)
    data = report_gen.generate_report()

    assert data["recoverable_denominator"] == 0.0
    assert data["money_recovered"] == 0.0
    assert data["recovery_rate"] == 0.0

def test_report_money_recovered_deduplication(report_repo):
    session, repo = report_repo
    p = FailedPaymentModel(
        txn_id="TXN_MULTI_SUCCESS",
        customer_id="C2",
        amount=1000.0,
        currency="INR",
        failure_code="TIMEOUT",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=2,
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        payment_method="upi"
    )
    session.add(p)
    session.commit()

    att1 = RecoveryAttempt(txn_id="TXN_MULTI_SUCCESS", attempt_number=1, outcome=Outcome.SUCCESS, timestamp=datetime(2026, 1, 1, 11, 0, 0))
    att2 = RecoveryAttempt(txn_id="TXN_MULTI_SUCCESS", attempt_number=2, outcome=Outcome.SUCCESS, timestamp=datetime(2026, 1, 1, 12, 0, 0))
    repo.save_attempt(att1)
    repo.save_attempt(att2)

    report_gen = GenerateRecoveryReport(repository=repo)
    data = report_gen.generate_report()

    assert data["money_recovered"] == 1000.0
    assert data["recoverable_denominator"] == 1000.0
    assert data["recovery_rate"] == 100.0



def test_report_executed_attempts_filtering(report_repo):
    session, repo = report_repo
    p = FailedPaymentModel(
        txn_id="TXN_ATTEMPTS",
        customer_id="C3",
        amount=300.0,
        currency="INR",
        failure_code="ERR",
        root_cause_label="Insufficient Funds",
        recoverable_flag=True,
        retry_count=1,
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        payment_method="card"
    )
    session.add(p)
    session.commit()

    att_failed = RecoveryAttempt(txn_id="TXN_ATTEMPTS", attempt_number=1, outcome=Outcome.FAILED, reason="Rail failure", timestamp=datetime(2026, 1, 1, 11, 0, 0))
    att_skipped = RecoveryAttempt(txn_id="TXN_ATTEMPTS", attempt_number=2, outcome=Outcome.SKIPPED, reason="Backoff hold", timestamp=datetime(2026, 1, 1, 12, 0, 0))
    att_escalated = RecoveryAttempt(txn_id="TXN_ATTEMPTS", attempt_number=3, outcome=Outcome.ESCALATED, reason="Policy stop", timestamp=datetime(2026, 1, 1, 13, 0, 0))
    repo.save_attempt(att_failed)
    repo.save_attempt(att_skipped)
    repo.save_attempt(att_escalated)

    report_gen = GenerateRecoveryReport(repository=repo)
    data = report_gen.generate_report()

    breakdown = data["intervention_breakdown"]["Insufficient Funds"]
    assert breakdown["executed_attempts"] == 1  # FAILED only; SKIPPED and ESCALATED excluded

def test_report_unique_escalation_count(report_repo):
    session, repo = report_repo
    p = FailedPaymentModel(
        txn_id="TXN_ESC",
        customer_id="C4",
        amount=400.0,
        currency="INR",
        failure_code="MANDATE",
        root_cause_label="Mandate Lapse",
        recoverable_flag=True,
        retry_count=1,
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        payment_method="mandate"
    )
    session.add(p)
    session.commit()

    esc1 = Escalation(txn_id="TXN_ESC", reason="First escalation", timestamp=datetime(2026, 1, 1, 11, 0, 0))
    esc2 = Escalation(txn_id="TXN_ESC", reason="Second escalation", timestamp=datetime(2026, 1, 1, 12, 0, 0))
    repo.save_escalation(esc1)
    repo.save_escalation(esc2)

    report_gen = GenerateRecoveryReport(repository=repo)
    data = report_gen.generate_report()

    assert data["escalation_count"] == 1  # Unique txn_id

def test_report_exception_list_contents(report_repo):
    session, repo = report_repo
    p_success = FailedPaymentModel(
        txn_id="TXN_OK", customer_id="C5", amount=100.0, currency="INR", failure_code="A",
        root_cause_label="Transient/Network", recoverable_flag=True, retry_count=1, timestamp=datetime(2026, 1, 1, 10, 0, 0), payment_method="card"
    )
    p_failed = FailedPaymentModel(
        txn_id="TXN_FAIL", customer_id="C6", amount=200.0, currency="INR", failure_code="B",
        root_cause_label="Transient/Network", recoverable_flag=True, retry_count=1, timestamp=datetime(2026, 1, 1, 10, 0, 0), payment_method="card"
    )
    session.add_all([p_success, p_failed])
    session.commit()

    repo.save_attempt(RecoveryAttempt(txn_id="TXN_OK", attempt_number=1, outcome=Outcome.SUCCESS, timestamp=datetime(2026, 1, 1, 11, 0, 0)))
    repo.save_attempt(RecoveryAttempt(txn_id="TXN_FAIL", attempt_number=1, outcome=Outcome.FAILED, reason="Declined by gateway", timestamp=datetime(2026, 1, 1, 11, 0, 0)))

    report_gen = GenerateRecoveryReport(repository=repo)
    data = report_gen.generate_report()

    exc_txns = [e["txn_id"] for e in data["exception_list"]]
    assert "TXN_OK" not in exc_txns
    assert "TXN_FAIL" in exc_txns

    formatted = report_gen.format_cli_report(data)
    assert "AI REVENUE RECOVERY REPORT" in formatted
    assert "TXN_FAIL" in formatted
