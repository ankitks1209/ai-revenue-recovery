"""P5.3 — Razorpay HMAC-SHA256 verifier. Infrastructure only."""

from __future__ import annotations

import hashlib
import hmac

from src.infrastructure.ports_webhook import WebhookSignatureVerifierPort


class RazorpayWebhookVerifier(WebhookSignatureVerifierPort):
    """Verifies X-Razorpay-Signature = hex(HMAC-SHA256(secret, raw_body))."""

    def __init__(self, webhook_secret: str) -> None:
        if not webhook_secret:
            raise ValueError("webhook_secret must be non-empty")
        self._secret = webhook_secret.encode("utf-8")

    def verify(self, raw_body: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        expected = hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def compute(self, raw_body: bytes) -> str:
        """Test helper — compute expected signature for raw_body."""
        return hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
