"""Persistence adapter for the recovery lifecycle read model.

The adapter deliberately exposes compare-and-set primitives; orchestration owns
the transaction so a lifecycle change and its operator audit are atomic.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from src.database import Base, RecoveryLifecycleModel
from src.domain.recovery_lifecycle import RecoveryLifecycle, RecoveryState


class RecoveryLifecycleRepository:
    def __init__(self, session_factory=None) -> None:
        from src.database import SessionLocal
        self.session_factory = session_factory or SessionLocal
        session = self.session_factory()
        try:
            Base.metadata.create_all(bind=session.get_bind())
        finally:
            session.close()

    @staticmethod
    def _entity(row: RecoveryLifecycleModel) -> RecoveryLifecycle:
        return RecoveryLifecycle(
            txn_id=row.txn_id,
            state=RecoveryState(row.state),
            updated_at=row.updated_at,
            reason=row.reason,
        )

    def get(self, txn_id: str, session: Optional[Session] = None) -> Optional[RecoveryLifecycle]:
        own = session is None
        session = session or self.session_factory()
        try:
            row = session.get(RecoveryLifecycleModel, txn_id)
            return self._entity(row) if row else None
        finally:
            if own:
                session.close()

    def compare_and_set(
        self, txn_id: str, expected: Optional[RecoveryState], lifecycle: RecoveryLifecycle,
        session: Session,
    ) -> bool:
        if expected is None:
            row = RecoveryLifecycleModel(
                txn_id=txn_id, state=lifecycle.state.value, updated_at=lifecycle.updated_at,
                reason=lifecycle.reason, version=0,
            )
            try:
                session.add(row)
                session.flush()
                return True
            except Exception:
                # IntegrityError for duplicate PK -> CAS lost race
                try:
                    session.rollback()
                except Exception:
                    pass
                # need to begin again? caller will rollback and re-read, so we need session active
                # start a new transaction implicitly after rollback
                return False
        result = session.execute(
            update(RecoveryLifecycleModel)
            .where(
                RecoveryLifecycleModel.txn_id == txn_id,
                RecoveryLifecycleModel.state == expected.value,
            )
            .values(
                state=lifecycle.state.value,
                updated_at=lifecycle.updated_at,
                reason=lifecycle.reason,
                version=RecoveryLifecycleModel.version + 1,
            )
        )
        # Need to flush to know rowcount
        # rowcount 0 means expected did not match
        return result.rowcount == 1

    # Friendly aliases used by callers/read-model tests.
    get_by_txn_id = get
    cas = compare_and_set
