"""
Phase 3 Day 8 — GenerateAuditReport Tests

Comprehensive test coverage for audit reporting use case:
- Empty audit log handling
- Single and multiple event scenarios
- Tier distribution aggregation
- Refusal counting (graceful failures)
- Escalation counting
- Time filtering (since parameter)
- Masking preservation
- JSON-compatible output
- No mutation of repository/events
"""

from datetime import datetime
import pytest

from src.infrastructure.audit_repository import AuditLogRepository
from src.application.generate_audit_report import GenerateAuditReport
from src.domain.audit import ActionType, AuditEvent, Outcome, ReasonCode


@pytest.fixture
def empty_audit_repo():
    """Empty in-memory audit repository."""
    return AuditLogRepository(db_url="sqlite:///:memory:")


@pytest.fixture
def populated_audit_repo():
    """In-memory audit repository with sample events."""
    repo = AuditLogRepository(db_url="sqlite:///:memory:")

    # Event 1: T1 automated retry - success
    repo.append(AuditEvent(
        event_id="evt_001",
        txn_id="TXN_001",
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        action=ActionType.RETRY,
        decision_rationale="Sufficient delay met; automated retry within bounds.",
        outcome=Outcome.RECOVERED,
        reason_code=ReasonCode.RECOVERED,
        customer_ref_masked="MASKED::abc123",
        tier="T1"
    ))

    # Event 2: T2 dunning - failed
    repo.append(AuditEvent(
        event_id="evt_002",
        txn_id="TXN_002",
        timestamp=datetime(2026, 1, 1, 11, 0, 0),
        action=ActionType.DUNNING,
        decision_rationale="Max retries reached; dunning notification sent.",
        outcome=Outcome.FAILED,
        reason_code=ReasonCode.RETRIES_EXHAUSTED,
        customer_ref_masked="MASKED::def456",
        tier="T2"
    ))

    # Event 3: T3 hard fraud refusal - escalated
    repo.append(AuditEvent(
        event_id="evt_003",
        txn_id="TXN_003",
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        action=ActionType.REFUSE,
        decision_rationale="Hard-fraud / do-not-retry code: agent refuses automated action.",
        outcome=Outcome.ESCALATED,
        reason_code=ReasonCode.DO_NOT_RETRY,
        customer_ref_masked="MASKED::ghi789",
        tier="T3"
    ))

    # Event 4: T3 stopping rule refusal - escalated
    repo.append(AuditEvent(
        event_id="evt_004",
        txn_id="TXN_004",
        timestamp=datetime(2026, 1, 1, 13, 0, 0),
        action=ActionType.REFUSE,
        decision_rationale="Stopping rule tripped: refuse + escalate.",
        outcome=Outcome.ESCALATED,
        reason_code=ReasonCode.STOPPING_RULE_TRIP,
        customer_ref_masked="MASKED::jkl012",
        tier="T3"
    ))

    # Event 5: T1 retry - skipped
    repo.append(AuditEvent(
        event_id="evt_005",
        txn_id="TXN_005",
        timestamp=datetime(2026, 1, 1, 14, 0, 0),
        action=ActionType.RETRY,
        decision_rationale="Backoff interval not met; skipped this cycle.",
        outcome=Outcome.SKIPPED,
        reason_code=ReasonCode.RECOVERED,
        customer_ref_masked="MASKED::mno345",
        tier="T1"
    ))

    return repo


def test_empty_audit_log_returns_empty_trail_and_zero_counts(empty_audit_repo):
    """Empty audit log → empty trail, zero summary counts."""
    report_gen = GenerateAuditReport(audit_repository=empty_audit_repo)
    result = report_gen.run()

    assert result["audit_trail"] == []
    assert result["escalation_summary"]["tier_counts"]["T1"] == 0
    assert result["escalation_summary"]["tier_counts"]["T2"] == 0
    assert result["escalation_summary"]["tier_counts"]["T3"] == 0
    assert result["escalation_summary"]["refusal_count"] == 0
    assert result["escalation_summary"]["total_escalated_count"] == 0
    assert result["escalation_summary"]["total_events"] == 0


