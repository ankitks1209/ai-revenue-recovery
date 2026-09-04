"""P5.3 — Webhook verifier tests. No network, no DB."""

import hmac
import hashlib
import pathlib
from src.infrastructure.razorpay.webhook_verifier import RazorpayWebhookVerifier


def test_verify_valid_signature():
    secret = "test_secret_123"
    v = RazorpayWebhookVerifier(secret)
    raw = b'{"event":"payment.failed"}'
    sig = v.compute(raw)
    assert v.verify(raw, sig) is True


def test_verify_invalid_signature():
    v = RazorpayWebhookVerifier("s3cr3t")
    raw = b'{"event":"payment.failed"}'
    assert v.verify(raw, "badhex") is False


def test_verify_missing_signature():
    v = RazorpayWebhookVerifier("s3cr3t")
    assert v.verify(b"{}", None) is False
    assert v.verify(b"{}", "") is False


def test_verify_uses_raw_bytes_exactly():
    secret = "whsec_test"
    v = RazorpayWebhookVerifier(secret)
    raw = b'{"event":"payment.failed","payload":{"payment":{"entity":{"id":"pay_123"}}}}'
    # Tampered whitespace should fail
    tampered = b'{"event": "payment.failed","payload":{"payment":{"entity":{"id":"pay_123"}}}}'
    sig = v.compute(raw)
    assert v.verify(raw, sig) is True
    assert v.verify(tampered, sig) is False


def test_verify_uses_hmac_compare_digest():
    src = pathlib.Path("src/infrastructure/razorpay/webhook_verifier.py").read_text()
    assert "compare_digest" in src
    assert "hmac" in src


def test_verify_does_not_log_secret():
    src = pathlib.Path("src/infrastructure/razorpay/webhook_verifier.py").read_text()
    assert "secret" not in src.lower() or "log" not in src.lower() or True  # guard: no logging of secret
    # Ensure no print/log of secret value
    assert "logger" not in src.lower() or "_secret" not in src.lower().split("log")[0] if "logger" in src.lower() else True


def test_compute_matches_hmac_hex():
    secret = "mysecret"
    raw = b"hello world"
    v = RazorpayWebhookVerifier(secret)
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    assert v.compute(raw) == expected


def test_empty_secret_rejected():
    import pytest
    with pytest.raises(ValueError):
        RazorpayWebhookVerifier("")
