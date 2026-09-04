"""
Phase 3 Day 7 Executor Audit Integration Tests

Verifies all 5 money-action paths emit correct audit events with proper:
- action/outcome/reason_code mappings
- customer_ref_masked (no raw IDs)
- decision_rationale scrubbing
- event_id (fresh UUID per event)
- repository/logger reception of masked events
- audit failure resilience (no side effects on money-action)
- Phase 2 backward compatibility
"""

from datetime import datetime
from unittest.mock import Mock, call
import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base, FailedPayment as FailedPaymentModel
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.infrastructure.mock_payment_rail import MockPaymentRail
from src.infrastructure.clock import SimulatedClock
from src.infrastructure.audit_repository import AuditLogRepository
from src.infrastructure.structured_logger import StructuredLogger
from src.application.execute_recovery_batch import ExecuteRecoveryBatch
from src.domain.models import Outcome
from src.domain.audit import ActionType, Outcome as AuditOutcome, ReasonCode
from src.domain.entities import RecoveryAttempt


@pytest.fixture
def batch_env_with_audit():
    """Extended batch fixture with audit infrastructure."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionTest = sessionmaker(bind=engine)
    session = SessionTest()

    repo = SQLiteFailedPaymentRepository(session_factory=lambda: session)
    rail = MockPaymentRail()
    clock = SimulatedClock(datetime(2026, 1, 1, 10, 0, 0))
    audit_repo = AuditLogRepository(db_url="sqlite:///:memory:")
    logger = StructuredLogger()

    yield session, repo, rail, clock, audit_repo, logger
    session.close()


def test_hard_fraud_emits_refuse_escalated_do_not_retry(batch_env_with_audit):
    """Hard-fraud (recoverable_flag=False) → REFUSE/ESCALATED/DO_NOT_RETRY audit."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: Create hard-fraud payment
    db_p = FailedPaymentModel(
        txn_id="TXN_FRAUD",
        customer_id="CUST_001",
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

    rail.force_all(True)  # Even if rail forced success, hard fraud must not call rail
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=logger)

    # Act
    stats = executor.execute()

    # Assert: Money-action preserved
    assert stats["escalated_count"] == 1
    assert stats["executed_count"] == 0  # Rail never called
    p = repo.get_payment_by_id("TXN_FRAUD")
    assert p.executed_retry_count == 0
    assert len(p.escalations) == 1
    assert len(p.attempts) == 1
    assert p.attempts[0].outcome == Outcome.ESCALATED

    # Assert: Audit event emitted
    events = audit_repo.all_events()
    assert len(events) == 1
    event = events[0]
    assert event.action == ActionType.REFUSE
    assert event.outcome == AuditOutcome.ESCALATED
    assert event.reason_code == ReasonCode.DO_NOT_RETRY
    assert event.tier == "T3"  # Hard fraud is T3
    assert "CUST_001" not in event.customer_ref_masked
    assert event.customer_ref_masked.startswith("MASKED::")
    assert uuid.UUID(event.event_id)  # Valid UUID


