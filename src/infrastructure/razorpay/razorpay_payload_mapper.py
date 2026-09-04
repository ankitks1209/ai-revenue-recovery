"""P5.3 — Razorpay payload → NormalizedPaymentFailedEvent. Infrastructure only."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.domain.webhook_events import NormalizedPaymentFailedEvent, MalformedPayloadError


SUPPORTED_EVENT = "payment.failed"


def parse_razorpay_payload(event_id: str, raw_body: bytes) -> NormalizedPaymentFailedEvent | None:
    """Parse raw Razorpay webhook body.

    Returns NormalizedPaymentFailedEvent for payment.failed, None for unsupported event.
    Raises MalformedPayloadError if JSON invalid or required fields missing for payment.failed.
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

    if event_type != SUPPORTED_EVENT:
        return None

    contains = payload.get("payload")
    if not isinstance(contains, dict):
        raise MalformedPayloadError("missing payload.payload for payment.failed")
    payment_wrap = contains.get("payment")
    if not isinstance(payment_wrap, dict):
        raise MalformedPayloadError("missing payload.payment for payment.failed")
    entity = payment_wrap.get("entity")
    if not isinstance(entity, dict):
        raise MalformedPayloadError("missing payload.payment.entity for payment.failed")

    payment_id = entity.get("id")
    if not isinstance(payment_id, str) or not payment_id:
        raise MalformedPayloadError("missing payment entity id")

    raw_amount = entity.get("amount")
    if raw_amount is None:
        raise MalformedPayloadError("missing payment entity amount")
    try:
        amount_paise = int(raw_amount)
    except Exception:
        raise MalformedPayloadError("invalid payment amount")
    amount = amount_paise / 100.0

    currency = entity.get("currency") or "INR"
    if not isinstance(currency, str):
        currency = "INR"

    status = entity.get("status")
    if status is not None and status != "failed":
        raise MalformedPayloadError(f"expected payment status 'failed', got '{status}'")

    error_code = entity.get("error_code") or "UNKNOWN"
    if not isinstance(error_code, str):
        error_code = str(error_code)
    error_desc = entity.get("error_description")
    if error_desc is not None and not isinstance(error_desc, str):
        error_desc = str(error_desc)
    method = entity.get("method")
    if method is not None and not isinstance(method, str):
        method = str(method)

    created_at_raw = entity.get("created_at")
    if created_at_raw is not None:
        try:
            created_at = datetime.fromtimestamp(int(created_at_raw), tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            created_at = datetime.utcnow()
    else:
        created_at = datetime.utcnow()

    return NormalizedPaymentFailedEvent(
        event_id=event_id,
        event_type=event_type,
        payment_id=payment_id,
        amount=amount,
        currency=currency,
        failure_code=error_code,
        error_description=error_desc,
        method=method,
        created_at=created_at,
    )
