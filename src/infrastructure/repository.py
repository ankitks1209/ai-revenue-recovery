from typing import List, Optional
from sqlalchemy.orm import Session
from src.database import FailedPayment as FailedPaymentModel, RecoveryAttemptModel, EscalationModel, SessionLocal
from src.domain.entities import FailedPaymentEntity, RecoveryAttempt, Escalation
from src.domain.models import Outcome
from src.infrastructure.ports import FailedPaymentRepositoryPort

class SQLiteFailedPaymentRepository(FailedPaymentRepositoryPort):
    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    def _to_entity(self, session: Session, db_p: FailedPaymentModel) -> FailedPaymentEntity:
        attempts_models = session.query(RecoveryAttemptModel).filter(
            RecoveryAttemptModel.txn_id == db_p.txn_id
        ).order_by(RecoveryAttemptModel.timestamp.asc()).all()

        escalations_models = session.query(EscalationModel).filter(
            EscalationModel.txn_id == db_p.txn_id
        ).order_by(EscalationModel.timestamp.asc()).all()

        attempts = [
            RecoveryAttempt(
                id=att.id,
                txn_id=att.txn_id,
                attempt_number=att.attempt_number,
                outcome=Outcome(att.outcome),
                timestamp=att.timestamp,
                reason=att.reason,
                action_type=att.action_type
            ) for att in attempts_models
        ]

        escalations = [
            Escalation(
                id=esc.id,
                txn_id=esc.txn_id,
                reason=esc.reason,
                timestamp=esc.timestamp
            ) for esc in escalations_models
        ]

        return FailedPaymentEntity(
            txn_id=db_p.txn_id,
            customer_id=db_p.customer_id,
            amount=db_p.amount,
            currency=db_p.currency,
            failure_code=db_p.failure_code,
            root_cause_label=db_p.root_cause_label,
            recoverable_flag=db_p.recoverable_flag,
            retry_count=db_p.retry_count,
            timestamp=db_p.timestamp,
            payment_method=db_p.payment_method,
            attempts=attempts,
            escalations=escalations
        )

    def get_all_payments(self) -> List[FailedPaymentEntity]:
        session: Session = self.session_factory()
        try:
            db_payments = session.query(FailedPaymentModel).all()
            return [self._to_entity(session, db_p) for db_p in db_payments]
        finally:
            session.close()

    def get_payment_by_id(self, txn_id: str) -> Optional[FailedPaymentEntity]:
        session: Session = self.session_factory()
        try:
            db_p = session.query(FailedPaymentModel).filter(FailedPaymentModel.txn_id == txn_id).first()
            if not db_p:
                return None
            return self._to_entity(session, db_p)
        finally:
            session.close()

    def save_attempt(self, attempt: RecoveryAttempt) -> RecoveryAttempt:
        session: Session = self.session_factory()
        try:
            attempt_model = RecoveryAttemptModel(
                txn_id=attempt.txn_id,
                attempt_number=attempt.attempt_number,
                outcome=attempt.outcome.value,
                reason=attempt.reason,
                action_type=attempt.action_type,
                timestamp=attempt.timestamp
            )
            session.add(attempt_model)
            session.commit()
            session.refresh(attempt_model)

            # If SUCCESS or FAILED, update FailedPayment.retry_count to number of executed rail attempts
            if attempt.outcome in (Outcome.SUCCESS, Outcome.FAILED):
                executed_count = session.query(RecoveryAttemptModel).filter(
                    RecoveryAttemptModel.txn_id == attempt.txn_id,
                    RecoveryAttemptModel.outcome.in_([Outcome.SUCCESS.value, Outcome.FAILED.value])
                ).count()
                db_p = session.query(FailedPaymentModel).filter(FailedPaymentModel.txn_id == attempt.txn_id).first()
                if db_p:
                    db_p.retry_count = executed_count
                    session.commit()

            attempt.id = attempt_model.id
            return attempt
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def save_escalation(self, escalation: Escalation) -> Escalation:
        session: Session = self.session_factory()
        try:
            esc_model = EscalationModel(
                txn_id=escalation.txn_id,
                reason=escalation.reason,
                timestamp=escalation.timestamp
            )
            session.add(esc_model)
            session.commit()
            session.refresh(esc_model)
            escalation.id = esc_model.id
            return escalation
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
