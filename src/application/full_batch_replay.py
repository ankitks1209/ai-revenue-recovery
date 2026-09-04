"""
Phase 4 Day 9 T9.1 — FullBatchReplay: Complete Phases 1-3 Pipeline Reproducibility

Re-runs the entire pipeline with isolated state to produce a deterministic
reproducibility fingerprint.

Orchestrates:
1. Phase 1: Fixed-seed data generation
2. Phase 2: ExecuteRecoveryBatch with bounded retry logic
3. Phase 3: Audit trail emission and report generation
4. Fingerprint computation from deterministic aggregates

All components use isolated in-memory databases for zero state leakage.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict
from collections import Counter

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.database import Base, FailedPayment as FailedPaymentModel
from src.generator import generate_failed_payments
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.infrastructure.mock_payment_rail import MockPaymentRail
from src.infrastructure.clock import SimulatedClock
from src.infrastructure.audit_repository import AuditBase, AuditLogRepository
from src.application.execute_recovery_batch import ExecuteRecoveryBatch
from src.application.generate_audit_report import GenerateAuditReport


@dataclass(frozen=True)
class ReplayFingerprint:
    """
    Deterministic reproducibility fingerprint.
    
    Includes only deterministic aggregates that prove pipeline reproducibility:
    - Execution stats from ExecuteRecoveryBatch
    - Money metrics from SQL reconciliation (Phase 2 semantics)
    - Audit metrics from GenerateAuditReport
    
    Explicitly excludes non-deterministic elements:
    - Individual event_id UUIDs (generated via uuid.uuid4())
    - Absolute timestamp values (SimulatedClock used but not fingerprinted)
    - Individual txn_id values (randomized by Faker)
    """
    seed: int
    batch_size: int
    total_processed: int
    executed_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    escalated_count: int
    money_recovered: float
    recoverable_denominator: float
    audit_event_count: int
    tier_counts: Dict[str, int]  # T1/T2/T3 distribution
    outcome_counts: Dict[str, int]  # recovered/failed/escalated/skipped


class FullBatchReplay:
    """
    Re-run complete Phases 1-3 pipeline with isolated state.
    
    Produces deterministic fingerprint for reproducibility verification.
    Each execution creates completely independent in-memory databases,
    ensuring zero state leakage between runs.
    """
    
    def __init__(
        self,
        seed: int = 42,
        count: int = 60,
        initial_time: datetime | None = None,
        payments_db_url: str | None = None,
        audit_db_url: str | None = None,
    ):
        """
        Initialize replay with reproducibility parameters.
        
        Args:
            seed: Random seed for data generation AND MockPaymentRail
            count: Number of payments to generate (must be >= 50 per Phase 4 spec)
            initial_time: Fixed time for SimulatedClock (defaults to 2026-01-01 10:00:00)
        
        Raises:
            ValueError: If count < 50 (Phase 4 contract violation)
        """
        if count < 50:
            raise ValueError(
                f"Phase 4 requires at least 50 records for reproducibility proof. Got: {count}"
            )
        
        self.seed = seed
        self.count = count
        self.initial_time = initial_time or datetime(2026, 1, 1, 10, 0, 0)
        self.payments_db_url = payments_db_url
        self.audit_db_url = audit_db_url
    
    def execute(self) -> ReplayFingerprint:
        """
        Execute complete Phases 1-3 pipeline and return reproducibility fingerprint.
        
        Pipeline steps:
        1. Create isolated in-memory SQLite engines (payments + audit)
        2. Generate fixed-seed dataset via generate_failed_payments()
        3. Persist to failed_payments table
        4. Wire all dependencies with deterministic components
        5. Execute recovery batch
        6. Generate audit report
        7. Compute money metrics via SQL reconciliation (Phase 2 pattern)
        8. Build fingerprint from deterministic aggregates
        
        Returns:
            ReplayFingerprint with all deterministic metrics
        """
        # ============================================================
        # 1. CREATE ISOLATED IN-MEMORY DATABASES
        # ============================================================
        
        # Payments database (failed_payments, recovery_attempts, escalations)
        # Default :memory: preserves existing fingerprint/determinism; optional URLs enable demo seeding.
        _payments_url = self.payments_db_url or "sqlite:///:memory:"
        _audit_url = self.audit_db_url or "sqlite:///:memory:"
        # Fresh state for file-backed demo runs: drop then create (idempotent, no duplication on re-seed)
        if _payments_url != "sqlite:///:memory:":
            _tmp = create_engine(_payments_url, echo=False)
            Base.metadata.drop_all(bind=_tmp)
            _tmp.dispose()
        payments_engine = create_engine(_payments_url, echo=False)
        Base.metadata.create_all(bind=payments_engine)
        PaymentsSessionLocal = sessionmaker(
            bind=payments_engine,
            autoflush=False,
            autocommit=False
        )
        
        # ============================================================
        # 2. PHASE 1: GENERATE AND PERSIST FIXED-SEED DATA
        # ============================================================
        
        # Generate records using existing Phase 1 generator
        records = generate_failed_payments(count=self.count, seed=self.seed)
        
        # Persist to database
        session = PaymentsSessionLocal()
        try:
            db_objects = [FailedPaymentModel(**record) for record in records]
            session.add_all(db_objects)
            session.commit()
        finally:
            session.close()
        
        # ============================================================
        # 3. WIRE DEPENDENCIES WITH DETERMINISTIC COMPONENTS
        # ============================================================
        
        # Repository using isolated payments database
        repo = SQLiteFailedPaymentRepository(session_factory=PaymentsSessionLocal)
        
        # Payment rail with SAME seed for deterministic hash-based outcomes
        # MockPaymentRail uses SHA256(seed:txn_id:attempt_number) for determinism
        rail = MockPaymentRail(seed=self.seed)
        
        # Simulated clock with fixed time (no advancement during execution)
        clock = SimulatedClock(initial_time=self.initial_time)
        
        # Audit repository — file-backed when demo URLs supplied, else isolated
        if _audit_url != "sqlite:///:memory:":
            _tmp_a = create_engine(_audit_url, echo=False)
            AuditBase.metadata.drop_all(bind=_tmp_a)
            _tmp_a.dispose()
        audit_repo = AuditLogRepository(db_url=_audit_url)
        
        # ============================================================
        # 4. PHASE 2: EXECUTE RECOVERY BATCH
        # ============================================================
        
        executor = ExecuteRecoveryBatch(
            repository=repo,
            payment_rail=rail,
            clock=clock,
            audit_repository=audit_repo
            # Uses default PolicyEngine, RetryPolicy, etc.
        )
        
        batch_stats = executor.execute()
        
        # ============================================================
        # 5. PHASE 3: GENERATE AUDIT REPORT
        # ============================================================
        
        report_gen = GenerateAuditReport(audit_repository=audit_repo)
        audit_report = report_gen.run()
        
        # ============================================================
        # 6. COMPUTE MONEY METRICS (Phase 2 SQL reconciliation pattern)
        # ============================================================
        
        with payments_engine.connect() as conn:
            # Money recovered: sum of amounts from successful txn_ids
            # Follows exact pattern from test_e2e_pipeline.py lines 180-199
            result = conn.execute(
                text(
                    """
                    SELECT SUM(amount)
                    FROM failed_payments
                    WHERE txn_id IN (
                        SELECT DISTINCT txn_id
                        FROM recovery_attempts
                        WHERE outcome = 'SUCCESS'
                    )
                    """
                )
            )
            money_recovered = result.scalar() or 0.0
            
            # Recoverable denominator: ground truth from Phase 1
            result = conn.execute(
                text(
                    """
                    SELECT SUM(amount)
                    FROM failed_payments
                    WHERE recoverable_flag = 1
                    """
                )
            )
            recoverable_denominator = result.scalar() or 0.0
        
        # ============================================================
        # 7. BUILD OUTCOME COUNTS FROM AUDIT EVENTS
        # ============================================================
        
        audit_events = audit_repo.all_events()
        outcome_counts = dict(Counter(e.outcome.value for e in audit_events))
        
        # ============================================================
        # 8. CLEANUP: DISPOSE PAYMENTS ENGINE
        # ============================================================
        
        payments_engine.dispose()
        
        # ============================================================
        # 9. RETURN DETERMINISTIC FINGERPRINT
        # ============================================================
        
        return ReplayFingerprint(
            seed=self.seed,
            batch_size=self.count,
            total_processed=batch_stats["total_processed"],
            executed_count=batch_stats["executed_count"],
            success_count=batch_stats["success_count"],
            failed_count=batch_stats["failed_count"],
            skipped_count=batch_stats["skipped_count"],
            escalated_count=batch_stats["escalated_count"],
            money_recovered=round(money_recovered, 2),
            recoverable_denominator=round(recoverable_denominator, 2),
            audit_event_count=len(audit_events),
            tier_counts=audit_report["escalation_summary"]["tier_counts"],
            outcome_counts=outcome_counts
        )
