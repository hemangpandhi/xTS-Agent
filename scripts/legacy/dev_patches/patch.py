import re

with open('/mnt/xTS_Agent/xts_agent/orchestrator.py', 'r') as f:
    content = f.read()

new_method = """    def generate_reports(self, results):
        logger.info("Generating beautiful HTML reports...")
        output_dir = Path("./results/reports")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        device_str = "device"
        if results.suites_results:
            first_suite = list(results.suites_results.values())[0]
            if first_suite.details and hasattr(first_suite.details, "device_info"):
                di = first_suite.details.device_info
                # usually "device_serial" is populated by TradeFed XML
                device_str = di.get("device_serial", "multiple_devices")
                device_str = device_str.replace(":", "_").replace(".", "_")
        
        report_filename = f"xts_report_{self.plan.name.replace(' ', '_')}_{device_str}_{timestamp}.html"
        report_path = output_dir / report_filename
        
        generator = HTMLReportGenerator()
        generator.generate(plan_result=results, rca_report=None, comparison=None, output_path=report_path)
        logger.info(f"Report generated successfully at: {report_path.absolute()}")
        
        # Trigger ATS 2.0 Upload if configured
        ats2 = ATS2Client("http://ats2.omnilab.local", "dummy_api_key")
        for suite_name, suite_res in results.suites_results.items():
            ats2.upload_results(suite_name, self.plan.name, suite_res.results_dir)
"""

content = re.sub(r'    def generate_reports\(self, results\):.*?            ats2\.upload_results\(suite_name, self\.plan\.name, suite_res\.results_dir\)\n', new_method, content, flags=re.DOTALL)

with open('/mnt/xTS_Agent/xts_agent/orchestrator.py', 'w') as f:
    f.write(content)

