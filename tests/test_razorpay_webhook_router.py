"""P5.3 — Router transport tests. Verifies HTTP semantics via FastAPI TestClient."""

import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.application.ingest_razorpay_event import IngestRazorpayEvent
from src.infrastructure.razorpay.webhook_verifier import RazorpayWebhookVerifier
from src.infrastructure.razorpay.webhook_event_repository import InMemoryWebhookEventRepository
from src.api.razorpay_webhook_router import create_razorpay_webhook_router


SECRET = "router_test_secret"


def _client_and_verifier():
    verifier = RazorpayWebhookVerifier(SECRET)
    repo = InMemoryWebhookEventRepository()
    ingest = IngestRazorpayEvent(verifier, repo)
    app = FastAPI()
    app.include_router(create_razorpay_webhook_router(ingest))
    client = TestClient(app)
    return client, verifier, repo


def _payment_failed_body():
    return json.dumps({
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": {
            "id": "pay_router1",
            "amount": 50000,
            "currency": "INR",
            "status": "failed",
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "fail",
            "method": "card",
            "created_at": 1700000000,
        }}},
        "created_at": 1700000000,
    }).encode()


def test_valid_new_event_returns_200_ingested():
    client, verifier, _ = _client_and_verifier()
    raw = _payment_failed_body()
    sig = verifier.compute(raw)
    resp = client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_router_1"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ingested"


def test_duplicate_event_returns_200_duplicate():
    client, verifier, _ = _client_and_verifier()
    raw = _payment_failed_body()
    sig = verifier.compute(raw)
    headers = {"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_router_dup"}
    client.post("/webhooks/razorpay", content=raw, headers=headers)
    resp = client.post("/webhooks/razorpay", content=raw, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate"


def test_invalid_signature_returns_401():
    client, _, _ = _client_and_verifier()
    raw = _payment_failed_body()
    resp = client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"x-razorpay-signature": "bad", "x-razorpay-event-id": "evt_bad_sig"},
    )
    assert resp.status_code == 401


def test_missing_signature_returns_401():
    client, _, _ = _client_and_verifier()
    raw = _payment_failed_body()
    resp = client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"x-razorpay-event-id": "evt_no_sig"},
    )
    assert resp.status_code == 401


def test_missing_event_id_returns_400():
    client, verifier, _ = _client_and_verifier()
    raw = _payment_failed_body()
    sig = verifier.compute(raw)
    resp = client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"x-razorpay-signature": sig},
    )
    assert resp.status_code == 400


def test_malformed_payload_returns_400():
    client, verifier, _ = _client_and_verifier()
    raw = b"not json"
    sig = verifier.compute(raw)
    resp = client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_malformed"},
    )
    assert resp.status_code == 400


def test_unsupported_event_returns_200_ignored():
    client, verifier, _ = _client_and_verifier()
    raw = json.dumps({"entity": "event", "event": "payment.captured", "payload": {}}).encode()
    sig = verifier.compute(raw)
    resp = client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_ignored"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_router_does_not_use_body_event_id_fallback():
    # Body contains event_id but header missing -> 400
    client, verifier, _ = _client_and_verifier()
    payload = json.loads(_payment_failed_body())
    payload["event_id"] = "evt_body_fallback"
    raw = json.dumps(payload).encode()
    sig = verifier.compute(raw)
    resp = client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"x-razorpay-signature": sig},
    )
    assert resp.status_code == 400


def test_router_uses_raw_body_not_parsed_json():
    import pathlib
    src = pathlib.Path("src/api/razorpay_webhook_router.py").read_text()
    assert "await request.body()" in src
