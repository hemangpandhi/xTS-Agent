from __future__ import annotations

from typing import Any, Dict, List

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
        return "catbox-functional"

    def execute(self, devices: List[str], config: Dict[str, Any]) -> SuiteResult:
        extra = list(config.get("extra_args") or [])
        spectatio_config = config.get("spectatio_config")
        if spectatio_config:
            extra.extend(["--template:map", f"spectatio={spectatio_config}"])
        cfg = dict(config)
        cfg["extra_args"] = extra
        cfg.setdefault("plan", self.plan)
        return super().execute(devices, cfg)
