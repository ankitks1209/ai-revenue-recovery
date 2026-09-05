"""M6.2 — FastAPI application factory for Razorpay webhooks.

Explicit, testable construction. No domain logic, no provider SDK calls.
"""

from __future__ import annotations

from fastapi import FastAPI

from src.config import RAZORPAY_WEBHOOK_SECRET
from src.database import SessionLocal
from src.api.razorpay_webhook_router import create_razorpay_webhook_router
from src.application.dispatch_razorpay_webhook import DispatchRazorpayWebhook
from src.application.ingest_payment_link_result import IngestPaymentLinkResult
from src.application.ingest_razorpay_event import IngestRazorpayEvent
from src.infrastructure.razorpay.webhook_verifier import RazorpayWebhookVerifier
from src.infrastructure.razorpay.webhook_event_repository import SQLiteWebhookEventRepository
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.infrastructure.recovery_lifecycle_repository import RecoveryLifecycleRepository


def create_app(
    *,
    webhook_secret: str = RAZORPAY_WEBHOOK_SECRET,
    session_factory=SessionLocal,
    webhook_repo=None,
    payment_repo=None,
    lifecycle_repo=None,
    verifier=None,
) -> FastAPI:
    verifier = verifier or RazorpayWebhookVerifier(webhook_secret or "change_me_for_tests_only_do_not_use_in_prod")
    webhook_repo = webhook_repo or SQLiteWebhookEventRepository()
    payment_repo = payment_repo or SQLiteFailedPaymentRepository(session_factory=session_factory)
    lifecycle_repo = lifecycle_repo or RecoveryLifecycleRepository(session_factory)

    ingest_failed = IngestRazorpayEvent(verifier, webhook_repo)
    ingest_link = IngestPaymentLinkResult(
        verifier=verifier,
        webhook_repo=webhook_repo,
        payment_repository=payment_repo,
        lifecycle_repository=lifecycle_repo,
        session_factory=session_factory,
    )

    dispatcher = DispatchRazorpayWebhook(
        verifier=verifier,
        ingest_failed=ingest_failed,
        ingest_link=ingest_link,
    )

    app = FastAPI(title="AI Revenue Recovery")
    app.include_router(create_razorpay_webhook_router(dispatcher))
    return app


app = create_app()
