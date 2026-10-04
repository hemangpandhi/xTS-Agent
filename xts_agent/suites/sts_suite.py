from __future__ import annotations
from typing import List, Dict, Any
from .base_suite import BaseSuite, SuiteResult

class StsSuite(BaseSuite):
    @property
    def name(self) -> str:
        return "STS"

    @property
    def command(self) -> str:
        return "sts"

    @property
    def plan(self) -> str:
        return "sts"

    def execute(self, devices: List[str], config: Dict[str, Any]) -> SuiteResult:
        shard_count = config.get("shard_count", len(devices))
        exclude_filters = config.get("exclude_filters", [])
        
        cmd = self.build_command(shard_count=shard_count, exclude_filters=exclude_filters)
        return self.run_tradefed(cmd, devices)

    def retry(self, session_id: str, devices: List[str], config: Dict[str, Any]) -> SuiteResult:
        cmd = ["retry", "--retry", session_id]
        return self.run_tradefed(cmd, devices)
