from datetime import datetime, timedelta
import pytest
from src.domain.retry_policy import RetryPolicy
from src.domain.entities import FailedPaymentEntity, RecoveryAttempt
from src.domain.models import Outcome

def test_retry_policy_hard_stop():
    policy = RetryPolicy()
    assert policy.is_hard_stop("Hard Fraud / Do-Not-Retry") is True
    assert policy.is_hard_stop("Unknown / Ambiguous") is True
    assert policy.is_hard_stop("Insufficient Funds") is False

def test_effective_delay_calculation():
    policy = RetryPolicy()
    # Transient/Network: min_interval = 0, backoff = 0 for index 0, 3600 for index 1
    delay_0 = policy.get_effective_delay_seconds("Transient/Network", 0)
    delay_1 = policy.get_effective_delay_seconds("Transient/Network", 1)
    assert delay_0 == 0.0
    assert delay_1 == 3600.0

    # Insufficient Funds: min_interval = 48h (172800s)
    delay_insuff = policy.get_effective_delay_seconds("Insufficient Funds", 0)
    assert delay_insuff == 172800.0

def test_first_attempt_eligibility():
    policy = RetryPolicy()
    now = datetime(2026, 1, 1, 12, 0, 0)
    # retry_count = 0, last_attempt_at = None
    eligible, reason = policy.is_eligible_for_attempt("Transient/Network", 0, None, now)
    assert eligible is True
    assert reason is None

def test_subsequent_attempt_eligibility_and_backoff():
    policy = RetryPolicy()
    last_time = datetime(2026, 1, 1, 10, 0, 0)
    now_early = datetime(2026, 1, 1, 10, 30, 0) # 30 mins elapsed
    now_later = datetime(2026, 1, 1, 12, 0, 0) # 2 hours elapsed

    # For Transient/Network at retry_count = 1, required backoff is 1h (3600s)
    eligible_early, _ = policy.is_eligible_for_attempt("Transient/Network", 1, last_time, now_early)
    assert eligible_early is False

    eligible_later, _ = policy.is_eligible_for_attempt("Transient/Network", 1, last_time, now_later)
    assert eligible_later is True

def test_failed_payment_entity_retry_count_semantics():
    now = datetime(2026, 1, 1, 10, 0, 0)
    payment = FailedPaymentEntity(
        txn_id="TXN1",
        customer_id="C1",
        amount=100.0,
        currency="INR",
        failure_code="ERR",
        root_cause_label="Transient/Network",
        recoverable_flag=True,
        retry_count=0,
        timestamp=now,
        payment_method="card"
    )

    assert payment.executed_retry_count == 0
    assert payment.last_executed_attempt_at is None

    # Add a SKIPPED attempt (should not increment executed_retry_count)
    payment.attempts.append(
        RecoveryAttempt(txn_id="TXN1", attempt_number=1, outcome=Outcome.SKIPPED, timestamp=now, reason="Backoff active")
    )
    assert payment.executed_retry_count == 0

    # Add a FAILED attempt (increments executed_retry_count by 1)
    payment.attempts.append(
        RecoveryAttempt(txn_id="TXN1", attempt_number=1, outcome=Outcome.FAILED, timestamp=now, reason="Declined")
    )
    assert payment.executed_retry_count == 1
    assert payment.last_executed_attempt_at == now
