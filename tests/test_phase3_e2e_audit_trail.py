"""
Phase 3 Final End-to-End Verification Test

Proves Phase 3 audit trail works end-to-end with real components:
- ExecuteRecoveryBatch emits audit events for all money actions
- AuditLogRepository persists masked events append-only
- GenerateAuditReport reconciles with batch execution stats
- Graceful failure (hard fraud + max retries) demonstrated
- No raw customer_id in any output
"""

import json
from datetime import datetime, timedelta
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
from src.application.generate_audit_report import GenerateAuditReport
from src.domain.models import Outcome
from src.domain.audit import ActionType, Outcome as AuditOutcome, ReasonCode
from src.domain.entities import RecoveryAttempt


@pytest.fixture
def phase3_e2e_env():
    """Complete Phase 3 E2E environment with audit infrastructure."""
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


def test_phase3_complete_audit_trail_e2e(phase3_e2e_env):
    """
    Phase 3 E2E: Seed → Execute → Audit → Report → Reconcile

    Scenario: 4 payments covering all money-action paths
    1. Hard fraud (recoverable_flag=False) → REFUSE/ESCALATED/T3
    2. Mandate lapse at max retries → REFUSE/ESCALATED/STOPPING_RULE_TRIP/T3
    3. Transient/Network success → RETRY/RECOVERED/T1
    4. Insufficient Funds failure → RETRY/FAILED/T2 (RAIL_DECLINED → T2)

    Verifies:
    - All 4 payments emit exactly 1 audit event each
    - Batch stats reconcile with audit report
    - Graceful failure demonstrated (hard fraud + max retries: zero rail calls)
    - No raw customer_id in audit events or report
    - JSON-compatible output
    """
    session, repo, rail, clock, audit_repo, logger = phase3_e2e_env

    # ============================================================
    # 1. SEED 4 PAYMENTS COVERING ALL AUDIT PATHS
    # ============================================================

    # Payment 1: Hard fraud - graceful refusal
    db_p1 = FailedPaymentModel(
        txn_id="TXN_FRAUD",
        customer_id="CUST_FRAUD_001",
        amount=1000.0,
        currency="INR",
        failure_code="FRAUD",
        root_cause_label="Hard Fraud / Do-Not-Retry",
        recoverable_flag=False,  # Hard fraud marker
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )

    # Payment 2: Mandate lapse already at max retries - graceful refusal
    db_p2 = FailedPaymentModel(
        txn_id="TXN_MAXRETRIES",
        customer_id="CUST_MANDATE_002",
        amount=500.0,
        currency="INR",
        failure_code="MANDATE_FAIL",
        root_cause_label="Mandate Lapse",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="mandate"
    )

    # Payment 3: Transient/Network - will succeed
    db_p3 = FailedPaymentModel(
        txn_id="TXN_SUCCESS",
        customer_id="CUST_TRANSIENT_003",
        amount=750.0,
        currency="INR",
        failure_code="TIMEOUT",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="upi"
    )

    # Payment 4: Insufficient Funds - will fail
    db_p4 = FailedPaymentModel(
        txn_id="TXN_FAILED",
        customer_id="CUST_INSUFF_004",
        amount=300.0,
        currency="INR",
        failure_code="51",
        root_cause_label="Insufficient Funds",
        recoverable_flag=True,
        retry_count=0,
        timestamp=datetime(2026, 1, 1, 8, 0, 0),
        payment_method="card"
    )

    session.add_all([db_p1, db_p2, db_p3, db_p4])
    session.commit()

    # Pre-populate mandate lapse with 1 attempt to reach max_retries=1
    # This triggers _handle_max_retries path instead of _execute_rail_attempt
    mandate_attempt = RecoveryAttempt(
        txn_id="TXN_MAXRETRIES",
        attempt_number=1,
        outcome=Outcome.FAILED,
        reason="Mandate failed",
        timestamp=datetime(2026, 1, 1, 9, 0, 0)
    )
    repo.save_attempt(mandate_attempt)

    # Configure payment rail outcomes
    rail.set_fixture("TXN_SUCCESS", True)   # Force success
    rail.set_fixture("TXN_FAILED", False)   # Force failure

    # Track rail calls to verify graceful failures don't hit rail
    rail_calls = []
    original_execute = rail.execute_attempt
    def tracked_execute(txn_id, amount, action_type, attempt_number):
        rail_calls.append(txn_id)
        return original_execute(txn_id, amount, action_type, attempt_number)
    rail.execute_attempt = tracked_execute

    # ============================================================
    # 2. EXECUTE BATCH WITH AUDIT INFRASTRUCTURE
    # ============================================================

    executor = ExecuteRecoveryBatch(
        repository=repo,
        payment_rail=rail,
        clock=clock,
        audit_repository=audit_repo,
        structured_logger=logger
    )

    batch_stats = executor.execute()

    # ============================================================
    # 3. VERIFY BATCH EXECUTION STATS (Phase 2 behavior preserved)
    # ============================================================

    assert batch_stats["total_processed"] == 4
    assert batch_stats["executed_count"] == 2  # Only TXN_SUCCESS and TXN_FAILED hit rail
    assert batch_stats["success_count"] == 1   # TXN_SUCCESS
    assert batch_stats["failed_count"] == 1    # TXN_FAILED
    assert batch_stats["escalated_count"] == 2 # TXN_FRAUD and TXN_MAXRETRIES

    # ============================================================
    # 4. VERIFY GRACEFUL FAILURE: ZERO RAIL CALLS FOR REFUSALS
    # ============================================================

    # Hard fraud and max retries should NOT call payment rail
    assert "TXN_FRAUD" not in rail_calls
    assert "TXN_MAXRETRIES" not in rail_calls
    # Only successful and failed retries should call rail
    assert "TXN_SUCCESS" in rail_calls
    assert "TXN_FAILED" in rail_calls
    assert len(rail_calls) == 2

    # ============================================================
    # 5. VERIFY AUDIT EVENTS EMITTED (Phase 3 audit trail)
    # ============================================================

    audit_events = audit_repo.all_events()

    # Exactly 4 events (one per payment)
    assert len(audit_events) == 4

    # Extract events by transaction
    events_by_txn = {e.txn_id: e for e in audit_events}
    assert set(events_by_txn.keys()) == {"TXN_FRAUD", "TXN_MAXRETRIES", "TXN_SUCCESS", "TXN_FAILED"}

    # Verify hard fraud event
    fraud_event = events_by_txn["TXN_FRAUD"]
    assert fraud_event.action == ActionType.REFUSE
    assert fraud_event.outcome == AuditOutcome.ESCALATED
    assert fraud_event.reason_code == ReasonCode.DO_NOT_RETRY
    assert fraud_event.tier == "T3"
    assert "CUST_FRAUD_001" not in fraud_event.customer_ref_masked
    assert fraud_event.customer_ref_masked.startswith("MASKED::")

    # Verify max retries event
    maxret_event = events_by_txn["TXN_MAXRETRIES"]
    assert maxret_event.action == ActionType.REFUSE
    assert maxret_event.outcome == AuditOutcome.ESCALATED
    assert maxret_event.reason_code == ReasonCode.STOPPING_RULE_TRIP
    assert maxret_event.tier == "T3"
    assert "CUST_MANDATE_002" not in maxret_event.customer_ref_masked
    assert maxret_event.customer_ref_masked.startswith("MASKED::")

    # Verify success event
    success_event = events_by_txn["TXN_SUCCESS"]
    assert success_event.action == ActionType.RETRY
    assert success_event.outcome == AuditOutcome.RECOVERED
    assert success_event.reason_code == ReasonCode.RECOVERED
    assert success_event.tier == "T1"
    assert "CUST_TRANSIENT_003" not in success_event.customer_ref_masked
    assert success_event.customer_ref_masked.startswith("MASKED::")

    # Verify failure event
    failed_event = events_by_txn["TXN_FAILED"]
    assert failed_event.action == ActionType.RETRY
    assert failed_event.outcome == AuditOutcome.FAILED
    assert failed_event.reason_code == ReasonCode.RAIL_DECLINED
    assert failed_event.tier == "T2"  # RAIL_DECLINED gets T2 per escalation policy
    assert "CUST_INSUFF_004" not in failed_event.customer_ref_masked
    assert failed_event.customer_ref_masked.startswith("MASKED::")

    # ============================================================
    # 6. GENERATE AUDIT REPORT (Phase 3 Day 8)
    # ============================================================

    report_gen = GenerateAuditReport(audit_repository=audit_repo)
    report = report_gen.run()

    # Verify report structure
    assert "audit_trail" in report
    assert "escalation_summary" in report

    audit_trail = report["audit_trail"]
    escalation_summary = report["escalation_summary"]

    # ============================================================
    # 7. RECONCILE BATCH STATS WITH AUDIT REPORT
    # ============================================================

    # A. Event count reconciliation
    assert len(audit_trail) == batch_stats["total_processed"]  # 4 == 4
    assert escalation_summary["total_events"] == 4

    # B. Escalation count reconciliation
    assert batch_stats["escalated_count"] == escalation_summary["total_escalated_count"]  # 2 == 2
    assert escalation_summary["refusal_count"] == 2  # Both escalations were refusals

    # C. Tier distribution reconciliation
    tier_counts = escalation_summary["tier_counts"]
    assert tier_counts["T1"] == 1  # TXN_SUCCESS only
    assert tier_counts["T2"] == 1  # TXN_FAILED (RAIL_DECLINED → T2)
    assert tier_counts["T3"] == 2  # TXN_FRAUD, TXN_MAXRETRIES

    # D. Action/Outcome mapping verification
    trail_by_txn = {e["txn_id"]: e for e in audit_trail}

    # Hard fraud trail entry
    fraud_trail = trail_by_txn["TXN_FRAUD"]
    assert fraud_trail["action"] == "refuse"
    assert fraud_trail["outcome"] == "escalated"
    assert fraud_trail["reason_code"] == "do_not_retry"
    assert fraud_trail["tier"] == "T3"

    # Max retries trail entry
    maxret_trail = trail_by_txn["TXN_MAXRETRIES"]
    assert maxret_trail["action"] == "refuse"
    assert maxret_trail["outcome"] == "escalated"
    assert maxret_trail["reason_code"] == "stopping_rule_trip"
    assert maxret_trail["tier"] == "T3"

    # Success trail entry
    success_trail = trail_by_txn["TXN_SUCCESS"]
    assert success_trail["action"] == "retry"
    assert success_trail["outcome"] == "recovered"
    assert success_trail["reason_code"] == "recovered"
    assert success_trail["tier"] == "T1"

    # Failure trail entry
    failed_trail = trail_by_txn["TXN_FAILED"]
    assert failed_trail["action"] == "retry"
    assert failed_trail["outcome"] == "failed"
    assert failed_trail["reason_code"] == "rail_declined"
    assert failed_trail["tier"] == "T2"  # RAIL_DECLINED gets T2 per escalation policy

    # ============================================================
    # 8. VERIFY MASKING PRESERVATION IN REPORT
    # ============================================================

    for trail_entry in audit_trail:
        # No raw customer_id appears anywhere
        assert "CUST_" not in trail_entry["customer_ref_masked"]
        assert "CUST_FRAUD" not in str(trail_entry)
        assert "CUST_MANDATE" not in str(trail_entry)
        assert "CUST_TRANSIENT" not in str(trail_entry)
        assert "CUST_INSUFF" not in str(trail_entry)

        # All customer refs are masked
        assert trail_entry["customer_ref_masked"].startswith("MASKED::")

    # ============================================================
    # 9. VERIFY JSON COMPATIBILITY
    # ============================================================

    # Report must be JSON-serializable
    report_json = json.dumps(report)
    assert report_json is not None

    # Timestamps are ISO strings
    for entry in audit_trail:
        assert isinstance(entry["timestamp"], str)
        assert "T" in entry["timestamp"]  # ISO 8601 format
        assert entry["timestamp"].startswith("2026-01-01T")

    # Enums are string values (not enum objects)
    for entry in audit_trail:
        assert isinstance(entry["action"], str)
        assert isinstance(entry["outcome"], str)
        assert isinstance(entry["reason_code"], str)
        assert entry["action"] in ["retry", "refuse"]
        assert entry["outcome"] in ["recovered", "failed", "escalated"]

    # ============================================================
    # 10. VERIFY PHASE 2 BEHAVIOR PRESERVED
    # ============================================================

    # Check actual payment records match batch stats
    p_fraud = repo.get_payment_by_id("TXN_FRAUD")
    assert p_fraud.executed_retry_count == 0  # No rail execution
    assert len(p_fraud.escalations) == 1

    p_maxret = repo.get_payment_by_id("TXN_MAXRETRIES")
    assert p_maxret.executed_retry_count == 1  # Pre-seeded attempt only
    assert len(p_maxret.escalations) == 1

    p_success = repo.get_payment_by_id("TXN_SUCCESS")
    assert p_success.executed_retry_count == 1
    assert any(att.outcome == Outcome.SUCCESS for att in p_success.attempts)

    p_failed = repo.get_payment_by_id("TXN_FAILED")
    assert p_failed.executed_retry_count == 1
    assert any(att.outcome == Outcome.FAILED for att in p_failed.attempts)

    # ============================================================
    # PHASE 3 E2E VERIFICATION COMPLETE ✓
    # ============================================================

    # Summary of what was proven:
    # ✓ All 4 money-action paths emit exactly 1 audit event
    # ✓ Batch stats reconcile with audit report
    # ✓ Graceful failure (hard fraud + max retries) demonstrated
    # ✓ Zero rail calls for refusals
    # ✓ Masking preserved throughout (no raw customer_id)
    # ✓ JSON-compatible output
    # ✓ Phase 2 behavior unchanged
