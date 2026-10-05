"""Optional LLM/Gemini-powered RCA (fallback when API unavailable)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .rca_engine import FailureType

logger = logging.getLogger(__name__)


@dataclass
class AIAnalysisResult:
    root_cause: str
    classification: FailureType
    confidence: float
    suggested_action: str
    reasoning: str


class AIAnalyzer:
    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.api_key = api_key
        self.model = model

    def analyze_failure(
        self, test_id: str, stack_trace: str, logcat_excerpt: str
    ) -> AIAnalysisResult:
        if not self.api_key:
            return self._fallback(test_id, stack_trace)

        # Optional dependency — keep agent usable without google-genai installed
        try:
            import json
            import urllib.request

            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent?key={self.api_key}"
            )
            prompt = (
                "Classify this Android xTS failure. Reply JSON with keys "
                "root_cause, classification (PRODUCT_BUG|FLAKY_TEST|"
                "INFRASTRUCTURE_FAILURE|ENVIRONMENT_ISSUE|TEST_BUG), "
                "confidence (0-1), suggested_action, reasoning.\n"
                f"test_id={test_id}\nstack={stack_trace[:3000]}\n"
                f"logcat={logcat_excerpt[:2000]}"
            )
            body = json.dumps(
                {"contents": [{"parts": [{"text": prompt}]}]}
            ).encode("utf-8")
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            text = (
                payload.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            # Extract JSON object if present
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(text[start : end + 1])
                cls_name = str(data.get("classification", "PRODUCT_BUG")).upper()
                classification = (
                    FailureType[cls_name]
                    if cls_name in FailureType.__members__
                    else FailureType.PRODUCT_BUG
                )
                return AIAnalysisResult(
                    root_cause=str(data.get("root_cause", "Unknown")),
                    classification=classification,
                    confidence=float(data.get("confidence", 0.5)),
                    suggested_action=str(data.get("suggested_action", "Review logs")),
                    reasoning=str(data.get("reasoning", "")),
                )
        except Exception as exc:
            logger.warning("Gemini RCA call failed, using fallback: %s", exc)

        return self._fallback(test_id, stack_trace)

    def _fallback(self, test_id: str, stack_trace: str) -> AIAnalysisResult:
        return AIAnalysisResult(
            root_cause=f"Unresolved failure in {test_id}",
            classification=FailureType.PRODUCT_BUG,
            confidence=0.4,
            suggested_action="Review TradeFed logs and device diagnostics",
            reasoning=(stack_trace or "")[:500],
        )