def test_single_event_appears_in_trail_with_correct_summary(empty_audit_repo):
    """Single event → trail with 1 entry, correct summary counts."""
    empty_audit_repo.append(AuditEvent(
        event_id="evt_single",
        txn_id="TXN_SINGLE",
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        action=ActionType.RETRY,
        decision_rationale="First retry attempt.",
        outcome=Outcome.RECOVERED,
        reason_code=ReasonCode.RECOVERED,
        customer_ref_masked="MASKED::single123",
        tier="T1"
    ))

    report_gen = GenerateAuditReport(audit_repository=empty_audit_repo)
    result = report_gen.run()

    assert len(result["audit_trail"]) == 1
    entry = result["audit_trail"][0]
    assert entry["event_id"] == "evt_single"
    assert entry["txn_id"] == "TXN_SINGLE"
    assert entry["action"] == "retry"
    assert entry["outcome"] == "recovered"
    assert entry["tier"] == "T1"

    assert result["escalation_summary"]["tier_counts"]["T1"] == 1
    assert result["escalation_summary"]["total_events"] == 1


def test_multiple_events_chronological_order_preserved(populated_audit_repo):
    """Multiple events → chronological order preserved, correct aggregation."""
    report_gen = GenerateAuditReport(audit_repository=populated_audit_repo)
    result = report_gen.run()

    assert len(result["audit_trail"]) == 5

    # Verify chronological order
    timestamps = [entry["timestamp"] for entry in result["audit_trail"]]
    assert timestamps == sorted(timestamps)

    # Verify first and last entries
    assert result["audit_trail"][0]["event_id"] == "evt_001"
    assert result["audit_trail"][-1]["event_id"] == "evt_005"


def test_tier_distribution_correct(populated_audit_repo):
    """Tier distribution → T1/T2/T3 counts match event tiers."""
    report_gen = GenerateAuditReport(audit_repository=populated_audit_repo)
    result = report_gen.run()

    tier_counts = result["escalation_summary"]["tier_counts"]
    assert tier_counts["T1"] == 2  # evt_001, evt_005
    assert tier_counts["T2"] == 1  # evt_002
    assert tier_counts["T3"] == 2  # evt_003, evt_004


def test_refusal_counting(populated_audit_repo):
    """Refusal counting → count REFUSE actions (graceful failures)."""
    report_gen = GenerateAuditReport(audit_repository=populated_audit_repo)
    result = report_gen.run()

    # evt_003 and evt_004 are REFUSE actions
    assert result["escalation_summary"]["refusal_count"] == 2


def test_escalation_counting(populated_audit_repo):
    """Escalation counting → count ESCALATED outcomes."""
    report_gen = GenerateAuditReport(audit_repository=populated_audit_repo)
    result = report_gen.run()

    # evt_003 and evt_004 have ESCALATED outcomes
    assert result["escalation_summary"]["total_escalated_count"] == 2


def test_time_filtering_with_since_parameter(populated_audit_repo):
    """Time filtering → since parameter filters correctly."""
    report_gen = GenerateAuditReport(audit_repository=populated_audit_repo)

    # Filter to events at/after 2026-01-01 12:00:00
    result = report_gen.run(since=datetime(2026, 1, 1, 12, 0, 0))

    assert len(result["audit_trail"]) == 3  # evt_003, evt_004, evt_005
    assert result["audit_trail"][0]["event_id"] == "evt_003"
    assert result["escalation_summary"]["total_events"] == 3


def test_masking_preservation_no_raw_customer_id(populated_audit_repo):
    """Masking preservation → no raw customer_id in output."""
    report_gen = GenerateAuditReport(audit_repository=populated_audit_repo)
    result = report_gen.run()

    for entry in result["audit_trail"]:
        # All customer refs must be masked
        assert entry["customer_ref_masked"].startswith("MASKED::")
        # Verify specific masked values preserved exactly
        assert "CUST_" not in entry["customer_ref_masked"]
        assert entry["customer_ref_masked"] in [
            "MASKED::abc123",
            "MASKED::def456",
            "MASKED::ghi789",
            "MASKED::jkl012",
            "MASKED::mno345"
        ]


def test_json_compatible_output_structure(populated_audit_repo):
    """JSON-compatible output → ISO timestamps, enum string values."""
    report_gen = GenerateAuditReport(audit_repository=populated_audit_repo)
    result = report_gen.run()

    entry = result["audit_trail"][0]

    # Timestamp is ISO 8601 string
    assert isinstance(entry["timestamp"], str)
    assert entry["timestamp"] == "2026-01-01T10:00:00"

    # Enums are string values
    assert isinstance(entry["action"], str)
    assert isinstance(entry["outcome"], str)
    assert isinstance(entry["reason_code"], str)

    # All string values (no enum objects)
    assert entry["action"] in ["retry", "dunning", "re-auth", "refuse"]
    assert entry["outcome"] in ["recovered", "failed", "skipped", "escalated"]


