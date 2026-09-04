"""P5.4 — GetRecoveryQueue: read-only operator queue query."""
from __future__ import annotations

from typing import Optional

from src.application.recommend_recovery import RecommendRecovery
from src.domain.entities import FailedPaymentEntity
from src.domain.recovery_lifecycle import RecoveryState, derive_state
from src.domain.recovery_queue import (
    RecoveryQueue,
    RecoveryQueueRow,
    derive_last_attempt_at,
    derive_queue_status,
)
from src.domain.recovery_recommendation import RecommendationKind
from src.infrastructure.audit_repository import AuditLogRepository
from src.infrastructure.ports import FailedPaymentRepositoryPort


class GetRecoveryQueue:
    """Separate read-model query service. No DB writes, no provider calls."""

    def __init__(
        self,
        payment_repository: FailedPaymentRepositoryPort,
        audit_repository: AuditLogRepository,
        recommend_recovery: RecommendRecovery | None = None,
    ) -> None:
        self._payment_repository = payment_repository
        self._audit_repository = audit_repository
        self._recommend = recommend_recovery or RecommendRecovery()

    def row_for(self, payment: FailedPaymentEntity, audit_map: dict | None = None) -> RecoveryQueueRow:
        lifecycle_state = derive_state(payment)
        rec = self._recommend.recommend(payment, auto_eligible=False)
        status = derive_queue_status(payment)
        last_attempt_at = derive_last_attempt_at(payment)
        audit = (audit_map or {}).get(payment.txn_id)
        tier = audit.tier if audit is not None else None
        reason_code = audit.reason_code.value if audit is not None and audit.reason_code else None
        customer_ref_masked = audit.customer_ref_masked if audit is not None else None
        return RecoveryQueueRow(
            txn_id=payment.txn_id,
            amount=float(payment.amount),
            currency=payment.currency,
            root_cause_label=payment.root_cause_label,
            failure_code=payment.failure_code,
            lifecycle_state=lifecycle_state,
            recommendation_kind=rec.kind,
            provider_hint=rec.provider_hint,
            chosen_action=rec.chosen_action,
            bounds=rec.bounds,
            rationale=rec.rationale,
            status=status,
            tier=tier,
            reason_code=reason_code,
            customer_ref_masked=customer_ref_masked,
            last_attempt_at=last_attempt_at,
            updated_at=payment.timestamp,
        )

    def run(
        self,
        *,
        state_filter: set[RecoveryState] | None = None,
        kind_filter: set[RecommendationKind] | None = None,
    ) -> RecoveryQueue:
        payments = self._payment_repository.get_all_payments()
        events = self._audit_repository.all_events()
        audit_by_txn: dict[str, object] = {}
        for e in events:
            if e.txn_id not in audit_by_txn:
                audit_by_txn[e.txn_id] = e
        rows: list[RecoveryQueueRow] = []
        for p in payments:
            row = self.row_for(p, audit_map=audit_by_txn)
            if state_filter is not None and row.lifecycle_state not in state_filter:
                continue
            if kind_filter is not None and row.recommendation_kind not in kind_filter:
                continue
            rows.append(row)
        rows_sorted = sorted(rows, key=lambda r: (r.updated_at, r.txn_id))
        counts_by_state = tuple(
            (s.value, sum(1 for r in rows_sorted if r.lifecycle_state == s)) for s in RecoveryState
        )
        counts_by_kind = tuple(
            (k.value, sum(1 for r in rows_sorted if r.recommendation_kind == k)) for k in RecommendationKind
        )
        return RecoveryQueue(
            rows=tuple(rows_sorted),
            total=len(rows_sorted),
            counts_by_state=counts_by_state,
            counts_by_kind=counts_by_kind,
        )
