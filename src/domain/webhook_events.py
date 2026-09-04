"""P5.3 — Webhook domain event. Pure domain, no I/O, no HTTP, no provider SDK."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class WebhookIngestStatus(str, enum.Enum):
    INGESTED = "ingested"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"


@dataclass(frozen=True)
class NormalizedPaymentFailedEvent:
    event_id: str
    event_type: str
    payment_id: str
    amount: float
    currency: str
    failure_code: str
    error_description: Optional[str]
    method: Optional[str]
    created_at: datetime


@dataclass(frozen=True)
class WebhookIngestResult:
    status: WebhookIngestStatus
    event_id: str
    event_type: Optional[str] = None
    payment_id: Optional[str] = None


class InvalidSignatureError(ValueError):
    pass


class MissingEventIdError(ValueError):
    pass


class MalformedPayloadError(ValueError):
    pass