def test_no_mutation_of_repository_or_events(populated_audit_repo):
    """No mutation → repository/events unchanged after report generation."""
    report_gen = GenerateAuditReport(audit_repository=populated_audit_repo)

    # Generate report twice
    result1 = report_gen.run()
    result2 = report_gen.run()

    # Results should be identical
    assert result1 == result2
    assert len(result1["audit_trail"]) == len(result2["audit_trail"])

    # Verify repository still has same events
    events = populated_audit_repo.all_events()
    assert len(events) == 5


def test_multiple_events_per_txn_counted_independently():
    """Multiple events per txn → each event counted independently."""
    repo = AuditLogRepository(db_url="sqlite:///:memory:")

    # Two events for same transaction
    repo.append(AuditEvent(
        event_id="evt_a1",
        txn_id="TXN_SHARED",
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        action=ActionType.RETRY,
        decision_rationale="First attempt.",
        outcome=Outcome.FAILED,
        reason_code=ReasonCode.RAIL_DECLINED,
        customer_ref_masked="MASKED::shared",
        tier="T1"
    ))

    repo.append(AuditEvent(
        event_id="evt_a2",
        txn_id="TXN_SHARED",
        timestamp=datetime(2026, 1, 1, 11, 0, 0),
        action=ActionType.RETRY,
        decision_rationale="Second attempt.",
        outcome=Outcome.RECOVERED,
        reason_code=ReasonCode.RECOVERED,
        customer_ref_masked="MASKED::shared",
        tier="T1"
    ))

    report_gen = GenerateAuditReport(audit_repository=repo)
    result = report_gen.run()

    # Both events appear in trail
    assert len(result["audit_trail"]) == 2
    assert result["audit_trail"][0]["event_id"] == "evt_a1"
    assert result["audit_trail"][1]["event_id"] == "evt_a2"

    # Both counted in summary
    assert result["escalation_summary"]["total_events"] == 2
    assert result["escalation_summary"]["tier_counts"]["T1"] == 2


def test_all_audit_trail_fields_present():
    """All required audit trail fields present in output."""
    repo = AuditLogRepository(db_url="sqlite:///:memory:")
    repo.append(AuditEvent(
        event_id="evt_fields",
        txn_id="TXN_FIELDS",
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        action=ActionType.REAUTH,
        decision_rationale="Re-authentication required.",
        outcome=Outcome.ESCALATED,
        reason_code=ReasonCode.RETRIES_EXHAUSTED,
        customer_ref_masked="MASKED::fields",
        tier="T2"
    ))

    report_gen = GenerateAuditReport(audit_repository=repo)
    result = report_gen.run()

    entry = result["audit_trail"][0]
    required_fields = [
        "event_id", "txn_id", "timestamp", "action", "outcome",
        "reason_code", "tier", "customer_ref_masked", "decision_rationale"
    ]

    for field in required_fields:
        assert field in entry, f"Missing required field: {field}"
        assert entry[field] is not None, f"Field {field} is None"


def test_escalation_summary_structure():
    """Escalation summary has all required fields with correct structure."""
    repo = AuditLogRepository(db_url="sqlite:///:memory:")
    report_gen = GenerateAuditReport(audit_repository=repo)
    result = report_gen.run()

    summary = result["escalation_summary"]

    # Verify structure
    assert "tier_counts" in summary
    assert "refusal_count" in summary
    assert "total_escalated_count" in summary
    assert "total_events" in summary

    # Verify tier_counts structure
    assert "T1" in summary["tier_counts"]
    assert "T2" in summary["tier_counts"]
    assert "T3" in summary["tier_counts"]

    # All counts are integers
    assert isinstance(summary["tier_counts"]["T1"], int)
    assert isinstance(summary["tier_counts"]["T2"], int)
    assert isinstance(summary["tier_counts"]["T3"], int)
    assert isinstance(summary["refusal_count"], int)
    assert isinstance(summary["total_escalated_count"], int)
    assert isinstance(summary["total_events"], int)
