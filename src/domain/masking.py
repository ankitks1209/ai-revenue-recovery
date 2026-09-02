"""T6.2 — MaskingPolicy: deterministic MNPI/PII redaction. Pure domain, no I/O."""
from __future__ import annotations

import hashlib
import re


class MaskingPolicy:
    """Redacts customer_ref before it can enter any log or audit sink.

    Deterministic so the same customer maps to the same token across events
    (useful for correlation) without ever exposing raw PII.
    """

    def __init__(self, salt: str = "revenue-recovery-phase3") -> None:
        self._salt = salt

    def mask_customer_ref(self, raw: str) -> str:
        if not raw:
            return "MASKED::empty"
        digest = hashlib.sha256(
            f"{self._salt}:{raw}".encode("utf-8")
        ).hexdigest()[:12]
        return f"MASKED::{digest}"

    def scrub_text(self, text: str) -> str:
        """Defensive scrub for free-text rationales: strip phone/email-like tokens."""
        if not text:
            return ""
        text = re.sub(r"\b\d{10,}\b", "MASKED::num", text)
        text = re.sub(r"[\w.\-]+@[\w.\-]+", "MASKED::email", text)
        return text
