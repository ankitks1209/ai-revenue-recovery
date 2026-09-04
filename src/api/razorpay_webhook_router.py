"""P5.3 — FastAPI transport for Razorpay webhooks. No domain logic here."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
import json

from src.domain.webhook_events import InvalidSignatureError, MissingEventIdError, MalformedPayloadError


def create_razorpay_webhook_router(ingest_service) -> APIRouter:
    router = APIRouter()

    @router.post("/webhooks/razorpay")
    async def razorpay_webhook(request: Request):
        raw_body = await request.body()
        signature = request.headers.get("x-razorpay-signature")
        # Canonical idempotency key only — no fallback
        event_id = request.headers.get("x-razorpay-event-id")

        try:
            result = ingest_service.ingest(
                raw_body=raw_body,
                signature=signature,
                event_id=event_id,
            )
        except MissingEventIdError as e:
            return Response(content=json.dumps({"error": str(e)}), status_code=400, media_type="application/json")
        except InvalidSignatureError as e:
            return Response(content=json.dumps({"error": str(e)}), status_code=401, media_type="application/json")
        except MalformedPayloadError as e:
            return Response(content=json.dumps({"error": str(e)}), status_code=400, media_type="application/json")

        if result.status.value == "ingested":
            return Response(
                content=json.dumps({"status": "ingested", "event_id": result.event_id}),
                status_code=200,
                media_type="application/json",
            )
        if result.status.value == "duplicate":
            return Response(
                content=json.dumps({"status": "duplicate", "event_id": result.event_id}),
                status_code=200,
                media_type="application/json",
            )
        # ignored
        return Response(
            content=json.dumps({"status": "ignored", "event_id": result.event_id}),
            status_code=200,
            media_type="application/json",
        )

    return router
