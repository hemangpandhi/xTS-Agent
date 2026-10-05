"""Parser for TradeFed test_result.xml."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TestCaseResult:
    class_name: str
    test_name: str
    result: str
    message: Optional[str] = None
    stack_trace: Optional[str] = None
    duration: Optional[int] = None
    rca_category: Optional[str] = None
    rca_recommendation: Optional[str] = None


@dataclass
class ModuleResult:
    name: str
    done: bool
    pass_count: int
    fail_count: int
    runtime: int
    test_cases: List[TestCaseResult] = field(default_factory=list)


@dataclass
class TestResults:
    suite_name: str
    device_info: Dict[str, str]
    start_time: str
    end_time: str
    modules: List[ModuleResult] = field(default_factory=list)
    summary: Dict[str, int] = field(
        default_factory=lambda: {"pass": 0, "fail": 0, "skip": 0, "error": 0}
    )


class ResultParser:
    """Parses TradeFed XML results."""

    def parse_xml(self, xml_path: str | Path) -> TestResults:
        xml_path = Path(xml_path)
        if not xml_path.exists():
            raise FileNotFoundError(f"Result file not found: {xml_path}")

        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.error("Failed to parse XML %s: %s", xml_path, e)
            raise

        results = TestResults(
            suite_name=root.attrib.get("suite_name", "Unknown"),
            device_info={},
            start_time=root.attrib.get("start_display", ""),
            end_time=root.attrib.get("end_display", ""),
        )

        build_info = root.find("Build")
        if build_info is not None:
            for k, v in build_info.attrib.items():
                results.device_info[k] = v

        summary = root.find("Summary")
        if summary is not None:
            results.summary["pass"] = int(summary.attrib.get("pass", 0))
            results.summary["fail"] = int(
                summary.attrib.get("failed", summary.attrib.get("fail", 0))
            )
            # Prefer explicit skipped/ignored attributes; never use modules_done
            results.summary["skip"] = int(
                summary.attrib.get(
                    "skipped",
                    summary.attrib.get(
                        "ignored",
                        summary.attrib.get("assumption_failure", 0),
                    ),
                )
            )
            results.summary["error"] = int(summary.attrib.get("error", 0))

        pass_from_cases = fail_from_cases = skip_from_cases = error_from_cases = 0

        for mod in root.findall("Module"):
            mod_res = ModuleResult(
                name=mod.attrib.get("name", "Unknown"),
                done=mod.attrib.get("done", "true").lower() == "true",
                pass_count=int(mod.attrib.get("pass", 0)),
                fail_count=int(mod.attrib.get("fail", 0)),
                runtime=int(mod.attrib.get("runtime", 0) or 0),
            )
            for tc in mod.findall(".//TestCase"):
                for t in tc.findall("Test"):
                    status = (t.attrib.get("result") or "PASS").upper()
                    fail = t.find("Failure")
                    msg = None
                    stack = None
                    rca_category = None
                    rca_recommendation = None
                    if fail is not None:
                        status = "FAIL"
                        msg = fail.attrib.get("message")
                        stack_elem = fail.find("StackTrace")
                        if stack_elem is not None:
                            stack = stack_elem.text or ""
                            rca_category, rca_recommendation = self._inline_rca(stack)

                    if status in ("PASS", "PASSED"):
                        status = "PASS"
                        pass_from_cases += 1
                    elif status in ("FAIL", "FAILED", "FAILURE"):
                        status = "FAIL"
                        fail_from_cases += 1
                    elif status in ("ERROR",):
                        error_from_cases += 1
                    else:
                        # IGNORED / ASSUMPTION_FAILURE / SKIP
                        status = "SKIP"
                        skip_from_cases += 1

                    mod_res.test_cases.append(
                        TestCaseResult(
                            class_name=tc.attrib.get("name", ""),
                            test_name=t.attrib.get("name", ""),
                            result=status,
                            message=msg,
                            stack_trace=stack,
                            rca_category=rca_category,
                            rca_recommendation=rca_recommendation,
                        )
                    )
            results.modules.append(mod_res)

        # If Summary omitted skip counts but cases were enumerated, prefer case totals
        case_total = pass_from_cases + fail_from_cases + skip_from_cases + error_from_cases
        summary_total = sum(results.summary.values())
        if case_total > 0 and (
            summary_total == 0
            or (
                results.summary.get("skip", 0) == 0
                and skip_from_cases > 0
                and summary is not None
                and "skipped" not in summary.attrib
                and "ignored" not in summary.attrib
            )
        ):
            if summary_total == 0:
                results.summary = {
                    "pass": pass_from_cases,
                    "fail": fail_from_cases,
                    "skip": skip_from_cases,
                    "error": error_from_cases,
                }
            else:
                results.summary["skip"] = skip_from_cases

        return results

    @staticmethod
    def _inline_rca(stack: str) -> tuple[Optional[str], Optional[str]]:
        stack_lower = stack.lower()
        if "nullpointerexception" in stack_lower:
            return (
                "Null Pointer Exception",
                "Check for uninitialized variables or missing null checks in the OS framework layer.",
            )
        if "permissiondenied" in stack_lower or "securityexception" in stack_lower:
            return (
                "Permission/Security Exception",
                "Verify SELinux policies (dmesg | grep denied) or Android Manifest hardware permissions.",
            )
        if "timeout" in stack_lower or "deadlinedelayed" in stack_lower:
            return (
                "Timeout",
                "Device hung or ADB disconnected. Check logcat for ANRs, tombstone crashes, or system server watchdog.",
            )
        if "assertionerror" in stack_lower or "expectation failed" in stack_lower:
            return (
                "Assertion Failed",
                "Test logically failed. The OS behavior deviated from CTS specifications.",
            )
        if "aaptparser" in stack_lower or "targetsetuperror" in stack_lower:
            return (
                "Environment Setup Error",
                "TradeFed failed to push or parse the APK. Check if SDK build-tools are in PATH and device supports the ABI.",
            )
        return (
            "Native Crash / Unhandled Error",
            "Review logcat stack trace directly. Look for tombstone logs in /data/tombstones.",
        )

    def get_failed_tests(self, results: TestResults) -> List[TestCaseResult]:
        failed = []
        for mod in results.modules:
            for tc in mod.test_cases:
                if tc.result in ("FAIL", "ERROR"):
                    failed.append(tc)
        return failed

    def get_summary(self, results: TestResults) -> Dict[str, float]:
        total = sum(results.summary.values())
        res: Dict[str, float] = dict(results.summary)
        res["total"] = total
        res["pass_rate"] = (results.summary["pass"] / total) * 100 if total > 0 else 0.0
        return res
