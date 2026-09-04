from datetime import datetime
from typing import Optional
from sqlalchemy import create_engine, String, Float, Integer, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from src.config import DATABASE_URL

# Create database engine and session maker
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

class FailedPayment(Base):
    __tablename__ = "failed_payments"

    txn_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    customer_id: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    failure_code: Mapped[str] = mapped_column(String, nullable=False)
    root_cause_label: Mapped[str] = mapped_column(String, nullable=False)
    recoverable_flag: Mapped[bool] = mapped_column(Boolean, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payment_method: Mapped[str] = mapped_column(String, nullable=False)

class RecoveryAttemptModel(Base):
    __tablename__ = "recovery_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_id: Mapped[str] = mapped_column(String, ForeignKey("failed_payments.txn_id"), index=True, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    action_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

class EscalationModel(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_id: Mapped[str] = mapped_column(String, ForeignKey("failed_payments.txn_id"), index=True, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

def init_db():
    """Create all tables in the database (recreating them for fresh runs)."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

def get_db():
    """Database session generator."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

