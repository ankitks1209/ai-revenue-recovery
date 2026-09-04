"""
Phase 4 Day 9 T9.2 — FullBatchReplay Reproducibility Tests

Proves that the complete Phases 1-3 pipeline is deterministic and reproducible
across independent runs with isolated state.
"""

import pytest
from src.application.full_batch_replay import FullBatchReplay


def test_count_below_50_raises_contract_violation():
    """
    Phase 4 Contract: FullBatchReplay must reject count < 50.
    
    The Phase 4 spec requires at least 50 records to demonstrate
    meaningful reproducibility over a realistic data volume.
    """
    with pytest.raises(ValueError, match="Phase 4 requires at least 50 records"):
        FullBatchReplay(seed=42, count=49)
    
    with pytest.raises(ValueError, match="Phase 4 requires at least 50 records"):
        FullBatchReplay(seed=42, count=0)
    
    # Exactly 50 should work
    replay = FullBatchReplay(seed=42, count=50)
    assert replay.count == 50


def test_two_independent_runs_produce_identical_fingerprints():
    """
    T9.2 Core Requirement: Reproducibility proof via independent pipeline re-runs.
    
    Scenario:
    1. Run FullBatchReplay with seed=42, count=60
    2. Run again with same parameters (completely fresh state: new engines, sessions, repos)
    3. Assert fingerprints are byte-for-byte identical
    
    Verifies:
    - generate_failed_payments(seed=42) produces identical category distribution
    - MockPaymentRail(seed=42) produces identical outcomes for same txn_ids
    - ExecuteRecoveryBatch produces identical execution stats
    - AuditLogRepository captures identical event counts and distributions
    - No state leakage between runs (Faker.unique, numpy random state, etc.)
    
    Critical: This test proves the ENTIRE Phases 1-3 pipeline is reproducible.
    """
    
    # Run 1: First independent execution
    replay1 = FullBatchReplay(seed=42, count=60)
    fingerprint1 = replay1.execute()
    
    # Run 2: Second independent execution (completely fresh state)
    replay2 = FullBatchReplay(seed=42, count=60)
    fingerprint2 = replay2.execute()
    
    # Assert complete dataclass equality (all 13 fields)
    assert fingerprint1 == fingerprint2, (
        f"Fingerprints must be identical across independent runs.\n"
        f"Run1: {fingerprint1}\n"
        f"Run2: {fingerprint2}\n"
        f"Differences indicate non-deterministic behavior in pipeline."
    )
    
    # Explicit field-by-field validations for debugging clarity
    assert fingerprint1.seed == fingerprint2.seed == 42
    assert fingerprint1.batch_size == fingerprint2.batch_size == 60
    assert fingerprint1.total_processed == fingerprint2.total_processed == 60
    assert fingerprint1.executed_count == fingerprint2.executed_count
    assert fingerprint1.success_count == fingerprint2.success_count
    assert fingerprint1.failed_count == fingerprint2.failed_count
    assert fingerprint1.skipped_count == fingerprint2.skipped_count
    assert fingerprint1.escalated_count == fingerprint2.escalated_count
    
    # Float comparison with tight tolerance
    assert abs(fingerprint1.money_recovered - fingerprint2.money_recovered) < 1e-6
    assert abs(fingerprint1.recoverable_denominator - fingerprint2.recoverable_denominator) < 1e-6
    
    # Audit metrics
    assert fingerprint1.audit_event_count == fingerprint2.audit_event_count == 60
    assert fingerprint1.tier_counts == fingerprint2.tier_counts
    assert fingerprint1.outcome_counts == fingerprint2.outcome_counts
    
    # Additional smoke tests
    assert fingerprint1.executed_count > 0, "At least some payments must execute rail attempts"
    assert fingerprint1.money_recovered > 0.0, "At least some money must be recovered"
    assert fingerprint1.recoverable_denominator > 0.0, "Ground truth denominator must be non-zero"


