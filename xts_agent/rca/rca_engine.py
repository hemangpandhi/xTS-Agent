"""RCA orchestrator."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional

from xts_agent.results.result_parser import ResultParser, TestResults

logger = logging.getLogger(__name__)


class FailureType(Enum):
    PRODUCT_BUG = auto()
    FLAKY_TEST = auto()
    INFRASTRUCTURE_FAILURE = auto()
    ENVIRONMENT_ISSUE = auto()
    TEST_BUG = auto()


@dataclass
class FailureAnalysis:
    test_id: str
    classification: FailureType
    confidence: float
    root_cause: str
    evidence: str
    suggested_action: str
    related_failures: List[str] = field(default_factory=list)


@dataclass
class RCAReport:
    failures: List[FailureAnalysis] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)


class RCAEngine:
    def __init__(
        self,
        config: Any,
        diagnostic_collector: Any = None,
        failure_classifier: Any = None,
        log_analyzer: Any = None,
        pattern_matcher: Any = None,
        ai_analyzer: Any = None,
    ):
        self.config = config
        self.diagnostic_collector = diagnostic_collector
        self.failure_classifier = failure_classifier
        self.log_analyzer = log_analyzer
        self.pattern_matcher = pattern_matcher
        self.ai_analyzer = ai_analyzer

    def analyze_failures(
        self,
        test_results: TestResults,
        device_serials: Optional[List[str]] = None,
    ) -> RCAReport:
        report = RCAReport()
        parser = ResultParser()
        failed = parser.get_failed_tests(test_results)
        device_serials = device_serials or []

        # Probe device state once per analysis, not once per failing test
        device_state: dict = {}
        if self.diagnostic_collector and device_serials and failed:
            try:
                device_state = self.diagnostic_collector.collect_device_state(device_serials[0])
            except Exception as exc:
                logger.debug("Diagnostic collection skipped: %s", exc)

        for tc in failed:
            test_id = tc.test_id
            classification = FailureType.PRODUCT_BUG
            confidence = 0.4
            root_cause = tc.rca_category or "Unknown failure"
            evidence = (tc.stack_trace or tc.message or "")[:2000]
            suggested = tc.rca_recommendation or "Inspect TradeFed and device logs"

            logcat = ""
            matched = None
            if self.pattern_matcher:
                matched = self.pattern_matcher.match(tc.message or "", tc.stack_trace or "")
                if matched:
                    classification = matched.classification
                    confidence = 0.85
                    root_cause = matched.description
                    suggested = getattr(matched, "suggested_action", None) or suggested

            # Curated patterns win; rules only fill in when nothing matched
            if self.failure_classifier and matched is None:
                try:
                    ruled = self.failure_classifier.classify(tc, logcat, device_state)
                    if ruled is not None:
                        classification = ruled
                        confidence = 0.6
                except Exception as exc:
                    logger.debug("Classifier failed: %s", exc)

            # The orchestrator only builds an analyzer when ai_rca is enabled
            if self.ai_analyzer:
                try:
                    ai = self.ai_analyzer.analyze_failure(
                        test_id, tc.stack_trace or "", logcat
                    )
                    if ai and ai.confidence >= confidence:
                        classification = ai.classification
                        confidence = ai.confidence
                        root_cause = ai.root_cause
                        suggested = ai.suggested_action
                except Exception as exc:
                    logger.debug("AI analyzer skipped: %s", exc)

            report.failures.append(
                FailureAnalysis(
                    test_id=test_id,
                    classification=classification,
                    confidence=confidence,
                    root_cause=root_cause,
                    evidence=evidence,
                    suggested_action=suggested,
                )
            )

        for failure in report.failures:
            key = failure.classification.name
            report.summary[key] = report.summary.get(key, 0) + 1

        # Top recommendations by frequency
        for cls_name, count in sorted(
            report.summary.items(), key=lambda kv: kv[1], reverse=True
        ):
            report.recommendations.append(
                f"{count} failure(s) classified as {cls_name}"
            )

        return report

    def analyze_plan_results(self, plan_result: Any) -> RCAReport:
        """Aggregate RCA across all suite details in a PlanResult."""
        combined = RCAReport()
        for suite_name, suite_res in (plan_result.suites_results or {}).items():
            if suite_res.details is None:
                continue
            partial = self.analyze_failures(
                suite_res.details, suite_res.device_serials or plan_result.device_serials
            )
            for failure in partial.failures:
                failure.related_failures.append(suite_name)
            combined.failures.extend(partial.failures)
            for k, v in partial.summary.items():
                combined.summary[k] = combined.summary.get(k, 0) + v
            combined.recommendations.extend(partial.recommendations)
        return combined
