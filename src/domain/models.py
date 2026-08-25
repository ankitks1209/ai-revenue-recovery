from dataclasses import dataclass
from enum import Enum
from typing import Optional

class Outcome(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ESCALATED = "ESCALATED"

@dataclass(frozen=True)
class Decision:
    category: str
    chosen_action: str
    bounds: str

@dataclass(frozen=True)
class RailResponse:
    success: bool
    error_message: Optional[str] = None
    gateway_reference: Optional[str] = None
