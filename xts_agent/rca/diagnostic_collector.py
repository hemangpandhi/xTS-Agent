"""Diagnostic artifact collector."""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class DiagnosticCollector:
    def __init__(self, adb_wrapper: Any, output_dir: str | Path):
        self.adb = adb_wrapper
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def collect_on_failure(self, device_serial: str, test_id: str) -> Dict[str, Path]:
        safe_id = test_id.replace("/", "_").replace("#", "_")
        target = self.output_dir / safe_id
        target.mkdir(parents=True, exist_ok=True)
        artifacts: Dict[str, Path] = {}
        try:
            screenshot = target / "screenshot.png"
            self.adb.screenshot(device_serial, str(screenshot))
            artifacts["screenshot"] = screenshot
        except Exception as exc:
            logger.warning("Screenshot failed: %s", exc)
        try:
            logcat_path = target / "logcat.txt"
            self.adb.logcat(device_serial, str(logcat_path), duration_secs=5)
            artifacts["logcat"] = logcat_path
        except Exception as exc:
            logger.warning("Logcat capture failed: %s", exc)
        try:
            bugreport = target / "bugreport.zip"
            self.adb.bugreport(device_serial, str(bugreport))
            artifacts["bugreport"] = bugreport
        except Exception as exc:
            logger.warning("Bugreport failed: %s", exc)
        return artifacts

    def collect_device_state(self, serial: str) -> dict:
        state: Dict[str, Any] = {"serial": serial}
        try:
            state["boot_completed"] = self.adb.get_prop(serial, "sys.boot_completed")
            state["fingerprint"] = self.adb.get_prop(serial, "ro.build.fingerprint")
            state["offline"] = False
        except Exception:
            state["offline"] = True
        return state

    def archive_diagnostics(self, test_id: str) -> Path:
        safe_id = test_id.replace("/", "_").replace("#", "_")
        source = self.output_dir / safe_id
        archive = self.output_dir / f"{safe_id}_diags.zip"
        if not source.exists():
            return archive
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in source.rglob("*"):
                if path.is_file():
                    zf.write(path, arcname=str(path.relative_to(source)))
        return archive
