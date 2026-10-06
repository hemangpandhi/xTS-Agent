"""Optional LLM/Gemini/Llama.cpp-powered RCA with RAG over OEM Code."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .rca_engine import FailureType
from xts_agent.config_loader import AiRcaConfig
from .ai_triage import AITriageEngine

logger = logging.getLogger(__name__)

@dataclass
class AIAnalysisResult:
    root_cause: str
    classification: FailureType
    confidence: float
    suggested_action: str
    reasoning: str


class AIAnalyzer:
    def __init__(self, ai_rca_config: AiRcaConfig):
        self.config = ai_rca_config
        self.triage_engine = AITriageEngine(ai_rca_config) if ai_rca_config.enabled else None

    def analyze_failure(
        self, test_id: str, stack_trace: str, logcat_excerpt: str
    ) -> AIAnalysisResult:
        if not self.triage_engine:
            return self._fallback(test_id, stack_trace)

        try:
            logger.info("Executing advanced AI Triage (RAG) for %s", test_id)
            rca_text = self.triage_engine.triage_failure(test_id, stack_trace)
            
            # Use basic heuristics to determine classification if not structured perfectly
            cls = FailureType.PRODUCT_BUG
            if "flaky" in rca_text.lower():
                cls = FailureType.FLAKY_TEST
            elif "infrastructure" in rca_text.lower() or "adb" in rca_text.lower():
                cls = FailureType.INFRASTRUCTURE_FAILURE
                
            return AIAnalysisResult(
                root_cause=f"AI Identified Cause for {test_id}",
                classification=cls,
                confidence=0.9,
                suggested_action="Review AI Triage Output",
                reasoning=rca_text,
            )
        except Exception as exc:
            logger.warning("AI RCA call failed, using fallback: %s", exc)

        return self._fallback(test_id, stack_trace)

    def _fallback(self, test_id: str, stack_trace: str) -> AIAnalysisResult:
        return AIAnalysisResult(
            root_cause=f"Unresolved failure in {test_id}",
            classification=FailureType.PRODUCT_BUG,
            confidence=0.4,
            suggested_action="Review TradeFed logs and device diagnostics",
            reasoning=(stack_trace or "")[:500],
        )
