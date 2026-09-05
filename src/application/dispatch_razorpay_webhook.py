"""M6.2 — Dispatch verified Razorpay webhook to the correct ingest service.

No domain/business logic here. Single responsibility: verify HMAC on raw bytes,
inspect event type, delegate to existing ingest services. Keeps transport thin.

Both ingest services MUST share the same WebhookEventRepository instance to
preserve PK idempotency across payment.failed and payment_link.* families.
"""

from __future__ import annotations

import json

from src.domain.webhook_events import MalformedPayloadError, MissingEventIdError, InvalidSignatureError
from src.infrastructure.ports_webhook import WebhookSignatureVerifierPort


class DispatchRazorpayWebhook:
    """Verified dispatcher — payment.failed vs payment_link.*.

    Construction is explicit and dependency-injectable for test isolation.
    Does not call any Razorpay HTTP API.
    """

    def __init__(
        self,
        verifier: WebhookSignatureVerifierPort,
        ingest_failed,
        ingest_link,
    ) -> None:
        self._verifier = verifier
        self._ingest_failed = ingest_failed
        self._ingest_link = ingest_link

    def ingest(
        self,
        *,
        raw_body: bytes,
        signature: str | None,
        event_id: str | None,
    ):
        if not event_id or not str(event_id).strip():
            raise MissingEventIdError("missing X-Razorpay-Event-Id")
        eid = str(event_id).strip()

        if not self._verifier.verify(raw_body, signature):
            raise InvalidSignatureError("invalid signature")

        try:
            payload = json.loads(raw_body)
        except Exception as exc:
            raise MalformedPayloadError(f"invalid JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise MalformedPayloadError("payload must be a JSON object")

        event_type = payload.get("event")
        if not isinstance(event_type, str) or not event_type.strip():
            raise MalformedPayloadError("missing or invalid 'event' field")

        et = event_type.strip()
        if et == "payment.failed":
            return self._ingest_failed.ingest(raw_body=raw_body, signature=signature, event_id=event_id)
        if et.startswith("payment_link."):
            return self._ingest_link.ingest(raw_body=raw_body, signature=signature, event_id=event_id)
        return self._ingest_failed.ingest(raw_body=raw_body, signature=signature, event_id=event_id)
