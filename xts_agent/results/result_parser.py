"""Parser for TradeFed test_result.xml."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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
    # TradeFed module id ("<abi> <module>"); the same test runs once per ABI
    module: str = ""

    @property
    def test_id(self) -> str:
        test = f"{self.class_name}#{self.test_name}"
        return f"{self.module} {test}" if self.module else test


@dataclass
class ModuleResult:
    name: str
    done: bool
    pass_count: int
    fail_count: int
    runtime: int
    test_cases: List[TestCaseResult] = field(default_factory=list)
    abi: str = ""

    @property
    def module_id(self) -> str:
        return f"{self.abi} {self.name}" if self.abi else self.name


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
    modules_done: int = 0
    modules_total: int = 0
    # Invocation start (epoch ms) from <Result start=...>; orders runs in history
    start_ms: int = 0
    end_ms: int = 0
    devices: List[str] = field(default_factory=list)  # <Result devices=...>

    @property
    def is_complete(self) -> bool:
        return self.modules_total == 0 or self.modules_done >= self.modules_total


def has_unexecuted_modules(suite_result) -> bool:
    """True when a suite's results show modules that never ran.

    Status alone is not enough: failures take precedence over INCOMPLETE, so
    a FAILED suite can still have hundreds of modules left to execute.
    """
    details = getattr(suite_result, "details", None)
    if details is not None and hasattr(details, "is_complete"):
        return not details.is_complete
    return getattr(suite_result, "status", "") == "INCOMPLETE"


def derive_suite_status(parsed: Optional[TestResults], exec_success: bool) -> Tuple[str, str]:
    """Return ``(status, reason)`` for a suite from its parsed results.

    The TradeFed exit code alone is never enough for PASSED: the run must have
    produced results, executed tests, completed every module and failed none.
    """
    if parsed is None:
        return "FAILED", "TradeFed produced no test_result.xml"
    fail = parsed.summary.get("fail", 0)
    error = parsed.summary.get("error", 0)
    if fail or error:
        return "FAILED", f"{fail} test(s) failed, {error} errored"
    if not parsed.is_complete:
        return (
            "INCOMPLETE",
            f"only {parsed.modules_done} of {parsed.modules_total} modules completed",
        )
    if parsed.summary.get("pass", 0) + parsed.summary.get("skip", 0) == 0:
        return "FAILED", "no tests were executed"
    if not exec_success:
        logger.warning("TradeFed exited non-zero but results are complete with no failures")
    return "PASSED", ""


def overall_status(statuses: Iterable[str]) -> str:
    """Combine suite statuses: FAILED > INCOMPLETE > PASSED; all DRY_RUN => DRY_RUN."""
    statuses = list(statuses)
    if not statuses:
        return "FAILED"
    if all(s == "DRY_RUN" for s in statuses):
        return "DRY_RUN"
    if any(s not in ("PASSED", "INCOMPLETE", "DRY_RUN") for s in statuses):
        return "FAILED"
    if "INCOMPLETE" in statuses:
        return "INCOMPLETE"
    return "PASSED"


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
        try:
            results.start_ms = int(root.attrib.get("start", 0) or 0)
            results.end_ms = int(root.attrib.get("end", 0) or 0)
        except ValueError:
            pass
        results.devices = [d for d in root.attrib.get("devices", "").split(",") if d]

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
            results.modules_done = int(summary.attrib.get("modules_done", 0) or 0)
            results.modules_total = int(summary.attrib.get("modules_total", 0) or 0)

        pass_from_cases = fail_from_cases = skip_from_cases = error_from_cases = 0

        for mod in root.findall("Module"):
            mod_res = ModuleResult(
                name=mod.attrib.get("name", "Unknown"),
                done=mod.attrib.get("done", "true").lower() == "true",
                pass_count=int(mod.attrib.get("pass", 0)),
                fail_count=int(mod.attrib.get("fail", 0)),
                runtime=int(mod.attrib.get("runtime", 0) or 0),
                abi=mod.attrib.get("abi", ""),
            )
            for tc in mod.findall(".//TestCase"):
                for t in tc.findall("Test"):
                    fail = t.find("Failure")
                    # The result attribute is authoritative: TradeFed also attaches
                    # <Failure> to ASSUMPTION_FAILURE tests, which are skips.
                    default = "FAIL" if fail is not None else "PASS"
                    status = (t.attrib.get("result") or default).upper()
                    msg = None
                    stack = None
                    rca_category = None
                    rca_recommendation = None
                    if fail is not None:
                        msg = fail.attrib.get("message")
                        stack_elem = fail.find("StackTrace")
                        if stack_elem is not None:
                            stack = stack_elem.text or ""
                            if status in ("FAIL", "FAILED", "FAILURE", "ERROR"):
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
                            module=mod_res.module_id,
                        )
                    )
            results.modules.append(mod_res)

        if summary is None or "modules_total" not in summary.attrib:
            results.modules_total = len(results.modules)
            results.modules_done = sum(1 for m in results.modules if m.done)

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
