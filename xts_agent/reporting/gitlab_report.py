"""GitLab JUnit XML generator."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


class GitLabReportGenerator:
    def generate(self, plan_result: Any, output_path: str | Path) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        root = ET.Element("testsuites")
        root.set("name", getattr(plan_result, "plan_name", "xts"))
        root.set("tests", str(
            plan_result.total_pass + plan_result.total_fail + plan_result.total_skip
        ))
        root.set("failures", str(plan_result.total_fail))
        root.set("time", f"{getattr(plan_result, 'duration', 0.0):.3f}")

        for suite_name, suite_res in (plan_result.suites_results or {}).items():
            suite_el = ET.SubElement(root, "testsuite")
            suite_el.set("name", suite_name)
            suite_el.set(
                "tests",
                str(suite_res.pass_count + suite_res.fail_count + suite_res.skip_count),
            )
            suite_el.set("failures", str(suite_res.fail_count))
            suite_el.set("skipped", str(suite_res.skip_count))
            suite_el.set("time", f"{suite_res.duration:.3f}")

            details = suite_res.details
            if details is not None and getattr(details, "modules", None):
                for mod in details.modules:
                    for tc in mod.test_cases:
                        case_el = ET.SubElement(suite_el, "testcase")
                        case_el.set("classname", tc.class_name or mod.name)
                        case_el.set("name", tc.test_name)
                        if tc.duration is not None:
                            case_el.set("time", f"{tc.duration / 1000:.3f}")
                        if tc.result == "FAIL":
                            fail_el = ET.SubElement(case_el, "failure")
                            fail_el.set("message", tc.message or "failed")
                            fail_el.text = tc.stack_trace or tc.message or ""
                        elif tc.result == "ERROR":
                            err_el = ET.SubElement(case_el, "error")
                            err_el.set("message", tc.message or "error")
                            err_el.text = tc.stack_trace or tc.message or ""
                        elif tc.result == "SKIP":
                            ET.SubElement(case_el, "skipped")
            else:
                # Fallback single case reflecting suite status
                case_el = ET.SubElement(suite_el, "testcase")
                case_el.set("classname", suite_name)
                case_el.set("name", "suite_execution")
                case_el.set("time", f"{suite_res.duration:.3f}")
                if suite_res.status != "PASSED":
                    fail_el = ET.SubElement(case_el, "failure")
                    fail_el.set("message", suite_res.error_message or suite_res.status)
                    fail_el.text = suite_res.error_message or suite_res.status

        tree = ET.ElementTree(root)
        tree.write(output_path, encoding="utf-8", xml_declaration=True)
        return output_path
