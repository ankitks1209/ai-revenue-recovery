from datetime import datetime
import structlog
from src.domain.audit import ActionType, AuditEvent, Outcome, ReasonCode
from src.infrastructure.structured_logger import StructuredLogger


def test_structured_logger_emits_masked_data():
    logger = StructuredLogger()

    event = AuditEvent(
        event_id="evt_003",
        txn_id="txn_300",
        timestamp=datetime.now(),
        action=ActionType.DUNNING,
        decision_rationale="Expired card notification",
        outcome=Outcome.FAILED,
        reason_code=ReasonCode.RAIL_DECLINED,
        customer_ref_masked="MASKED::abcdef123456",
        tier="T2",
    )

    raw_customer_id = "USER_SECRET_ID_123"

    with structlog.testing.capture_logs() as captured:
        logger.emit(event)

    assert len(captured) == 1
    log_entry = captured[0]
    assert log_entry["event"] == "money_action"
    assert log_entry["event_id"] == "evt_003"
    assert log_entry["txn_id"] == "txn_300"
    assert log_entry["customer_ref"] == "MASKED::abcdef123456"
    assert raw_customer_id not in str(log_entry)
