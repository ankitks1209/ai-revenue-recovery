"""P5.3 — Webhook persistence port. Infrastructure boundary, no domain Razorpay leakage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class WebhookEventRepositoryPort(ABC):
    @abstractmethod
    def exists(self, event_id: str) -> bool: ...

    @abstractmethod
    def try_insert(
        self,
        event_id: str,
        event_type: str,
        payment_id: Optional[str] = None,
    ) -> bool:
        """Attempt to insert event_id atomically. Returns True if inserted, False if duplicate."""
        ...

    @abstractmethod
    def all_event_ids(self) -> list[str]: ...


class WebhookSignatureVerifierPort(ABC):
    @abstractmethod
    def verify(self, raw_body: bytes, signature: str | None) -> bool: ...