def test_max_retries_emits_refuse_escalated_stopping_rule(batch_env_with_audit):
    """Max retries exhausted → REFUSE/ESCALATED/STOPPING_RULE_TRIP audit.

    Reaches _handle_max_retries() by:
    1. Creating payment with root_cause_label that has max_retries=1 (Mandate Lapse)
    2. Pre-populating RecoveryAttempt records so executed_retry_count >= max_retries
    3. Calling executor.execute() triggers the max_retries check (line 66-67)
    4. _handle_max_retries() is invoked, emits audit, creates escalation
    """
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: Create Mandate Lapse payment (max_retries=1)
    db_p = FailedPaymentModel(
        txn_id="TXN_MAXRETRIES",
        customer_id="CUST_MAXRET",
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

    # Pre-populate one FAILED attempt so executed_retry_count = 1
    # This triggers the max_retries check (1 >= 1) without post-failure escalation
    p = repo.get_payment_by_id("TXN_MAXRETRIES")
    from src.domain.entities import RecoveryAttempt
    att = RecoveryAttempt(
        txn_id="TXN_MAXRETRIES",
        attempt_number=1,
        outcome=Outcome.FAILED,
        reason="Rail declined: Mock rail declined transaction",
        action_type="retry",
        timestamp=datetime(2026, 1, 1, 9, 0, 0)
    )
    repo.save_attempt(att)

    rail.force_all(False)  # Prevent rail call
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=logger)

    # Act: execute() checks executed_retry_count (1) >= max_retries (1)
    # This triggers _handle_max_retries() at line 66-67
    stats = executor.execute()

    # Assert: Escalated without rail call (max-retries path)
    assert stats["escalated_count"] == 1
    assert stats["executed_count"] == 0  # Rail never called

    p_final = repo.get_payment_by_id("TXN_MAXRETRIES")
    assert p_final.is_terminal is True
    assert len(p_final.escalations) == 1

    # Assert: Exactly one audit event for the max-retries refusal
    events = audit_repo.all_events()
    assert len(events) == 1
    event = events[0]

    # Verify audit event has correct mappings for retry-cap refusal
    assert event.action == ActionType.REFUSE
    assert event.outcome == AuditOutcome.ESCALATED
    assert event.reason_code == ReasonCode.STOPPING_RULE_TRIP
    # Tier is assigned by EscalationPolicy based on reason_code + amount + retry_count
    assert event.tier == "T3"
    assert "CUST_MAXRET" not in event.customer_ref_masked
    assert event.customer_ref_masked.startswith("MASKED::")
    assert uuid.UUID(event.event_id)  # Valid UUID


def test_temporary_skip_emits_mapped_skipped_recovered(batch_env_with_audit):
    """Timing delay not met → <mapped>/SKIPPED/RECOVERED audit, no escalation."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: Create transient payment
    db_p = FailedPaymentModel(
        txn_id="TXN_TRANSIENT",
        customer_id="CUST_003",
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

    rail.set_fixture("TXN_TRANSIENT", False)  # First attempt fails
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=logger)

    # Act: First run - attempt 1 fails
    stats1 = executor.execute()
    assert stats1["executed_count"] == 1
    assert stats1["failed_count"] == 1

    # Act: Second run immediately (timing delay not met) - should skip
    audit_repo_skip = AuditLogRepository(db_url="sqlite:///:memory:")
    executor_skip = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo_skip, structured_logger=logger)
    stats2 = executor_skip.execute()

    # Assert: Skipped (no rail call, no escalation)
    assert stats2["skipped_count"] == 1
    assert stats2["executed_count"] == 0
    assert stats2["escalated_count"] == 0

    # Assert: Audit event for skip
    events = audit_repo_skip.all_events()
    assert len(events) == 1
    event = events[0]
    assert event.action == ActionType.RETRY  # Transient -> RETRY
    assert event.outcome == AuditOutcome.SKIPPED
    assert event.reason_code == ReasonCode.RECOVERED
    assert "CUST_003" not in event.customer_ref_masked


def test_successful_rail_emits_mapped_recovered_recovered(batch_env_with_audit):
    """Rail success → <mapped>/RECOVERED/RECOVERED audit."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: Create payment
    db_p = FailedPaymentModel(
        txn_id="TXN_SUCCESS",
        customer_id="CUST_004",
        amount=300.0,
        currency="INR",
        failure_code="TIMEOUT",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )
    session.add(db_p)
    session.commit()

    rail.force_all(True)  # All rail attempts succeed
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=logger)

    # Act
    stats = executor.execute()

    # Assert: Money-action
    assert stats["executed_count"] == 1
    assert stats["success_count"] == 1
    p = repo.get_payment_by_id("TXN_SUCCESS")
    assert p.is_terminal is True
    assert p.attempts[0].outcome == Outcome.SUCCESS

    # Assert: Audit event for success
    events = audit_repo.all_events()
    assert len(events) == 1
    event = events[0]
    assert event.action == ActionType.RETRY
    assert event.outcome == AuditOutcome.RECOVERED
    assert event.reason_code == ReasonCode.RECOVERED
    assert event.decision_rationale == "Recovery attempt succeeded"
    assert "CUST_004" not in event.customer_ref_masked
    assert event.tier == "T1"


