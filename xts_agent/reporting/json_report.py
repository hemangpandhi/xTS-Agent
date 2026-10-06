"""JSON summary generator."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


def _serialize(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Enum):
        return obj.name
    if is_dataclass(obj):
        return {k: _serialize(v) for k, v in asdict(obj).items()}
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
