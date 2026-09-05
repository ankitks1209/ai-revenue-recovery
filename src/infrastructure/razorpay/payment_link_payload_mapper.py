"""M5.3.1 — Razorpay Payment Link payload → NormalizedPaymentLinkResult. Infrastructure only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from src.domain.webhook_events import MalformedPayloadError


@dataclass(frozen=True)
class NormalizedPaymentLinkResult:
    event_id: str
    event_type: str
    plink_id: str
    reference_id: Optional[str]
    notes_txn_id: Optional[str]
    notes_attempt: Optional[int]
    notes_action_type: Optional[str]
    status: str
    short_url: Optional[str]


def parse_payment_link_payload(event_id: str, raw_body: bytes) -> NormalizedPaymentLinkResult | None:
    """Parse raw Razorpay webhook body for payment_link events.

    Returns NormalizedPaymentLinkResult for payment_link.* events,
    None for unsupported/unrelated events.
    Raises MalformedPayloadError if JSON invalid or required fields missing for payment_link.*.
    """
    try:
        payload: Any = json.loads(raw_body)
    except Exception as exc:
        raise MalformedPayloadError(f"invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise MalformedPayloadError("payload must be a JSON object")

    event_type = payload.get("event")
    if not isinstance(event_type, str) or not event_type:
        raise MalformedPayloadError("missing or invalid 'event' field")

    if not event_type.startswith("payment_link."):
        return None

    contains = payload.get("payload")
    if not isinstance(contains, dict):
        raise MalformedPayloadError("missing payload.payload for payment_link event")

    plink_wrap = contains.get("payment_link")
    if not isinstance(plink_wrap, dict):
        raise MalformedPayloadError("missing payload.payment_link for payment_link event")

    entity = plink_wrap.get("entity")
    if not isinstance(entity, dict):
        raise MalformedPayloadError("missing payload.payment_link.entity for payment_link event")

    plink_id = entity.get("id")
    if not isinstance(plink_id, str) or not plink_id:
        raise MalformedPayloadError("missing payment_link entity id")

    status_raw = entity.get("status")
    if not isinstance(status_raw, str) or not status_raw:
        raise MalformedPayloadError("missing payment_link entity status")
    status = status_raw.lower().strip()

    reference_id = entity.get("reference_id")
    if not isinstance(reference_id, str) or not reference_id.strip():
        reference_id = None
    else:
        reference_id = reference_id.strip()

    short_url = entity.get("short_url")
    if not isinstance(short_url, str) or not short_url.strip():
        short_url = None

    notes = entity.get("notes")
    if not isinstance(notes, dict):
        notes = {}

    notes_txn_id = notes.get("txn_id")
    if not isinstance(notes_txn_id, str) or not notes_txn_id.strip():
        notes_txn_id = None
    else:
        notes_txn_id = notes_txn_id.strip()

    notes_attempt: Optional[int] = None
    raw_attempt = notes.get("attempt_number")
    if raw_attempt is not None:
        try:
            notes_attempt = int(str(raw_attempt).strip())
        except Exception:
            notes_attempt = None

    notes_action_type = notes.get("action_type")
    if not isinstance(notes_action_type, str) or not notes_action_type.strip():
        notes_action_type = None
    else:
        notes_action_type = notes_action_type.strip()

    return NormalizedPaymentLinkResult(
        event_id=event_id,
        event_type=event_type,
        plink_id=plink_id,
        reference_id=reference_id,
        notes_txn_id=notes_txn_id,
        notes_attempt=notes_attempt,
        status=status,
        short_url=short_url,
        notes_action_type=notes_action_type,
    )
