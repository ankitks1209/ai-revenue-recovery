from __future__ import annotations

import json
from typing import Any, Dict

from groq import Groq

from src.config import GROQ_API_KEY, GROQ_MODEL


class AIDiagnosis:
    """
    AI-assisted payment failure diagnosis.

    The AI explains a failure but never decides or executes
    a payment recovery action.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or GROQ_API_KEY
        self.model = model or GROQ_MODEL

        if not self.api_key:
            raise ValueError("Missing GROQ_API_KEY")

        self.client = Groq(api_key=self.api_key)

    def diagnose(
        self,
        failure_code: str | None,
        root_cause: str | None = None,
        reason: str | None = None,
    ) -> Dict[str, Any]:

        prompt = (
            "You are a payment failure diagnosis assistant.\n"
            "Explain the likely cause of the payment failure.\n"
            "Do NOT decide or authorize any payment action.\n\n"
            f"Failure code: {failure_code or 'unknown'}\n"
            f"Existing root cause: {root_cause or 'unknown'}\n"
            f"Failure details: {reason or 'not provided'}\n\n"
            "Return ONLY a JSON object with exactly these keys:\n"
            "diagnosis, confidence, explanation\n\n"
            "confidence must be exactly one of: high, medium, low.\n"
            "All values must be strings."
        )

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                temperature=0,
                max_completion_tokens=500,
                reasoning_effort="none",
                reasoning_format="hidden",
                response_format={"type": "json_object"},
            )

            content = completion.choices[0].message.content or "{}"
            result = json.loads(content)

            confidence = str(result.get("confidence", "low")).lower()

            if confidence not in {"high", "medium", "low"}:
                confidence = "low"

            return {
                "diagnosis": str(result.get("diagnosis", "Unknown")),
                "confidence": confidence,
                "explanation": str(
                    result.get(
                        "explanation",
                        "No explanation returned.",
                    )
                ),
                "model": self.model,
                "source": "groq",
            }

        except Exception as exc:
            # AI is advisory only. A provider failure must never
            # interrupt payment recovery or policy execution.
            return {
                "diagnosis": root_cause or "Unknown / Ambiguous",
                "confidence": "low",
                "explanation": (
                    "AI diagnosis temporarily unavailable; "
                    "using the existing deterministic failure classification."
                ),
                "model": self.model,
                "source": "fallback",
                "error": type(exc).__name__,
            }