from __future__ import annotations
"""Main orchestration engine for xTS Agent."""
import shutil
from datetime import datetime
from typing import Optional
from pathlib import Path
import logging

from .reporting.html_report import HTMLReportGenerator
from .execution.ats2_client import ATS2Client

from .config_loader import ConfigLoader, TestPlanConfig
from .device.device_manager import DeviceManager
from .execution.shard_manager import ShardManager
from .retry.retry_manager import RetryManager
from .execution.test_plan_executor import TestPlanExecutor
from .suites.suite_registry import SuiteRegistry

logger = logging.getLogger(__name__)

class Orchestrator:
    def __init__(self, plan_path: str | Path):
        self.config_loader = ConfigLoader(plan_path)
        self.plan: Optional[TestPlanConfig] = None
        
        self.device_manager = DeviceManager()
        self.shard_manager = ShardManager(self.device_manager)
        self.retry_manager = RetryManager(None, None) # Mocks for now
        self.suite_registry = SuiteRegistry()
        
    def _initialize(self):
        self.plan = self.config_loader.load_plan()
        logger.info(f"Loaded plan: {self.plan.name}")
        # Assuming suites are in /opt/xts/ or local
        self.suite_registry.discover_installed_suites(Path("/opt/xts"))
        
    def run_plan(self, auto_retry: bool = False, dry_run: bool = False):
        self._initialize()
        logger.info(f"Running plan {self.plan.name} (dry_run={dry_run}, auto_retry={auto_retry})")
        
        executor = TestPlanExecutor(
            config=self.plan,
            device_manager=self.device_manager,
            shard_manager=self.shard_manager,
            retry_manager=self.retry_manager,
            suite_registry=self.suite_registry
        )
        
        results = executor.execute_plan(self.plan.__dict__, dry_run=dry_run)
        logger.info(f"Plan Execution Finished: {results.overall_status}")
        logger.info(f"Duration: {results.duration:.2f}s | Pass: {results.total_pass}, Fail: {results.total_fail}")
        self.generate_reports(results)

    def retry_plan(self, max_retries: int = 1):
        self._initialize()
        logger.info(f"Retrying plan {self.plan.name} with max_retries={max_retries}")
        
    def analyze(self):
        logger.info("Running RCA analysis...")
        
    def generate_reports(self, results):
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
        
        # Zipping official Google results for ATS 2.0
        ats2 = ATS2Client("http://ats2.omnilab.local", "dummy_api_key")
        for suite_name, suite_res in results.suites_results.items():
            if suite_res.results_dir and Path(suite_res.results_dir).is_dir():
                zip_path = Path(suite_res.results_dir).with_suffix('.zip')
                logger.info(f"Zipping official Google results: {suite_res.results_dir}")
                shutil.make_archive(str(zip_path.with_suffix('')), 'zip', suite_res.results_dir)
                logger.info(f"Uploading official compliance zip {zip_path} to ATS 2.0...")
                ats2.upload_results(suite_name, self.plan.name, str(zip_path))
            else:
                logger.warning(f"Results dir not found for suite {suite_name}, skipping ATS 2.0 upload.")
