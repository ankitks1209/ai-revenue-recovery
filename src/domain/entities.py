from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
from src.domain.models import Outcome

@dataclass
class RecoveryAttempt:
    txn_id: str
    attempt_number: int
    outcome: Outcome
    timestamp: datetime
    reason: Optional[str] = None
    action_type: Optional[str] = None
    id: Optional[int] = None

@dataclass
class Escalation:
    txn_id: str
    reason: str
    timestamp: datetime
    id: Optional[int] = None

@dataclass
class FailedPaymentEntity:
    txn_id: str
    customer_id: str
    amount: float
    currency: str
    failure_code: str
    root_cause_label: str
    recoverable_flag: bool
    retry_count: int
    timestamp: datetime
    payment_method: str
    attempts: List[RecoveryAttempt] = field(default_factory=list)
    escalations: List[Escalation] = field(default_factory=list)

    @property
    def executed_retry_count(self) -> int:
        """
        Calculates retry_count based on executed rail attempts (SUCCESS or FAILED).
        SKIPPED and ESCALATED records do NOT increment retry_count.
        """
        return sum(1 for att in self.attempts if att.outcome in (Outcome.SUCCESS, Outcome.FAILED))

    @property
    def is_terminal(self) -> bool:
        """
        Returns True if the record has reached a terminal state (recovered via SUCCESS or escalated).
        """
        has_success = any(att.outcome == Outcome.SUCCESS for att in self.attempts)
        has_escalation = len(self.escalations) > 0
        return has_success or has_escalation

    @property
    def last_executed_attempt_at(self) -> Optional[datetime]:
        """
        Returns the timestamp of the last executed rail attempt (SUCCESS or FAILED).
        Does NOT use self.timestamp (the initial payment failure timestamp).
        """
        executed = [att for att in self.attempts if att.outcome in (Outcome.SUCCESS, Outcome.FAILED)]
        if not executed:
            return None
        return max(att.timestamp for att in executed)
