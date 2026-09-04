"""T10.1 — MetricsAggregator: pure headline-metric computation. No I/O."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from src.domain.audit import ActionType, AuditEvent, ReasonCode
from src.domain.entities import FailedPaymentEntity
from src.domain.models import Outcome


@dataclass(frozen=True)
class ExceptionMetric:
    txn_id: str
    amount: float
    root_cause_label: str
    status: str
    reason: str
    tier: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class GracefulFailureMetric:
    txn_id: str
    tier: str
    reason_code: str
    customer_ref_masked: str
    decision_rationale: str
    action: str
    timestamp: datetime


@dataclass(frozen=True)
class DashboardMetrics:
    total_at_risk: float
    money_recoverable: float
    money_recovered: float
    recovery_rate: float
    intervention_mix: dict[str, int]
    tier_breakdown: dict[str, int]
    exception_list: tuple[ExceptionMetric, ...]
    graceful_failure: GracefulFailureMetric | None
    total_processed: int
    total_events: int


class MetricsAggregator:
    """Pure domain aggregation. No repositories, no I/O, deterministic."""

    def compute(
        self,
        *,
        audit_events: Sequence[AuditEvent],
        payments: Sequence[FailedPaymentEntity],
    ) -> DashboardMetrics:
        total_processed = len(payments)
        total_events = len(audit_events)

        total_at_risk = round(sum(float(p.amount) for p in payments), 2) if payments else 0.0
        money_recoverable = round(
            sum(float(p.amount) for p in payments if p.recoverable_flag), 2
        ) if payments else 0.0

        money_recovered_raw = sum(
            float(p.amount)
            for p in payments
            if any(a.outcome == Outcome.SUCCESS for a in p.attempts)
        )
        money_recovered = round(money_recovered_raw, 2)

        if money_recoverable > 0:
            recovery_rate = round((money_recovered / money_recoverable) * 100.0, 2)
        else:
            recovery_rate = 0.0

        # Intervention mix — zero-fill expected keys, deterministic ordering
        expected_actions = [
            ActionType.RETRY.value,
            ActionType.DUNNING.value,
            ActionType.REAUTH.value,
            ActionType.REFUSE.value,
        ]
        intervention_mix: dict[str, int] = {k: 0 for k in expected_actions}
        counter = Counter(e.action.value for e in audit_events)
        for k, v in counter.items():
            if k in intervention_mix:
                intervention_mix[k] = v
            else:
                intervention_mix[k] = v
        # Ensure deterministic key order: expected first, then any extras sorted
        ordered_mix: dict[str, int] = {}
        for k in expected_actions:
            ordered_mix[k] = intervention_mix.pop(k, 0)
        for k in sorted(intervention_mix):
            ordered_mix[k] = intervention_mix[k]

        # Tier breakdown — always T1/T2/T3
        tier_breakdown: dict[str, int] = {"T1": 0, "T2": 0, "T3": 0}
        for e in audit_events:
            if e.tier in tier_breakdown:
                tier_breakdown[e.tier] += 1

        # Build txn -> audit lookup for tier/reason_code enrichment
        audit_by_txn: dict[str, AuditEvent] = {}
        for e in audit_events:
            if e.txn_id not in audit_by_txn:
                audit_by_txn[e.txn_id] = e

        exceptions: list[ExceptionMetric] = []
        for p in payments:
            has_success = any(a.outcome == Outcome.SUCCESS for a in p.attempts)
            if has_success:
                continue

            if len(p.escalations) > 0:
                status = "ESCALATED"
                reason = p.escalations[-1].reason
            elif any(a.outcome == Outcome.FAILED for a in p.attempts):
                last_failed = [a for a in p.attempts if a.outcome == Outcome.FAILED][-1]
                status = "FAILED"
                reason = last_failed.reason or "Payment rail declined"
            elif any(a.outcome == Outcome.SKIPPED for a in p.attempts):
                last_skipped = [a for a in p.attempts if a.outcome == Outcome.SKIPPED][-1]
                status = "SKIPPED"
                reason = last_skipped.reason or "Skipped due to retry policy hold"
            else:
                status = "UNPROCESSED"
                reason = "No attempt executed"

            audit_match = audit_by_txn.get(p.txn_id)
            tier = audit_match.tier if audit_match else None
            reason_code = audit_match.reason_code.value if audit_match and audit_match.reason_code else None

            exceptions.append(
                ExceptionMetric(
                    txn_id=p.txn_id,
                    amount=round(float(p.amount), 2),
                    root_cause_label=p.root_cause_label,
                    status=status,
                    reason=reason,
                    tier=tier,
                    reason_code=reason_code,
                )
            )

        # Graceful failure — deterministic: earliest timestamp among REFUSE+DO_NOT_RETRY+T3
        candidates = [
            e
            for e in audit_events
            if e.action == ActionType.REFUSE
            and e.reason_code == ReasonCode.DO_NOT_RETRY
            and e.tier == "T3"
        ]
        graceful_failure: GracefulFailureMetric | None = None
        if candidates:
            chosen = min(candidates, key=lambda e: (e.timestamp, e.txn_id))
            graceful_failure = GracefulFailureMetric(
                txn_id=chosen.txn_id,
                tier=chosen.tier,
                reason_code=chosen.reason_code.value,
                customer_ref_masked=chosen.customer_ref_masked,
                decision_rationale=chosen.decision_rationale,
                action=chosen.action.value,
                timestamp=chosen.timestamp,
            )

        return DashboardMetrics(
            total_at_risk=total_at_risk,
            money_recoverable=money_recoverable,
            money_recovered=money_recovered,
            recovery_rate=recovery_rate,
            intervention_mix=ordered_mix,
            tier_breakdown=tier_breakdown,
            exception_list=tuple(exceptions),
            graceful_failure=graceful_failure,
            total_processed=total_processed,
            total_events=total_events,
        )
