import hashlib
from typing import Dict, Optional
from src.domain.models import RailResponse
from src.infrastructure.ports import PaymentRailPort

class MockPaymentRail(PaymentRailPort):
    def __init__(self, seed: int = 42, default_success_rate: float = 60.0):
        self.seed = seed
        self.default_success_rate = default_success_rate
        self.fixtures: Dict[str, bool] = {}
        self.force_global: Optional[bool] = None

    def set_fixture(self, txn_id: str, success: bool):
        """Sets an explicit deterministic outcome for a specific transaction."""
        self.fixtures[txn_id] = success

    def force_all(self, success: Optional[bool]):
        """Forces all non-fixtured (or all) attempts to return the specified success outcome."""
        self.force_global = success

    def clear_fixtures(self):
        """Clears all configured fixtures and global overrides."""
        self.fixtures.clear()
        self.force_global = None

    def execute_attempt(self, txn_id: str, amount: float, action_type: str, attempt_number: int) -> RailResponse:
        """
        Executes a payment rail attempt.
        1. Checks global forced override if set.
        2. Checks explicit fixture for txn_id if set.
        3. Falls back to deterministic seeded hash calculation.
        """
        if self.force_global is not None:
            is_success = self.force_global
        elif txn_id in self.fixtures:
            is_success = self.fixtures[txn_id]
        else:
            # Deterministic hash function using seed, txn_id, and attempt_number
            hash_input = f"{self.seed}:{txn_id}:{attempt_number}".encode("utf-8")
            hash_val = int(hashlib.sha256(hash_input).hexdigest(), 16)
            score = hash_val % 100
            is_success = score < self.default_success_rate

        gateway_ref = f"MOCK_GW_{txn_id}_{attempt_number}"

        if is_success:
            return RailResponse(
                success=True,
                gateway_reference=gateway_ref
            )
        else:
            return RailResponse(
                success=False,
                error_message="Mock rail declined transaction",
                gateway_reference=gateway_ref
            )
