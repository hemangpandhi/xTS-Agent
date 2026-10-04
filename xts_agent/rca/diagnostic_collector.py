"""
from __future__ import annotations
Diagnostic artifact collector.
"""
from pathlib import Path
from typing import Dict, Any

class DiagnosticCollector:
    def __init__(self, adb_wrapper: Any, output_dir: str | Path):
        self.adb = adb_wrapper
        self.output_dir = Path(output_dir)
        
    def collect_on_failure(self, device_serial: str, test_id: str) -> Dict[str, Path]:
        return {}
        
    def collect_device_state(self, serial: str) -> dict:
        return {}
        
    def archive_diagnostics(self, test_id: str) -> Path:
        return self.output_dir / f"{test_id}_diags.zip"