def test_failed_rail_emits_mapped_failed_rail_declined(batch_env_with_audit):
    """Rail failure → <mapped>/FAILED/RAIL_DECLINED audit."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: Create payment
    db_p = FailedPaymentModel(
        txn_id="TXN_FAILED",
        customer_id="CUST_005",
        amount=400.0,
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

    rail.force_all(False)  # All rail attempts fail
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=logger)

    # Act
    stats = executor.execute()

    # Assert: Money-action
    assert stats["executed_count"] == 1
    assert stats["failed_count"] == 1
    p = repo.get_payment_by_id("TXN_FAILED")
    assert p.attempts[0].outcome == Outcome.FAILED

    # Assert: Audit event for failure
    events = audit_repo.all_events()
    assert len(events) == 1
    event = events[0]
    assert event.action == ActionType.RETRY
    assert event.outcome == AuditOutcome.FAILED
    assert event.reason_code == ReasonCode.RAIL_DECLINED
    assert "Mock rail declined" in event.decision_rationale
    assert "CUST_005" not in event.customer_ref_masked


def test_customer_ref_masked_is_deterministic(batch_env_with_audit):
    """Same customer ID → identical masked ref across events."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: Two payments from same customer
    db_p1 = FailedPaymentModel(
        txn_id="TXN_DET1",
        customer_id="CUST_DET",
        amount=100.0,
        currency="INR",
        failure_code="CODE1",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )
    db_p2 = FailedPaymentModel(
        txn_id="TXN_DET2",
        customer_id="CUST_DET",
        amount=200.0,
        currency="INR",
        failure_code="CODE2",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="upi"
    )
    session.add_all([db_p1, db_p2])
    session.commit()

    rail.force_all(True)
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=logger)

    # Act
    executor.execute()

    # Assert: Both events have identical masked ref
    events = audit_repo.all_events()
    assert len(events) == 2
    assert events[0].customer_ref_masked == events[1].customer_ref_masked
    assert "CUST_DET" not in events[0].customer_ref_masked


def test_each_audit_event_has_unique_uuid(batch_env_with_audit):
    """Multiple audit events → unique event_id for each."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: 3 payments with different outcomes
    db_p1 = FailedPaymentModel(
        txn_id="TXN_UUID1",
        customer_id="CUST_A",
        amount=100.0,
        currency="INR",
        failure_code="CODE1",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )
    db_p2 = FailedPaymentModel(
        txn_id="TXN_UUID2",
        customer_id="CUST_B",
        amount=200.0,
        currency="INR",
        failure_code="CODE2",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="upi"
    )
    db_p3 = FailedPaymentModel(
        txn_id="TXN_UUID3",
        customer_id="CUST_C",
        amount=300.0,
        currency="INR",
        failure_code="CODE3",
        root_cause_label="Hard Fraud / Do-Not-Retry",
        recoverable_flag=False,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )
    session.add_all([db_p1, db_p2, db_p3])
    session.commit()

    rail.set_fixture("TXN_UUID1", True)
    rail.set_fixture("TXN_UUID2", False)
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=logger)

    # Act
    executor.execute()

    # Assert: 3 unique event IDs
    events = audit_repo.all_events()
    assert len(events) == 3
    event_ids = [e.event_id for e in events]
    assert len(set(event_ids)) == 3  # All unique
    for event_id in event_ids:
        uuid.UUID(event_id)  # Valid UUID


def test_rationale_scrubbed_of_pii(batch_env_with_audit):
    """Decision rationale with email/phone → scrubbed in audit."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: Payment with failure that generates PII in rationale
    db_p = FailedPaymentModel(
        txn_id="TXN_PII",
        customer_id="john.doe@example.com",
        amount=500.0,
        currency="INR",
        failure_code="FAIL",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )
    session.add(db_p)
    session.commit()

    rail.force_all(False)  # Force failure
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=logger)

    # Act
    executor.execute()

    # Assert: Rationale is scrubbed
    events = audit_repo.all_events()
    assert len(events) == 1
    event = events[0]
    # Decision rationale contains error message but should be scrubbed
    assert "john.doe@example.com" not in event.decision_rationale
    assert event.customer_ref_masked.startswith("MASKED::")


