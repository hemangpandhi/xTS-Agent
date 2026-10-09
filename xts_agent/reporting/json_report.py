"""JSON summary generator."""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from xts_agent.results.result_parser import TestResults


def compact_results(results: TestResults) -> dict:
    """Summary + per-module counts + failing tests only.

    Full CTS details are ~150 MB of JSON per run and nothing reads the
    passing entries back (the agent re-parses test_result.xml when needed).
    """
    return {
        "suite_name": results.suite_name,
        "device_info": results.device_info,
        "start_time": results.start_time,
        "end_time": results.end_time,
        "start_ms": results.start_ms,
        "summary": results.summary,
        "modules_done": results.modules_done,
        "modules_total": results.modules_total,
        "modules": [
            {
                "name": m.name,
                "abi": m.abi,
                "done": m.done,
                "pass": sum(1 for t in m.test_cases if t.result == "PASS"),
                "fail": sum(1 for t in m.test_cases if t.result in ("FAIL", "ERROR")),
                "failures": [
                    _serialize(t) for t in m.test_cases if t.result in ("FAIL", "ERROR")
                ],
            }
            for m in results.modules
        ],
    }


def _serialize(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, TestResults):
        return compact_results(obj)
    if isinstance(obj, Enum):
        return obj.name
    if is_dataclass(obj):
        # Field-by-field (not asdict) so nested TestResults stay compact
        return {f.name: _serialize(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


class JSONReportGenerator:
    def generate(
        self,
        plan_result: Any,
        rca_report: Any,
        comparison: Any,
        output_path: str | Path,
        triage: Any = None,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "status": getattr(plan_result, "overall_status", "unknown"),
            "plan_name": getattr(plan_result, "plan_name", ""),
            "profile": getattr(plan_result, "profile", "development"),
            "duration": getattr(plan_result, "duration", 0),
            "total_pass": getattr(plan_result, "total_pass", 0),
            "total_fail": getattr(plan_result, "total_fail", 0),
            "total_skip": getattr(plan_result, "total_skip", 0),
            "device_serials": getattr(plan_result, "device_serials", []),
            "suites": _serialize(getattr(plan_result, "suites_results", {})),
            "rca": _serialize(rca_report),
            "comparison": _serialize(comparison),
            "triage": triage.to_dict() if triage is not None else None,
        }
        output_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return output_path
