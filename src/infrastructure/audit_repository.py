"""T6.3 — Append-only audit persistence (SQLAlchemy/SQLite). No update/delete."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from src.domain.audit import ActionType, AuditEvent, Outcome, ReasonCode


class AuditBase(DeclarativeBase):
    pass


class AuditEventRow(AuditBase):
    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    txn_id: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    decision_rationale: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    reason_code: Mapped[str] = mapped_column(String, nullable=False)
    customer_ref_masked: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False)


class AuditLogRepository:
    """Append-only. Exposes ONLY append() and read helpers — no update/delete."""

    def __init__(self, db_url: str = "sqlite:///audit_log.db") -> None:
        self._engine = create_engine(db_url, echo=False)
        AuditBase.metadata.create_all(self._engine)

    def append(self, event: AuditEvent) -> None:
        with Session(self._engine) as session:
            session.add(
                AuditEventRow(
                    event_id=event.event_id,
                    txn_id=event.txn_id,
                    timestamp=event.timestamp,
                    action=event.action.value if hasattr(event.action, "value") else str(event.action),
                    decision_rationale=event.decision_rationale,
                    outcome=event.outcome.value if hasattr(event.outcome, "value") else str(event.outcome),
                    reason_code=event.reason_code.value if hasattr(event.reason_code, "value") else str(event.reason_code),
                    customer_ref_masked=event.customer_ref_masked,
                    tier=event.tier,
                )
            )
            session.commit()

    def all_events(self, since: Optional[datetime] = None) -> List[AuditEvent]:
        with Session(self._engine) as session:
            query = session.query(AuditEventRow)
            if since is not None:
                query = query.filter(AuditEventRow.timestamp >= since)
            rows = query.order_by(AuditEventRow.timestamp.asc(), AuditEventRow.event_id.asc()).all()
            return [
                AuditEvent(
                    event_id=r.event_id,
                    txn_id=r.txn_id,
                    timestamp=r.timestamp,
                    action=ActionType(r.action),
                    decision_rationale=r.decision_rationale,
                    outcome=Outcome(r.outcome),
                    reason_code=ReasonCode(r.reason_code),
                    customer_ref_masked=r.customer_ref_masked,
                    tier=r.tier,
                )
                for r in rows
            ]
