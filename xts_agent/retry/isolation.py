"""Retry isolation handlers."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Union

logger = logging.getLogger(__name__)


class IsolationHandler:
    def __init__(self, device_manager: Any):
        self.device_manager = device_manager

    def _grade_name(self, grade: Any) -> str:
        if grade is None:
            return "NO_ISOLATION"
        if isinstance(grade, Enum):
            return grade.name
        return str(grade).upper()

    def apply_isolation(self, device_serial: str, grade: Union[str, Enum, Any]) -> None:
        name = self._grade_name(grade)
        if name in {"REBOOT_ISOLATED", "REBOOT"}:
            self.reboot_device(device_serial)
        elif name in {"FULLY_ISOLATED", "FULL"}:
            self.wipe_device(device_serial)
            self.reboot_device(device_serial)
            self.rerun_preparers(device_serial)
        else:
            logger.info("No isolation applied for grade=%s on %s", name, device_serial)

    def reboot_device(self, serial: str) -> None:
        logger.info("Rebooting device %s for isolation", serial)
        ok = self.device_manager.reboot_device(serial)
        if ok:
            self.device_manager.wait_for_device(serial)
        else:
            logger.warning("Reboot failed for %s", serial)

    def wipe_device(self, serial: str) -> None:
        """Factory-reset style wipe via adb (userdata). Use with care on physical devices."""
        logger.warning("Wiping userdata on device %s", serial)
        try:
            from xts_agent.device.adb_wrapper import AdbWrapper

            AdbWrapper.shell(serial, "recovery --wipe_data", timeout=30)
        except Exception:
            # Prefer explicit wipe via reboot recovery when shell recovery is unavailable
            try:
                from xts_agent.device.adb_wrapper import AdbWrapper

                AdbWrapper._run_cmd(["adb", "-s", serial, "shell", "am", "broadcast",
                                     "-a", "android.intent.action.MASTER_CLEAR"], timeout=30)
            except Exception as exc:
                logger.error("Wipe unavailable for %s: %s — falling back to reboot only", serial, exc)

    def rerun_preparers(self, serial: str) -> None:
        logger.info("Re-running basic preparers for %s", serial)
        try:
            from xts_agent.device.adb_wrapper import AdbWrapper

            AdbWrapper.shell(serial, "settings put global stay_on_while_plugged_in 3", timeout=15)
            AdbWrapper.shell(serial, "svc power stayon true", timeout=15)
        except Exception as exc:
            logger.warning("Preparers failed on %s: %s", serial, exc)
