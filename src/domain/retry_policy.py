from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

@dataclass
class PolicyRule:
    category: str
    max_retries: int
    min_interval_seconds: float
    backoff_schedule: Tuple[float, ...]  # backoff delays in seconds for retry indexes 0, 1, 2, ...
    is_hard_stop: bool = False

DEFAULT_RULES = {
    "Insufficient Funds": PolicyRule(
        category="Insufficient Funds",
        max_retries=3,
        min_interval_seconds=48 * 3600.0,  # 48 hours
        backoff_schedule=(0.0, 48 * 3600.0, 48 * 3600.0),
        is_hard_stop=False
    ),
    "Expired Card": PolicyRule(
        category="Expired Card",
        max_retries=2,
        min_interval_seconds=24 * 3600.0,  # min 24h apart
        backoff_schedule=(0.0, 3.5 * 86400.0), # over 7 days
        is_hard_stop=False
    ),
    "Transient/Network": PolicyRule(
        category="Transient/Network",
        max_retries=3,
        min_interval_seconds=0.0,
        backoff_schedule=(0.0, 3600.0, 6 * 3600.0, 24 * 3600.0), # 1h, 6h, 24h
        is_hard_stop=False
    ),
    "Mandate Lapse": PolicyRule(
        category="Mandate Lapse",
        max_retries=1,
        min_interval_seconds=0.0,
        backoff_schedule=(0.0,),
        is_hard_stop=False
    ),
    "Hard Fraud / Do-Not-Retry": PolicyRule(
        category="Hard Fraud / Do-Not-Retry",
        max_retries=0,
        min_interval_seconds=0.0,
        backoff_schedule=(),
        is_hard_stop=True
    ),
    "Unknown / Ambiguous": PolicyRule(
        category="Unknown / Ambiguous",
        max_retries=0,
        min_interval_seconds=0.0,
        backoff_schedule=(),
        is_hard_stop=True
    ),
}

class RetryPolicy:
    def __init__(self, rules=None):
        self.rules = rules if rules is not None else DEFAULT_RULES

    def get_rule(self, category: str) -> PolicyRule:
        return self.rules.get(category, PolicyRule(
            category=category,
            max_retries=0,
            min_interval_seconds=0.0,
            backoff_schedule=(),
            is_hard_stop=True
        ))

    def is_hard_stop(self, category: str) -> bool:
        return self.get_rule(category).is_hard_stop

    def get_effective_delay_seconds(self, category: str, retry_count: int) -> float:
        """
        Calculates MAX(min_interval_seconds, applicable_backoff_seconds) for the given retry count.
        retry_count is the number of previously executed attempts (1 for second attempt timing).
        """
        rule = self.get_rule(category)
        min_interval = rule.min_interval_seconds
        
        if retry_count < len(rule.backoff_schedule):
            backoff = rule.backoff_schedule[retry_count]
        elif rule.backoff_schedule:
            backoff = rule.backoff_schedule[-1]
        else:
            backoff = 0.0

        return max(min_interval, backoff)

    def is_eligible_for_attempt(
        self,
        category: str,
        retry_count: int,
        last_attempt_at: Optional[datetime],
        current_time: datetime
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluates eligibility for execution based on:
        1. Hard stop check
        2. Retry cap check
        3. Timing delay check (MAX(min_interval, backoff))
        """
        rule = self.get_rule(category)

        if rule.is_hard_stop:
            return False, f"Hard stop policy for category: {category}"

        if retry_count >= rule.max_retries:
            return False, f"Max retry count ({rule.max_retries}) reached"

        if retry_count == 0 or last_attempt_at is None:
            # First attempt is eligible immediately
            return True, None

        effective_delay = self.get_effective_delay_seconds(category, retry_count)
        elapsed_seconds = (current_time - last_attempt_at).total_seconds()

        if elapsed_seconds < effective_delay:
            return False, f"Required delay {effective_delay}s not met (elapsed: {elapsed_seconds}s)"

        return True, None
