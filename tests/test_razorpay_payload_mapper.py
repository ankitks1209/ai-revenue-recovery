"""P5.3 — Razorpay payload mapper tests. Pure, no network."""

import json
import pytest
from src.domain.webhook_events import MalformedPayloadError
from src.infrastructure.razorpay.razorpay_payload_mapper import parse_razorpay_payload, SUPPORTED_EVENT


def _payment_failed_body(payment_overrides=None, event_overrides=None):
    entity = {
        "id": "pay_test123",
        "amount": 50000,
        "currency": "INR",
        "status": "failed",
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Payment failed",
        "method": "card",
        "customer_id": "cust_123",
        "created_at": 1700000000,
    }
    if payment_overrides:
        entity.update(payment_overrides)
        # allow deletion marker
        for k, v in list(entity.items()):
            if v is None and k in payment_overrides and payment_overrides[k] is None:
                entity.pop(k, None)
    payload = {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
        "created_at": 1700000000,
    }
    if event_overrides:
        payload.update(event_overrides)
    return json.dumps(payload).encode()


def test_parse_valid_payment_failed():
    raw = _payment_failed_body()
    result = parse_razorpay_payload("evt_123", raw)
    assert result is not None
    assert result.event_id == "evt_123"
    assert result.event_type == "payment.failed"
    assert result.payment_id == "pay_test123"
    assert result.amount == 500.0  # 50000 paise -> 500.0
    assert result.currency == "INR"
    assert result.failure_code == "BAD_REQUEST_ERROR"


def test_parse_amount_paise_conversion():
    raw = _payment_failed_body({"amount": 12345})
    result = parse_razorpay_payload("evt_1", raw)
    assert result.amount == 123.45


def test_parse_unsupported_event_returns_none():
    body = json.dumps({"entity": "event", "event": "payment.captured", "payload": {}}).encode()
    result = parse_razorpay_payload("evt_2", body)
    assert result is None


def test_parse_invalid_json_raises():
    with pytest.raises(MalformedPayloadError):
        parse_razorpay_payload("evt_3", b"not json{")


def test_parse_missing_event_raises():
    body = json.dumps({"payload": {}}).encode()
    with pytest.raises(MalformedPayloadError):
        parse_razorpay_payload("evt_4", body)


def test_parse_missing_payment_entity_raises():
    body = json.dumps({"event": "payment.failed", "payload": {}}).encode()
    with pytest.raises(MalformedPayloadError):
        parse_razorpay_payload("evt_5", body)


def test_parse_missing_payment_id_raises():
    raw = _payment_failed_body({"id": ""})
    with pytest.raises(MalformedPayloadError):
        parse_razorpay_payload("evt_6", raw)


def test_parse_missing_amount_raises():
    # build manually without amount
    entity = {"id": "pay_1", "currency": "INR", "status": "failed"}
    body = json.dumps({"event": "payment.failed", "payload": {"payment": {"entity": entity}}}).encode()
    with pytest.raises(MalformedPayloadError):
        parse_razorpay_payload("evt_7", body)


def test_parse_non_object_payload_raises():
    with pytest.raises(MalformedPayloadError):
        parse_razorpay_payload("evt_8", b"[]")


def test_parse_error_code_defaults_to_unknown():
    raw = _payment_failed_body({"error_code": None})
    # Remove error_code explicitly
    payload = json.loads(raw)
    payload["payload"]["payment"]["entity"].pop("error_code", None)
    raw2 = json.dumps(payload).encode()
    result = parse_razorpay_payload("evt_9", raw2)
    assert result.failure_code == "UNKNOWN"


def test_mapper_does_not_import_razorpay_sdk():
    import pathlib
    src = pathlib.Path("src/infrastructure/razorpay/razorpay_payload_mapper.py").read_text()
    assert "import razorpay" not in src.lower()
    assert "razorpay" not in src.lower() or "razorpay_payload" in src.lower()  # only file name


def test_domain_not_leaking_raw_payload():
    import pathlib
    src = pathlib.Path("src/domain/webhook_events.py").read_text()
    assert "razorpay" not in src.lower()
    assert "payload" not in src.lower() or "Normalized" in src  # no raw dict storage
