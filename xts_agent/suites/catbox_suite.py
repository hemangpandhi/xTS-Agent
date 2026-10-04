from __future__ import annotations
from typing import List, Dict, Any
from .base_suite import BaseSuite, SuiteResult

class CatboxSuite(BaseSuite):
    @property
    def name(self) -> str:
        return "CATBox"

    @property
    def command(self) -> str:
        return "catbox"

    @property
    def plan(self) -> str:
        return "catbox"

    def execute(self, devices: List[str], config: Dict[str, Any]) -> SuiteResult:
        shard_count = config.get("shard_count", len(devices))
        
        cmd = ["run", "commandAndExit", self.plan]
        
        spectatio_config = config.get("spectatio_config")
        if spectatio_config:
            # Specific flags for CATBox / Spectatio
            cmd.extend(["--template:map", f"spectatio={spectatio_config}"])
            
        if shard_count > 1:
            cmd.extend(["--shard-count", str(shard_count)])
            
        return self.run_tradefed(cmd, devices)

    def retry(self, session_id: str, devices: List[str], config: Dict[str, Any]) -> SuiteResult:
        cmd = ["retry", "--retry", session_id]
        return self.run_tradefed(cmd, devices)
