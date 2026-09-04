"""T10.2 — BuildDashboardData: reads repos, delegates aggregation. No business rules."""

from __future__ import annotations

from src.domain.metrics import DashboardMetrics, MetricsAggregator
from src.infrastructure.audit_repository import AuditLogRepository
from src.infrastructure.ports import FailedPaymentRepositoryPort


class BuildDashboardData:
    """Application-layer orchestrator. No metric calculations, no rendering."""

    def __init__(
        self,
        payment_repository: FailedPaymentRepositoryPort,
        audit_repository: AuditLogRepository,
        metrics_aggregator: MetricsAggregator,
    ) -> None:
        self._payment_repository = payment_repository
        self._audit_repository = audit_repository
        self._metrics_aggregator = metrics_aggregator

    def run(self) -> DashboardMetrics:
        payments = self._payment_repository.get_all_payments()
        audit_events = self._audit_repository.all_events()
        return self._metrics_aggregator.compute(audit_events=audit_events, payments=payments)
