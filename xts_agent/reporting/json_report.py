from __future__ import annotations
"""
from __future__ import annotations
JSON summary generator.
"""
from pathlib import Path
from typing import Any
import json

class JSONReportGenerator:
    def generate(self, plan_result: Any, rca_report: Any, comparison: Any, output_path: str | Path):
        data = {
            "status": "complete"
        }
        with open(output_path, "w") as f:
            json.dump(data, f, indent=2)
