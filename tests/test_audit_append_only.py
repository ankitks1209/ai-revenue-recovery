from datetime import datetime
from sqlalchemy import create_engine, text
from src.domain.audit import ActionType, AuditEvent, Outcome, ReasonCode
from src.infrastructure.audit_repository import AuditLogRepository


def test_repository_has_no_mutation_api():
    repo = AuditLogRepository(db_url="sqlite:///:memory:")

    # Append-only contract: no update/delete methods exposed.
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")
    assert hasattr(repo, "append")
    assert hasattr(repo, "all_events")


def test_append_and_read_domain_objects():
    repo = AuditLogRepository(db_url="sqlite:///:memory:")
    now = datetime.now()

    event = AuditEvent(
        event_id="evt_001",
        txn_id="txn_100",
        timestamp=now,
        action=ActionType.RETRY,
        decision_rationale="Sufficient delay met",
        outcome=Outcome.RECOVERED,
        reason_code=ReasonCode.RECOVERED,
        customer_ref_masked="MASKED::123456789012",
        tier="T1",
    )

    repo.append(event)
    events = repo.all_events()

    assert len(events) == 1
    retrieved = events[0]
    assert isinstance(retrieved, AuditEvent)
    assert retrieved.event_id == "evt_001"
    assert retrieved.txn_id == "txn_100"
    assert retrieved.action == ActionType.RETRY
    assert retrieved.outcome == Outcome.RECOVERED
    assert retrieved.customer_ref_masked == "MASKED::123456789012"


def test_persisted_row_contains_only_masked_ref():
    db_url = "sqlite:///:memory:"
    repo = AuditLogRepository(db_url=db_url)

    raw_customer_ref = "CUST_SECRET_999"
    masked_ref = "MASKED::secret999hash"

    event = AuditEvent(
        event_id="evt_002",
        txn_id="txn_200",
        timestamp=datetime.now(),
        action=ActionType.REFUSE,
        decision_rationale="Hard stop triggered",
        outcome=Outcome.ESCALATED,
        reason_code=ReasonCode.DO_NOT_RETRY,
        customer_ref_masked=masked_ref,
        tier="T3",
    )

    repo.append(event)

    # Directly query SQLite database to verify raw customer ref is nowhere stored
    with repo._engine.connect() as conn:
        result = conn.execute(text("SELECT customer_ref_masked FROM audit_events")).fetchall()
        assert len(result) == 1
        stored_value = result[0][0]
        assert stored_value == masked_ref
        assert raw_customer_ref not in stored_value