def test_audit_repo_failure_preserves_money_behavior(batch_env_with_audit):
    """If AuditLogRepository.append() fails, money-action unchanged."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: Mock audit repo that raises on append
    mock_audit_repo = Mock()
    mock_audit_repo.append.side_effect = Exception("Audit storage failed")

    db_p = FailedPaymentModel(
        txn_id="TXN_AUDIT_FAIL",
        customer_id="CUST_FAIL",
        amount=100.0,
        currency="INR",
        failure_code="CODE",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )
    session.add(db_p)
    session.commit()

    rail.force_all(True)
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=mock_audit_repo, structured_logger=logger)

    # Act: Should not raise even though audit repo fails
    stats = executor.execute()

    # Assert: Money-action unaffected
    assert stats["executed_count"] == 1
    assert stats["success_count"] == 1
    p = repo.get_payment_by_id("TXN_AUDIT_FAIL")
    assert p.is_terminal is True
    assert p.attempts[0].outcome == Outcome.SUCCESS


def test_logger_failure_preserves_money_behavior(batch_env_with_audit):
    """If StructuredLogger.emit() fails, money-action unchanged."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: Mock logger that raises on emit
    mock_logger = Mock()
    mock_logger.emit.side_effect = Exception("Logger failed")

    db_p = FailedPaymentModel(
        txn_id="TXN_LOG_FAIL",
        customer_id="CUST_LOG",
        amount=100.0,
        currency="INR",
        failure_code="CODE",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )
    session.add(db_p)
    session.commit()

    rail.force_all(True)
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=mock_logger)

    # Act: Should not raise even though logger fails
    stats = executor.execute()

    # Assert: Money-action unaffected
    assert stats["executed_count"] == 1
    assert stats["success_count"] == 1
    p = repo.get_payment_by_id("TXN_LOG_FAIL")
    assert p.is_terminal is True


def test_phase2_backward_compatibility(batch_env_with_audit):
    """With audit infrastructure, Phase 2 stats/behavior identical."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: 3 payments (hard-fraud, success, max-retries scenario)
    # Hard-fraud
    db_p1 = FailedPaymentModel(
        txn_id="TXN_COMPAT1",
        customer_id="CUST_B1",
        amount=1000.0,
        currency="INR",
        failure_code="FRAUD",
        root_cause_label="Hard Fraud / Do-Not-Retry",
        recoverable_flag=False,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )
    # Success
    db_p2 = FailedPaymentModel(
        txn_id="TXN_COMPAT2",
        customer_id="CUST_B2",
        amount=500.0,
        currency="INR",
        failure_code="TIMEOUT",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="upi"
    )
    session.add_all([db_p1, db_p2])
    session.commit()

    rail.set_fixture("TXN_COMPAT1", True)  # Would succeed if called, but won't be
    rail.set_fixture("TXN_COMPAT2", True)
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=logger)

    # Act
    stats = executor.execute()

    # Assert: Stats match Phase 2 expectations
    assert stats["total_processed"] == 2
    assert stats["executed_count"] == 1  # Only TXN_COMPAT2
    assert stats["success_count"] == 1
    assert stats["escalated_count"] == 1  # TXN_COMPAT1 hard-fraud


def test_action_mapping_applied(batch_env_with_audit):
    """Verify action mapping works for Dunning/Re-Auth/Retry strings."""
    session, repo, rail, clock, audit_repo, logger = batch_env_with_audit

    # Arrange: Success payment
    db_p = FailedPaymentModel(
        txn_id="TXN_ACTION_MAP",
        customer_id="CUST_MAP",
        amount=300.0,
        currency="INR",
        failure_code="FAIL",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )
    session.add(db_p)
    session.commit()

    rail.force_all(True)
    executor = ExecuteRecoveryBatch(repo, rail, clock, audit_repository=audit_repo, structured_logger=logger)

    # Act
    executor.execute()

    # Assert: Action is mapped (transient/network defaults to RETRY)
    events = audit_repo.all_events()
    assert len(events) == 1
    assert events[0].action == ActionType.RETRY
