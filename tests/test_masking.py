from datetime import datetime
import pytest
from pydantic import ValidationError
from src.domain.audit import ActionType, AuditEvent, Outcome, ReasonCode
from src.domain.masking import MaskingPolicy


def test_audit_event_immutability():
    event = AuditEvent(
        event_id="evt_123",
        txn_id="txn_456",
        timestamp=datetime.now(),
        action=ActionType.RETRY,
        decision_rationale="Retry bounds met",
        outcome=Outcome.RECOVERED,
        reason_code=ReasonCode.RECOVERED,
        customer_ref_masked="MASKED::abc123def456",
        tier="T1",
    )
    with pytest.raises(ValidationError):
        event.tier = "T2"


def test_customer_ref_is_masked_and_deterministic():
    m = MaskingPolicy()
    raw = "9876543210"
    masked = m.mask_customer_ref(raw)
    assert raw not in masked
    assert masked.startswith("MASKED::")
    assert len(masked) == 20  # "MASKED::" (8) + 12 hex chars = 20
    assert masked == m.mask_customer_ref(raw)


def test_empty_customer_ref_returns_masked_empty():
    m = MaskingPolicy()
    assert m.mask_customer_ref("") == "MASKED::empty"
    assert m.mask_customer_ref(None) == "MASKED::empty"


def test_scrub_text_strips_phone_and_email():
    m = MaskingPolicy()
    scrubbed = m.scrub_text("call 9876543210 or mail a@b.com")
    assert "9876543210" not in scrubbed
    assert "a@b.com" not in scrubbed
    assert "MASKED::num" in scrubbed
    assert "MASKED::email" in scrubbed
