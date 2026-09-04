"""P5.1 — Recovery lifecycle state machine. Pure domain, no I/O."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.domain.entities import FailedPaymentEntity
from src.domain.models import Outcome
from src.domain.retry_policy import RetryPolicy
from src.domain.escalation import GracefulFailureHandler


class RecoveryState(str, enum.Enum):
    RECEIVED = "RECEIVED"
    ANALYZED = "ANALYZED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    AUTO_ELIGIBLE = "AUTO_ELIGIBLE"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"
    EXECUTING = "EXECUTING"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"


class InvalidTransitionError(ValueError):
    """Raised when a lifecycle transition is not allowed."""


class HardStopViolation(InvalidTransitionError):
    """Raised when a hard-stop payment attempts a forbidden transition."""


ALLOWED_TRANSITIONS: dict[RecoveryState, set[RecoveryState]] = {
    RecoveryState.RECEIVED: {RecoveryState.ANALYZED},
    RecoveryState.ANALYZED: {
        RecoveryState.PENDING_APPROVAL,
        RecoveryState.AUTO_ELIGIBLE,
        RecoveryState.ESCALATED,
    },
    RecoveryState.PENDING_APPROVAL: {
        RecoveryState.APPROVED,
        RecoveryState.REJECTED,
        RecoveryState.ESCALATED,
    },
    RecoveryState.AUTO_ELIGIBLE: {
        RecoveryState.APPROVED,
        RecoveryState.ESCALATED,
    },
    RecoveryState.APPROVED: {
        RecoveryState.EXECUTING,
        RecoveryState.ESCALATED,
    },
    RecoveryState.EXECUTING: {
        RecoveryState.RECOVERED,
        RecoveryState.FAILED,
        RecoveryState.ESCALATED,
    },
    RecoveryState.REJECTED: set(),
    RecoveryState.ESCALATED: set(),
    RecoveryState.RECOVERED: set(),
    RecoveryState.FAILED: set(),
}

# Hard-stop payments must never enter these targets.
_HARD_STOP_FORBIDDEN_TARGETS: set[RecoveryState] = {
    RecoveryState.AUTO_ELIGIBLE,
    RecoveryState.APPROVED,
    RecoveryState.EXECUTING,
    RecoveryState.RECOVERED,
}


@dataclass(frozen=True)
class RecoveryLifecycle:
    txn_id: str
    state: RecoveryState
    updated_at: datetime
    reason: Optional[str] = None


def _is_hard_stop_payment(
    payment: FailedPaymentEntity,
    retry_policy: RetryPolicy,
    graceful_handler: GracefulFailureHandler,
) -> bool:
    if retry_policy.is_hard_stop(payment.root_cause_label):
        return True
    refusal = graceful_handler.evaluate(
        is_do_not_retry=not payment.recoverable_flag,
        stopping_rule_tripped=False,
    )
    return refusal is not None


def validate_transition(
    current: RecoveryState,
    target: RecoveryState,
    payment: FailedPaymentEntity,
    retry_policy: RetryPolicy | None = None,
    graceful_handler: GracefulFailureHandler | None = None,
    *,
    auto_eligible: bool = False,
) -> None:
    """Pure deterministic validation. Raises on invalid transition."""
    if retry_policy is None:
        retry_policy = RetryPolicy()
    if graceful_handler is None:
        graceful_handler = GracefulFailureHandler()

    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(
            f"Transition {current.value} -> {target.value} is not allowed"
        )

    if target in _HARD_STOP_FORBIDDEN_TARGETS:
        if _is_hard_stop_payment(payment, retry_policy, graceful_handler):
            raise HardStopViolation(
                f"Hard-stop payment {payment.txn_id} cannot transition "
                f"{current.value} -> {target.value}"
            )

    if target == RecoveryState.AUTO_ELIGIBLE and not auto_eligible:
        raise InvalidTransitionError(
            f"Transition {current.value} -> {target.value} requires explicit auto_eligible=True"
        )


def transition(
    lifecycle: RecoveryLifecycle,
    target: RecoveryState,
    payment: FailedPaymentEntity,
    retry_policy: RetryPolicy | None = None,
    graceful_handler: GracefulFailureHandler | None = None,
    *,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
    auto_eligible: bool = False,
) -> RecoveryLifecycle:
    """Pure transition returning a new frozen lifecycle. Does not mutate inputs."""
    if retry_policy is None:
        retry_policy = RetryPolicy()
    if graceful_handler is None:
        graceful_handler = GracefulFailureHandler()
    if now is None:
        now = datetime.utcnow()

    validate_transition(
        lifecycle.state,
        target,
        payment,
        retry_policy,
        graceful_handler,
        auto_eligible=auto_eligible,
    )
    return RecoveryLifecycle(
        txn_id=lifecycle.txn_id,
        state=target,
        updated_at=now,
        reason=reason,
    )


def derive_state(payment: FailedPaymentEntity) -> RecoveryState:
    """Display/backward-compat mapping. Pure, no mutation, no persistence."""
    has_success = any(a.outcome == Outcome.SUCCESS for a in payment.attempts)
    if has_success:
        return RecoveryState.RECOVERED
    if payment.escalations:
        return RecoveryState.ESCALATED
    has_failed = any(a.outcome == Outcome.FAILED for a in payment.attempts)
    if has_failed:
        return RecoveryState.FAILED
    # No terminal evidence -> initial state
    return RecoveryState.RECEIVED


# Alias for backward compatibility
derive_initial_state = derive_state
