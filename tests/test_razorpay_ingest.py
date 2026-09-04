"""P5.3 — Ingest application tests. No HTTP, no real DB."""

import json
import pytest
from src.domain.webhook_events import WebhookIngestStatus, InvalidSignatureError, MissingEventIdError, MalformedPayloadError
from src.application.ingest_razorpay_event import IngestRazorpayEvent
from src.infrastructure.razorpay.webhook_verifier import RazorpayWebhookVerifier
from src.infrastructure.razorpay.webhook_event_repository import InMemoryWebhookEventRepository


SECRET = "test_webhook_secret_123"


def _make_ingest(repo=None):
    verifier = RazorpayWebhookVerifier(SECRET)
    repo = repo or InMemoryWebhookEventRepository()
    return IngestRazorpayEvent(verifier, repo), verifier, repo


def _payment_failed_raw(payment_id="pay_abc123", amount_paise=10000):
    payload = {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount_paise,
                    "currency": "INR",
                    "status": "failed",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "declined",
                    "method": "card",
                    "created_at": 1700000000,
                }
            }
        },
        "created_at": 1700000000,
    }
    return json.dumps(payload).encode()


def test_ingest_new_payment_failed_returns_ingested():
    ingest, verifier, repo = _make_ingest()
    raw = _payment_failed_raw()
    sig = verifier.compute(raw)
    result = ingest.ingest(raw_body=raw, signature=sig, event_id="evt_001")
    assert result.status == WebhookIngestStatus.INGESTED
    assert result.event_id == "evt_001"
    assert result.payment_id == "pay_abc123"


def test_ingest_duplicate_returns_duplicate():
    ingest, verifier, repo = _make_ingest()
    raw = _payment_failed_raw()
    sig = verifier.compute(raw)
    r1 = ingest.ingest(raw_body=raw, signature=sig, event_id="evt_dup")
    r2 = ingest.ingest(raw_body=raw, signature=sig, event_id="evt_dup")
    assert r1.status == WebhookIngestStatus.INGESTED
    assert r2.status == WebhookIngestStatus.DUPLICATE


def test_replay_same_event_twice_is_idempotent():
    ingest, verifier, _ = _make_ingest()
    raw = _payment_failed_raw(payment_id="pay_replay")
    sig = verifier.compute(raw)
    for _ in range(3):
        result = ingest.ingest(raw_body=raw, signature=sig, event_id="evt_replay")
    assert result.status == WebhookIngestStatus.DUPLICATE
    # only one stored (InMemory)
    assert ingest._repo.all_event_ids().count("evt_replay") == 1


def test_ingest_invalid_signature_raises():
    ingest, _, _ = _make_ingest()
    raw = _payment_failed_raw()
    with pytest.raises(InvalidSignatureError):
        ingest.ingest(raw_body=raw, signature="bad", event_id="evt_bad_sig")
    # not stored
    assert not ingest._repo.exists("evt_bad_sig")


def test_ingest_missing_event_id_raises():
    ingest, verifier, _ = _make_ingest()
    raw = _payment_failed_raw()
    sig = verifier.compute(raw)
    for bad_id in [None, "", "   "]:
        with pytest.raises(MissingEventIdError):
            ingest.ingest(raw_body=raw, signature=sig, event_id=bad_id)


def test_missing_event_id_does_not_fallback_to_body():
    ingest, verifier, repo = _make_ingest()
    payload = json.loads(_payment_failed_raw())
    payload["event_id"] = "evt_from_body"
    raw = json.dumps(payload).encode()
    sig = verifier.compute(raw)
    with pytest.raises(MissingEventIdError):
        ingest.ingest(raw_body=raw, signature=sig, event_id=None)
    assert not repo.exists("evt_from_body")


def test_never_marks_seen_before_signature_fails():
    ingest, _, repo = _make_ingest()
    raw = _payment_failed_raw()
    with pytest.raises(InvalidSignatureError):
        ingest.ingest(raw_body=raw, signature="invalid", event_id="evt_not_seen")
    assert not repo.exists("evt_not_seen")


def test_never_marks_seen_before_malformed_payload():
    ingest, verifier, repo = _make_ingest()
    raw = b"not json"
    sig = verifier.compute(raw)
    with pytest.raises(MalformedPayloadError):
        ingest.ingest(raw_body=raw, signature=sig, event_id="evt_malformed")
    assert not repo.exists("evt_malformed")


def test_unsupported_event_returns_ignored():
    ingest, verifier, repo = _make_ingest()
    raw = json.dumps({"entity": "event", "event": "payment.captured", "payload": {}}).encode()
    sig = verifier.compute(raw)
    result = ingest.ingest(raw_body=raw, signature=sig, event_id="evt_unsupported")
    assert result.status == WebhookIngestStatus.IGNORED
    assert repo.exists("evt_unsupported")


def test_unsupported_event_duplicate_returns_duplicate():
    ingest, verifier, _ = _make_ingest()
    raw = json.dumps({"entity": "event", "event": "order.paid", "payload": {}}).encode()
    sig = verifier.compute(raw)
    r1 = ingest.ingest(raw_body=raw, signature=sig, event_id="evt_unsup_dup")
    r2 = ingest.ingest(raw_body=raw, signature=sig, event_id="evt_unsup_dup")
    assert r1.status == WebhookIngestStatus.IGNORED
    assert r2.status == WebhookIngestStatus.DUPLICATE


def test_malformed_payment_failed_raises():
    ingest, verifier, repo = _make_ingest()
    # payment.failed missing payment entity id
    payload = {
        "entity": "event",
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"amount": 1000}}},
    }
    raw = json.dumps(payload).encode()
    sig = verifier.compute(raw)
    with pytest.raises(MalformedPayloadError):
        ingest.ingest(raw_body=raw, signature=sig, event_id="evt_malformed2")
    assert not repo.exists("evt_malformed2")


def test_ingest_does_not_import_fastapi_or_request():
    import pathlib
    src = pathlib.Path("src/application/ingest_razorpay_event.py").read_text()
    assert "fastapi" not in src.lower()
    assert "Request" not in src or "from fastapi" not in src


def test_canonical_header_only_no_fallback_hash():
    import pathlib
    src = pathlib.Path("src/application/ingest_razorpay_event.py").read_text()
    assert "hash" not in src.lower() or "hashlib" not in src.lower()
