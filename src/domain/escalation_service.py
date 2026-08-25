from datetime import datetime
from src.domain.entities import Escalation

class EscalationService:
    @staticmethod
    def create_escalation(txn_id: str, reason: str, timestamp: datetime) -> Escalation:
        """
        Creates an Escalation entity routing the item for human review.
        """
        return Escalation(
            txn_id=txn_id,
            reason=reason,
            timestamp=timestamp
        )
