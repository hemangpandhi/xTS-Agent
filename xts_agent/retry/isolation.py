"""Retry isolation handlers."""

from __future__ import annotations

import logging
import shlex
import subprocess
from enum import Enum
from typing import Any, Union

from xts_agent.device.adb_wrapper import AdbWrapper

logger = logging.getLogger(__name__)


class IsolationHandler:
    """Applies isolation between retries.

    Physical devices are never wiped: a data wipe drops the host's ADB key
    authorization, Wi-Fi and setup state, silently shrinking the device farm.
    FULLY_ISOLATED therefore means reboot + re-run preparers on hardware. On
    virtual devices an optional host-side reset command (e.g. Cuttlefish
    ``cvd powerwash``) can be configured via ``device.virtual_reset_command``;
    ``{serial}`` in it is replaced with the device serial.
    """

    def __init__(self, device_manager: Any, virtual_reset_command: str = "", preparer: Any = None):
        self.device_manager = device_manager
        self.virtual_reset_command = virtual_reset_command or ""
        # DevicePreparer re-applies the full prep profile after a reset
        self.preparer = preparer

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
            if not self.reset_virtual_device(device_serial):
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

    def reset_virtual_device(self, serial: str) -> bool:
        """Run the configured host-side reset for a virtual device.

        Returns False (caller falls back to reboot) for physical devices, when
        no command is configured, or when the reset fails.
        """
        if not self.virtual_reset_command:
            return False
        if not self.device_manager.is_virtual_device(serial):
            logger.info("Not wiping physical device %s; rebooting instead", serial)
            return False
        cmd = [part.replace("{serial}", serial) for part in shlex.split(self.virtual_reset_command)]
        logger.warning("Resetting virtual device %s: %s", serial, " ".join(cmd))
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Virtual reset failed for %s: %s", serial, exc)
            return False
        return self.device_manager.wait_for_device(serial)

    def rerun_preparers(self, serial: str) -> None:
        logger.info("Re-running preparers for %s", serial)
        if self.preparer is not None:
            self.preparer.prepare(serial)
            return
        try:
            AdbWrapper.shell(serial, "settings put global stay_on_while_plugged_in 3", timeout=15)
            AdbWrapper.shell(serial, "svc power stayon true", timeout=15)
        except Exception as exc:
            logger.warning("Preparers failed on %s: %s", serial, exc)
