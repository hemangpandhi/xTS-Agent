"""Plan-based execution sequencer."""
from __future__ import annotations
from typing import List
from dataclasses import field
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import Dict, Any, List
from .tradefed_runner import TradefedRunner
from xts_agent.results.result_parser import ResultParser, TestResults
from pathlib import Path


logger = logging.getLogger(__name__)

@dataclass
class SuiteResult:
    name: str
    status: str
    pass_count: int
    fail_count: int
    skip_count: int
    duration: float
    session_id: int
    results_dir: str
    retry_count: int
    details: Any = None

@dataclass
class PlanResult:
    suites_results: Dict[str, SuiteResult]
    total_pass: int
    total_fail: int
    total_skip: int
    duration: float
    overall_status: str
    device_serials: List[str] = field(default_factory=list)

class TestPlanExecutor:
    def __init__(self, config: Any, device_manager: Any, shard_manager: Any, retry_manager: Any, suite_registry: Any):
        self.config = config
        self.device_manager = device_manager
        self.shard_manager = shard_manager
        self.retry_manager = retry_manager
        self.suite_registry = suite_registry

    def execute_plan(self, plan_config: dict, dry_run: bool = False) -> PlanResult:
        suites = plan_config.get("suites", [])
        suites_results = {}
        total_pass, total_fail, total_skip = 0, 0, 0
        overall_status = "PASSED"
        start_time = time.time()
        
        for suite_config in sorted(suites, key=lambda x: getattr(x, 'priority', 0), reverse=True):
            if not getattr(suite_config, 'enabled', True):
                continue
                
            suite_res = self.execute_suite(suite_config, dry_run)
            suites_results[suite_res.name] = suite_res
            
            total_pass += suite_res.pass_count
            total_fail += suite_res.fail_count
            total_skip += suite_res.skip_count
            
            if suite_res.status != "PASSED":
                overall_status = "FAILED"
                
        duration = time.time() - start_time
        return PlanResult(suites_results, total_pass, total_fail, total_skip, duration, overall_status)

    def execute_suite(self, suite_config: dict, dry_run: bool = False) -> SuiteResult:
        name = getattr(suite_config, 'name', 'Unknown')
        plan_name = getattr(suite_config, 'plan', 'default')
        logger.info(f"Preparing to execute suite: {name} (plan: {plan_name})")
        
        # 1. Allocate devices
        try:
            available_count = len(self.device_manager.get_available_devices())
            req_devices = min(suite_config.sharding.shard_count, available_count)
            if req_devices == 0:
                raise ValueError("No devices available!")
            devices = self.device_manager.allocate_devices(req_devices, "any")
            logger.info(f"Allocated {len(devices)} devices: {[d.serial for d in devices]}")
        except ValueError as e:
            logger.error(f"Device allocation failed: {e}")
            return SuiteResult(name, "FAILED", 0, 0, 0, 0.0, 0, "", 0)

        # 2. Build Tradefed Command
        runner = TradefedRunner(f"/opt/xts/android-{name.lower()}", f"{name.lower()}-tradefed")
        
        # AAOS specific flags
        extra_args = []
        is_aaos = any(d.device_type == "aaos" for d in devices)
        if is_aaos:
            pass

        cmd = runner.build_run_command(
            plan=plan_name,
            shard_count=len(devices),
            retry_config=dataclasses.asdict(getattr(suite_config, 'retry')) if getattr(suite_config, 'retry', None) else {},
            exclude_filters=getattr(suite_config, 'exclude_filters', []),
            include_filters=getattr(suite_config, 'include_filters', []),
            extra_args=extra_args
        )
        
        logger.info(f"Executing: {' '.join(cmd)}")
        if dry_run:
            logger.info("DRY RUN: Skipping actual execution.")
            self.device_manager.release_devices([d.serial for d in devices])
            return SuiteResult(name, "PASSED", 0, 0, 0, 0.0, 0, "", 0)

        # 3. Execute
        exec_res = runner.execute(cmd, timeout_hours=72, log_dir="./results/logs")
        
        self.device_manager.release_devices([d.serial for d in devices])
        status = "PASSED" if exec_res.success else "FAILED"
        
        pass_c, fail_c, skip_c = 0, 0, 0
        if exec_res.results_dir:
            res_file = Path(exec_res.results_dir) / "test_result.xml"
            if res_file.exists():
                try:
                    parser = ResultParser()
                    parsed = parser.parse_xml(res_file)
                    pass_c = parsed.summary.get('pass', 0)
                    fail_c = parsed.summary.get('fail', 0)
                    skip_c = parsed.summary.get('skip', 0)
                    if fail_c > 0:
                        status = "FAILED"
                except Exception as e:
                    logger.error(f"Failed to parse results: {e}")

        return SuiteResult(
            name=name,
            status=status,
            pass_count=pass_c,
            fail_count=fail_c,
            skip_count=skip_c,
            duration=exec_res.duration,
            session_id=exec_res.session_id or 0,
            results_dir=exec_res.results_dir or "",
            retry_count=0,
            details=parsed if 'parsed' in locals() else None
        )
