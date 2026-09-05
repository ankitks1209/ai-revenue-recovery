"""M6.2 — FastAPI webhook app dispatch tests. No live network."""
import json
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.app import create_app
from src.database import Base, FailedPayment, RecoveryLifecycleModel, RecoveryAttemptModel, OperatorAuditModel
from src.domain.recovery_lifecycle import RecoveryState
from src.infrastructure.razorpay.webhook_verifier import RazorpayWebhookVerifier
from src.infrastructure.razorpay.webhook_event_repository import InMemoryWebhookEventRepository
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.infrastructure.recovery_lifecycle_repository import RecoveryLifecycleRepository
from src.infrastructure.razorpay.razorpay_recovery_rail import _reference_id

SECRET = "m6_2_test_secret_abc123"


def _make_env():
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionTest = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    verifier = RazorpayWebhookVerifier(SECRET)
    webhook_repo = InMemoryWebhookEventRepository()
    payment_repo = SQLiteFailedPaymentRepository(session_factory=SessionTest)
    lifecycle_repo = RecoveryLifecycleRepository(session_factory=SessionTest)
    app = create_app(
        webhook_secret=SECRET,
        session_factory=SessionTest,
        webhook_repo=webhook_repo,
        payment_repo=payment_repo,
        lifecycle_repo=lifecycle_repo,
        verifier=verifier,
    )
    client = TestClient(app)
    return engine, SessionTest, verifier, webhook_repo, payment_repo, lifecycle_repo, app, client


def _add_payment(SessionTest, txn_id="txn_api_1", customer_id="CUST_API_1", amount=1000.0):
    with SessionTest() as s:
        s.add(FailedPayment(
            txn_id=txn_id, customer_id=customer_id, amount=amount, currency="INR",
            failure_code="insufficient_funds", root_cause_label="Insufficient Funds",
            recoverable_flag=True, retry_count=0, timestamp=datetime.utcnow(),
            payment_method="card"
        ))
        s.commit()


def _set_lifecycle(SessionTest, txn_id, state: RecoveryState):
    with SessionTest() as s:
        row = s.get(RecoveryLifecycleModel, txn_id)
        if row:
            row.state = state.value
            row.updated_at = datetime.utcnow()
        else:
            s.add(RecoveryLifecycleModel(txn_id=txn_id, state=state.value, updated_at=datetime.utcnow(), reason=None, version=0))
        s.commit()


def _get_lifecycle(SessionTest, txn_id):
    with SessionTest() as s:
        row = s.get(RecoveryLifecycleModel, txn_id)
        return RecoveryState(row.state) if row else None


def _count_attempts(SessionTest, txn_id):
    with SessionTest() as s:
        return s.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.txn_id == txn_id).count()


def _count_audits(SessionTest, txn_id):
    with SessionTest() as s:
        return s.query(OperatorAuditModel).filter(OperatorAuditModel.txn_id == txn_id).count()


