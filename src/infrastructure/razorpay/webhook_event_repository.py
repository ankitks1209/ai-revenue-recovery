"""P5.3 — Webhook event persistence. Dedicated boundary, separate from failed_payments."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import String, DateTime, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from src.infrastructure.ports_webhook import WebhookEventRepositoryPort


class WebhookBase(DeclarativeBase):
    pass


class WebhookEventRow(WebhookBase):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payment_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class SQLiteWebhookEventRepository(WebhookEventRepositoryPort):
    """SQLite-backed webhook event store. Race-safe via PK unique constraint."""

    def __init__(self, db_url: str = "sqlite:///webhook_events.db") -> None:
        self._engine = create_engine(db_url, echo=False)
        WebhookBase.metadata.create_all(self._engine)

    def exists(self, event_id: str) -> bool:
        with Session(self._engine) as session:
            return session.get(WebhookEventRow, event_id) is not None

    def try_insert(self, event_id: str, event_type: str, payment_id: Optional[str] = None) -> bool:
        try:
            with Session(self._engine) as session:
                session.add(
                    WebhookEventRow(
                        event_id=event_id,
                        event_type=event_type,
                        payment_id=payment_id,
                        created_at=datetime.utcnow(),
                    )
                )
                session.commit()
                return True
        except IntegrityError:
            return False

    def all_event_ids(self) -> list[str]:
        with Session(self._engine) as session:
            rows = session.query(WebhookEventRow).all()
            return [r.event_id for r in rows]


class InMemoryWebhookEventRepository(WebhookEventRepositoryPort):
    """In-memory impl for tests. Also race-safe via set check."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, Optional[str]]] = {}

    def exists(self, event_id: str) -> bool:
        return event_id in self._store

    def try_insert(self, event_id: str, event_type: str, payment_id: Optional[str] = None) -> bool:
        if event_id in self._store:
            return False
        self._store[event_id] = (event_type, payment_id)
        return True

    def all_event_ids(self) -> list[str]:
        return list(self._store.keys())
