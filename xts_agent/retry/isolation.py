"""Retry isolation."""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

class IsolationHandler:
    def __init__(self, device_manager: Any):
        self.device_manager = device_manager

    def apply_isolation(self, device_serial: str, grade: Any):
        if grade.name == "REBOOT_ISOLATED":
            self.reboot_device(device_serial)
        elif grade.name == "FULLY_ISOLATED":
            self.wipe_device(device_serial)
            self.reboot_device(device_serial)
            self.rerun_preparers(device_serial)

    def reboot_device(self, serial: str):
        logger.info(f"Rebooting device {serial}")
        
    def wipe_device(self, serial: str):
        logger.info(f"Wiping device {serial}")
        
    def rerun_preparers(self, serial: str):
        logger.info(f"Rerunning preparers for device {serial}")
