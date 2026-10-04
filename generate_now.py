import sys
from pathlib import Path
from xts_agent.execution.test_plan_executor import SuiteResult, PlanResult
from xts_agent.results.result_parser import ResultParser
from xts_agent.reporting.html_report import HTMLReportGenerator

parser = ResultParser()
parsed_results = parser.parse_xml("/opt/xts/android-cts/results/2026.10.03_13.21.28.012_2796/test_result.xml")

suite_result = SuiteResult(
    name="cts",
    status="PASSED",
    pass_count=parsed_results.summary["pass"],
    fail_count=parsed_results.summary["fail"],
    skip_count=parsed_results.summary["skip"],
    duration=255.0,
    session_id=1,
    results_dir="/opt/xts/android-cts/results/2026.10.03_13.21.28.012_2796",
    retry_count=0,
    details=parsed_results
)

plan = PlanResult(
    suites_results={"cts": suite_result},
    total_pass=parsed_results.summary["pass"],
    total_fail=parsed_results.summary["fail"],
    total_skip=parsed_results.summary["skip"],
    duration=255.0,
    overall_status="PASSED"
)

report_dir = Path("/mnt/xTS_Agent/results/reports")
report_dir.mkdir(parents=True, exist_ok=True)
generator = HTMLReportGenerator()
generator.generate(plan, None, None, report_dir / "xts_report_Bionic_Manual.html")
print("Report generated at", report_dir / "xts_report_Bionic_Manual.html")
