"""
Phase 3 Day 8 — GenerateAuditReport Use Case

Reads masked audit events from AuditLogRepository and produces:
1. Per-event explainable audit trail
2. Escalation summary with tier distribution and refusal/escalation counts

This is a thin application orchestrator with no business logic.
All masking and tier assignment happened at event creation time.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime

from src.infrastructure.audit_repository import AuditLogRepository
from src.domain.audit import ActionType, Outcome


class GenerateAuditReport:
    """
    Day 8 Use Case: Read masked audit events and produce explainable audit trail + escalation summary.

    Responsibilities:
    - Query AuditLogRepository for masked events (optionally filtered by time)
    - Transform domain AuditEvent objects into JSON-compatible audit trail entries
    - Aggregate escalation summary (T1/T2/T3/refusal counts)
    - Return structured data (no UI rendering)
    """

    def __init__(self, audit_repository: Optional[AuditLogRepository] = None):
        """
        Initialize with an AuditLogRepository.

        Args:
            audit_repository: Optional repository. Defaults to file-based SQLite if not provided.
        """
        self._repo = audit_repository or AuditLogRepository(db_url="sqlite:///audit_log.db")

    def run(self, since: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Generate audit report containing per-event trail and escalation summary.

        Args:
            since: Optional datetime filter — only events at/after this timestamp

        Returns:
            {
                "audit_trail": [
                    {
                        "event_id": str,
                        "txn_id": str,
                        "timestamp": str,  # ISO 8601
                        "action": str,
                        "outcome": str,
                        "reason_code": str,
                        "tier": str,
                        "customer_ref_masked": str,
                        "decision_rationale": str
                    },
                    ...
                ],
                "escalation_summary": {
                    "tier_counts": {
                        "T1": int,
                        "T2": int,
                        "T3": int
                    },
                    "refusal_count": int,
                    "total_escalated_count": int,
                    "total_events": int
                }
            }
        """
        # Query masked events from repository
        events = self._repo.all_events(since=since)

        # Transform events into JSON-compatible audit trail entries
        audit_trail = self._build_audit_trail(events)

        # Aggregate escalation summary
        escalation_summary = self._build_escalation_summary(events)

        return {
            "audit_trail": audit_trail,
            "escalation_summary": escalation_summary
        }

    def _build_audit_trail(self, events: List) -> List[Dict[str, Any]]:
        """
        Transform domain AuditEvent objects into JSON-compatible audit trail entries.

        Converts:
        - datetime objects to ISO 8601 strings
        - Enum objects to their string values
        - Preserves masked customer references exactly as stored
        """
        trail = []
        for event in events:
            trail.append({
                "event_id": event.event_id,
                "txn_id": event.txn_id,
                "timestamp": event.timestamp.isoformat(),
                "action": event.action.value,
                "outcome": event.outcome.value,
                "reason_code": event.reason_code.value,
                "tier": event.tier,
                "customer_ref_masked": event.customer_ref_masked,
                "decision_rationale": event.decision_rationale
            })
        return trail

    def _build_escalation_summary(self, events: List) -> Dict[str, Any]:
        """
        Aggregate escalation metrics from audit events.

        Computes:
        - tier_counts: Distribution across T1/T2/T3
        - refusal_count: Count of REFUSE actions (graceful failures)
        - total_escalated_count: Count of ESCALATED outcomes
        - total_events: Total number of events processed
        """
        tier_counts = {"T1": 0, "T2": 0, "T3": 0}
        refusal_count = 0
        total_escalated_count = 0

        for event in events:
            # Count by tier
            if event.tier in tier_counts:
                tier_counts[event.tier] += 1

            # Count refusals (graceful failures)
            if event.action == ActionType.REFUSE:
                refusal_count += 1

            # Count escalated outcomes
            if event.outcome == Outcome.ESCALATED:
                total_escalated_count += 1

        return {
            "tier_counts": tier_counts,
            "refusal_count": refusal_count,
            "total_escalated_count": total_escalated_count,
            "total_events": len(events)
        }