def test_different_seeds_produce_different_fingerprints():
    """
    Guard against false determinism (ensures test isn't trivially passing).
    
    Different seeds MUST produce different data distributions from generate_failed_payments(),
    which SHOULD (with high probability) produce different execution outcomes.
    
    We assert differences only in metrics that generate_failed_payments() demonstrably changes:
    - Category distribution (affects execution paths)
    - Amount distribution (affects money_recovered)
    
    We do NOT assert differences in non-deterministic fields like UUIDs or timestamps.
    """
    
    replay1 = FullBatchReplay(seed=42, count=60)
    fingerprint1 = replay1.execute()
    
    replay2 = FullBatchReplay(seed=99, count=60)
    fingerprint2 = replay2.execute()
    
    # Seeds must differ
    assert fingerprint1.seed == 42
    assert fingerprint2.seed == 99
    
    # Batch sizes are the same (same count parameter)
    assert fingerprint1.batch_size == fingerprint2.batch_size == 60
    assert fingerprint1.total_processed == fingerprint2.total_processed == 60
    
    # At least one deterministic execution metric should differ
    # (Different seeds → different categories → different execution paths)
    metrics_differ = (
        fingerprint1.executed_count != fingerprint2.executed_count or
        fingerprint1.success_count != fingerprint2.success_count or
        fingerprint1.failed_count != fingerprint2.failed_count or
        abs(fingerprint1.money_recovered - fingerprint2.money_recovered) > 1e-6 or
        fingerprint1.tier_counts != fingerprint2.tier_counts or
        fingerprint1.outcome_counts != fingerprint2.outcome_counts
    )
    
    assert metrics_differ, (
        f"Different seeds produced identical deterministic metrics — test may be broken.\n"
        f"Seed 42: {fingerprint1}\n"
        f"Seed 99: {fingerprint2}\n"
        f"Expected at least one execution metric to differ due to different data distributions."
    )


def test_fingerprint_proves_50plus_records():
    """
    Phase 4 Spec Requirement: Batch must process 50+ records.
    
    This proves the demo dataset is not trivially small and exercises
    the full pipeline over a realistic data volume.
    """
    
    replay = FullBatchReplay(seed=42, count=60)
    fingerprint = replay.execute()
    
    # Phase 4 requirement: 50+ records
    assert fingerprint.batch_size >= 50, "Phase 4 requires at least 50 records"
    assert fingerprint.total_processed >= 50, "Phase 4 requires at least 50 records processed"
    
    # Actual values with default parameters
    assert fingerprint.batch_size == 60
    assert fingerprint.total_processed == 60
    
    # Audit event reconciliation: every payment emits exactly one event
    assert fingerprint.audit_event_count == 60, (
        "Every payment must emit exactly one audit event (Phase 3 requirement)"
    )
    
    # Pipeline executed successfully
    assert fingerprint.executed_count > 0, "At least some payments must execute rail attempts"
    assert fingerprint.success_count >= 0, "Success count must be non-negative"
    assert fingerprint.failed_count >= 0, "Failed count must be non-negative"
    assert fingerprint.skipped_count >= 0, "Skipped count must be non-negative"
    assert fingerprint.escalated_count >= 0, "Escalated count must be non-negative"
    
    # Money metrics are valid
    assert fingerprint.money_recovered >= 0.0, "Money recovered must be non-negative"
    assert fingerprint.recoverable_denominator > 0.0, (
        "Ground truth denominator must be positive (at least some payments are recoverable)"
    )
    
    # Tier distribution is non-empty
    assert len(fingerprint.tier_counts) > 0, "Tier counts must be non-empty"
    assert all(tier in ["T1", "T2", "T3"] for tier in fingerprint.tier_counts.keys()), (
        "Tier counts must only contain T1, T2, T3"
    )
    
    # Outcome distribution is non-empty
    assert len(fingerprint.outcome_counts) > 0, "Outcome counts must be non-empty"
    assert all(
        outcome in ["recovered", "failed", "skipped", "escalated"]
        for outcome in fingerprint.outcome_counts.keys()
    ), "Outcome counts must only contain valid outcomes"