def _payment_failed_body():
    return json.dumps({
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": {
            "id": "pay_api_1",
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


def _plink_body(event="payment_link.paid", plink_id="plink_api_1", status="paid", reference_id="txn_api_1_1",
                short_url="https://rzp.io/i/api123", notes=None):
    if notes is None:
        notes = {"txn_id": "txn_api_1", "attempt_number": "1", "action_type": "retry"}
    entity = {
        "id": plink_id,
        "status": status,
        "reference_id": reference_id,
        "short_url": short_url,
        "notes": notes,
    }
    payload = {
        "entity": "event",
        "account_id": "acc_test",
        "event": event,
        "payload": {"payment_link": {"entity": entity}},
        "created_at": 1700000000,
    }
    return json.dumps(payload).encode()


def test_factory_creates_fastapi_app():
    _, _, _, _, _, _, app, _ = _make_env()
    assert app is not None
    schema = app.openapi()
    assert "/webhooks/razorpay" in schema.get("paths", {})


def test_post_webhooks_razorpay_exists():
    _, _, verifier, _, _, _, _, client = _make_env()
    raw = _payment_failed_body()
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_route_exists"})
    assert resp.status_code in (200, 400, 401)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ingested"


def test_payment_failed_dispatches():
    engine, SessionTest, verifier, webhook_repo, _, _, _, client = _make_env()
    raw = _payment_failed_body()
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_pf_1"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ingested"
    assert webhook_repo.exists("evt_pf_1")


def test_payment_link_paid_transitions_executing_to_recovered():
    engine, SessionTest, verifier, webhook_repo, _, _, _, client = _make_env()
    txn = "txn_api_paid"
    _add_payment(SessionTest, txn_id=txn)
    _set_lifecycle(SessionTest, txn, RecoveryState.EXECUTING)
    ref = _reference_id(txn, 1)
    raw = _plink_body(event="payment_link.paid", plink_id="plink_paid_api", status="paid", reference_id=ref,
                      short_url="https://rzp.io/i/paid_api", notes={"txn_id": txn, "attempt_number": "1", "action_type": "retry"})
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_pl_paid"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ingested"
    assert _get_lifecycle(SessionTest, txn) == RecoveryState.RECOVERED
    assert _count_attempts(SessionTest, txn) == 1
    with SessionTest() as s:
        att = s.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.txn_id == txn).first()
        assert att is not None
        assert att.outcome == "SUCCESS"
    assert _count_audits(SessionTest, txn) == 1
    with SessionTest() as s:
        audit = s.query(OperatorAuditModel).filter(OperatorAuditModel.txn_id == txn).first()
        assert audit is not None
        assert audit.outcome == "recovered"
        assert audit.customer_ref_masked.startswith("MASKED::")


def test_payment_link_failed_results_in_failed():
    _, SessionTest, verifier, _, _, _, _, client = _make_env()
    txn = "txn_api_failed"
    _add_payment(SessionTest, txn_id=txn)
    _set_lifecycle(SessionTest, txn, RecoveryState.EXECUTING)
    ref = _reference_id(txn, 1)
    raw = _plink_body(event="payment_link.failed", plink_id="plink_fail_api", status="failed", reference_id=ref,
                      notes={"txn_id": txn, "attempt_number": "1"})
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_pl_failed"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ingested"
    assert _get_lifecycle(SessionTest, txn) == RecoveryState.FAILED
    assert _count_attempts(SessionTest, txn) == 1
    with SessionTest() as s:
        att = s.query(RecoveryAttemptModel).filter(RecoveryAttemptModel.txn_id == txn).first()
        assert att.outcome == "FAILED"


def test_payment_link_cancelled_results_in_failed():
    _, SessionTest, verifier, _, _, _, _, client = _make_env()
    txn = "txn_api_cancelled"
    _add_payment(SessionTest, txn_id=txn)
    _set_lifecycle(SessionTest, txn, RecoveryState.EXECUTING)
    ref = _reference_id(txn, 1)
    raw = _plink_body(event="payment_link.cancelled", plink_id="plink_can_api", status="cancelled", reference_id=ref,
                      notes={"txn_id": txn, "attempt_number": "1"})
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_pl_cancelled"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ingested"
    assert _get_lifecycle(SessionTest, txn) == RecoveryState.FAILED


def test_payment_link_expired_results_in_failed():
    _, SessionTest, verifier, _, _, _, _, client = _make_env()
    txn = "txn_api_expired"
    _add_payment(SessionTest, txn_id=txn)
    _set_lifecycle(SessionTest, txn, RecoveryState.EXECUTING)
    ref = _reference_id(txn, 1)
    raw = _plink_body(event="payment_link.expired", plink_id="plink_exp_api", status="expired", reference_id=ref,
                      notes={"txn_id": txn, "attempt_number": "1"})
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_pl_expired"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ingested"
    assert _get_lifecycle(SessionTest, txn) == RecoveryState.FAILED


def test_invalid_signature_returns_401():
    _, _, _, _, _, _, _, client = _make_env()
    raw = _payment_failed_body()
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": "bad", "x-razorpay-event-id": "evt_bad_sig_api"})
    assert resp.status_code == 401


def test_missing_event_id_returns_400():
    _, _, verifier, _, _, _, _, client = _make_env()
    raw = _payment_failed_body()
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig})
    assert resp.status_code == 400


