"""P5.3 — Application orchestration for Razorpay webhook ingestion. No HTTP objects."""

from __future__ import annotations

from typing import Optional

from src.domain.webhook_events import (
    WebhookIngestResult,
    WebhookIngestStatus,
    InvalidSignatureError,
    MissingEventIdError,
    MalformedPayloadError,
)
from src.infrastructure.ports_webhook import WebhookEventRepositoryPort, WebhookSignatureVerifierPort
from src.infrastructure.razorpay.razorpay_payload_mapper import parse_razorpay_payload


class IngestRazorpayEvent:
    """Orchestrates: verify signature → validate payload → dedupe → persist."""

    def __init__(
        self,
        verifier: WebhookSignatureVerifierPort,
        webhook_repo: WebhookEventRepositoryPort,
    ) -> None:
        self._verifier = verifier
        self._repo = webhook_repo

    def ingest(
        self,
        *,
        raw_body: bytes,
        signature: str | None,
        event_id: str | None,
    ) -> WebhookIngestResult:
        if not event_id or not str(event_id).strip():
            raise MissingEventIdError("missing X-Razorpay-Event-Id")
        eid = str(event_id).strip()

        if not self._verifier.verify(raw_body, signature):
            raise InvalidSignatureError("invalid signature")

        parsed = parse_razorpay_payload(eid, raw_body)

        if parsed is None:
            # Unsupported event — still idempotent
            if self._repo.exists(eid):
                return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
            inserted = self._repo.try_insert(eid, event_type="unsupported")
            if not inserted:
                return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
            return WebhookIngestResult(status=WebhookIngestStatus.IGNORED, event_id=eid)

        # Supported payment.failed — dedupe then persist
        if self._repo.exists(eid):
            return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)
        inserted = self._repo.try_insert(eid, event_type=parsed.event_type, payment_id=parsed.payment_id)
        if not inserted:
            return WebhookIngestResult(status=WebhookIngestStatus.DUPLICATE, event_id=eid)

        return WebhookIngestResult(
            status=WebhookIngestStatus.INGESTED,
            event_id=eid,
            event_type=parsed.event_type,
            payment_id=parsed.payment_id,
        )
