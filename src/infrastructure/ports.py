from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional
from src.domain.entities import FailedPaymentEntity, RecoveryAttempt, Escalation
from src.domain.models import RailResponse

class ClockPort(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Returns current time (system or simulated)."""
        pass

class PaymentRailPort(ABC):
    @abstractmethod
    def execute_attempt(self, txn_id: str, amount: float, action_type: str, attempt_number: int) -> RailResponse:
        """Executes a recovery rail attempt."""
        pass

class FailedPaymentRepositoryPort(ABC):
    @abstractmethod
    def get_all_payments(self) -> List[FailedPaymentEntity]:
        """Loads all failed payments along with attempt history and escalations."""
        pass

    @abstractmethod
    def get_payment_by_id(self, txn_id: str) -> Optional[FailedPaymentEntity]:
        """Loads a single payment by transaction ID with attempt history."""
        pass

    @abstractmethod
    def save_attempt(self, attempt: RecoveryAttempt) -> RecoveryAttempt:
        """Persists a new recovery attempt record."""
        pass

    @abstractmethod
    def save_escalation(self, escalation: Escalation) -> Escalation:
        """Persists a new escalation record."""
        pass