def test_blank_event_id_returns_400():
    _, _, verifier, _, _, _, _, client = _make_env()
    raw = _payment_failed_body()
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "   "})
    assert resp.status_code == 400


def test_event_id_in_body_does_not_replace_missing_header():
    _, _, verifier, _, _, _, _, client = _make_env()
    payload = json.loads(_payment_failed_body())
    payload["event_id"] = "evt_body_only"
    raw = json.dumps(payload).encode()
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig})
    assert resp.status_code == 400


def test_malformed_json_returns_400():
    _, _, verifier, _, _, _, _, client = _make_env()
    raw = b"not json {"
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_malformed_api"})
    assert resp.status_code == 400


def test_duplicate_event_id_returns_duplicate_no_second_side_effect():
    _, SessionTest, verifier, _, _, _, _, client = _make_env()
    txn = "txn_api_dup"
    _add_payment(SessionTest, txn_id=txn)
    _set_lifecycle(SessionTest, txn, RecoveryState.EXECUTING)
    ref = _reference_id(txn, 1)
    raw = _plink_body(event="payment_link.paid", plink_id="plink_dup_api", status="paid", reference_id=ref,
                      short_url="https://rzp.io/i/dup_api", notes={"txn_id": txn, "attempt_number": "1"})
    sig = verifier.compute(raw)
    headers = {"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_dup_api"}
    r1 = client.post("/webhooks/razorpay", content=raw, headers=headers)
    assert r1.status_code == 200
    assert r1.json()["status"] == "ingested"
    assert _count_attempts(SessionTest, txn) == 1
    assert _count_audits(SessionTest, txn) == 1
    r2 = client.post("/webhooks/razorpay", content=raw, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"
    assert _count_attempts(SessionTest, txn) == 1
    assert _count_audits(SessionTest, txn) == 1
    assert _get_lifecycle(SessionTest, txn) == RecoveryState.RECOVERED


def test_unsupported_event_returns_ignored():
    _, _, verifier, webhook_repo, _, _, _, client = _make_env()
    raw = json.dumps({"entity": "event", "event": "payment.captured", "payload": {}}).encode()
    sig = verifier.compute(raw)
    resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_unsupported_api"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert webhook_repo.exists("evt_unsupported_api")


def test_webhook_never_invokes_razorpay_recovery_rail():
    _, SessionTest, verifier, _, _, _, _, client = _make_env()
    txn = "txn_api_no_rail"
    _add_payment(SessionTest, txn_id=txn)
    _set_lifecycle(SessionTest, txn, RecoveryState.EXECUTING)
    ref = _reference_id(txn, 1)
    raw = _plink_body(event="payment_link.paid", plink_id="plink_norail", status="paid", reference_id=ref,
                      notes={"txn_id": txn, "attempt_number": "1"})
    sig = verifier.compute(raw)
    # Only assert recovery rail is not used — patching httpx globally breaks TestClient
    with patch("src.infrastructure.razorpay.razorpay_recovery_rail.RazorpayRecoveryRail.execute_attempt") as mock_exec:
        resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_norail"})
        assert resp.status_code == 200
        mock_exec.assert_not_called()


def test_raw_body_reaches_signature_verification():
    _, _, verifier, _, _, _, _, client = _make_env()
    raw = _payment_failed_body()

    seen = {}

    orig_verify = verifier.verify

    def spy_verify(body, sig):
        seen["body"] = body
        seen["sig"] = sig
        return orig_verify(body, sig)

    with patch.object(verifier, "verify", side_effect=spy_verify):
        sig = verifier.compute(raw)
        resp = client.post("/webhooks/razorpay", content=raw, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "evt_raw_body"})
        assert resp.status_code == 200
        assert seen["body"] == raw
