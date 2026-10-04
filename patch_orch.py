import re

with open('/mnt/xTS_Agent/xts_agent/orchestrator.py', 'r') as f:
    content = f.read()

new_imports = """
from .reporting.html_report import HTMLReportGenerator
from .execution.ats2_client import ATS2Client
"""
if "HTMLReportGenerator" not in content:
    content = content.replace("import logging", "import logging\n" + new_imports)

# Replace generate_reports
old_generate = """    def generate_reports(self):
        logger.info("Generating reports...")"""
new_generate = """    def generate_reports(self, results):
        logger.info("Generating beautiful HTML reports...")
        output_dir = Path("./results/reports")
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"xts_report_{self.plan.name.replace(' ', '_')}.html"
        
        generator = HTMLReportGenerator()
        generator.generate(plan_result=results, rca_report=None, comparison=None, output_path=report_path)
        logger.info(f"Report generated successfully at: {report_path.absolute()}")
        
        # Trigger ATS 2.0 Upload if configured
        ats2 = ATS2Client("http://ats2.omnilab.local", "dummy_api_key")
        for suite_name, suite_res in results.suites_results.items():
            ats2.upload_results(suite_name, self.plan.name, suite_res.results_dir)"""

content = content.replace(old_generate, new_generate)

# Hook it into run_plan
old_run = "logger.info(f\"Duration: {results.duration:.2f}s | Pass: {results.total_pass}, Fail: {results.total_fail}\")"
new_run = "logger.info(f\"Duration: {results.duration:.2f}s | Pass: {results.total_pass}, Fail: {results.total_fail}\")\n        self.generate_reports(results)"
content = content.replace(old_run, new_run)

with open('/mnt/xTS_Agent/xts_agent/orchestrator.py', 'w') as f:
    f.write(content)
