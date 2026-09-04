"""T6.4 — StructuredLogger adapter. Only ever receives already-masked events."""
from __future__ import annotations

import structlog

from src.domain.audit import AuditEvent


_logger = structlog.get_logger("revenue_recovery.audit")


class StructuredLogger:
    def emit(self, event: AuditEvent) -> None:
        _logger.info(
            "money_action",
            event_id=event.event_id,
            txn_id=event.txn_id,
            action=event.action.value if hasattr(event.action, "value") else str(event.action),
            outcome=event.outcome.value if hasattr(event.outcome, "value") else str(event.outcome),
            reason_code=event.reason_code.value if hasattr(event.reason_code, "value") else str(event.reason_code),
            tier=event.tier,
            customer_ref=event.customer_ref_masked,  # already masked upstream
        )
