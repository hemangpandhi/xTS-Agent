from __future__ import annotations

"""Background device state monitor."""

import logging
import threading
import time
from typing import Any, Dict, Set

from .device_manager import DeviceManager

logger = logging.getLogger(__name__)


class DeviceMonitor(threading.Thread):
    """Periodically observes device state.

    Auto-reboot on offline is opt-in (``auto_recover=False`` by default) so
    monitoring never fights an active TradeFed run unless explicitly enabled.
    """

    def __init__(
        self,
        device_manager: DeviceManager,
        interval: int = 60,
        auto_recover: bool = False,
        protected_serials: Set[str] | None = None,
    ):
        super().__init__(daemon=True)
        self.device_manager = device_manager
        self.interval = interval
        self.auto_recover = auto_recover
        self.protected_serials = protected_serials or set()
        self._stop_event = threading.Event()
        self.device_history: Dict[str, list] = {}

    def run(self) -> None:
        logger.info(
            "Device monitor started (interval=%ss, auto_recover=%s)",
            self.interval,
            self.auto_recover,
        )
        while not self._stop_event.is_set():
            self._check_devices()
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()

    def protect(self, serials: Set[str]) -> None:
        self.protected_serials |= set(serials)

    def unprotect(self, serials: Set[str]) -> None:
        self.protected_serials -= set(serials)

    def _check_devices(self) -> None:
        devices = self.device_manager.discover_devices()
        for d in devices:
            history = self.device_history.setdefault(d.serial, [])
            if history and history[-1].state != d.state:
                logger.warning(
                    "Device %s state changed from %s to %s",
                    d.serial,
                    history[-1].state,
                    d.state,
                )
                if (
                    self.auto_recover
                    and d.state == "offline"
                    and d.serial not in self.protected_serials
                ):
                    logger.info("Attempting recovery reboot for %s", d.serial)
                    self.device_manager.reboot_device(d.serial)

            history.append(d)
            if len(history) > 10:
                history.pop(0)

    def get_status(self) -> Dict[str, Any]:
        devices = self.device_manager.discover_devices()
        return {
            "total_devices": len(devices),
            "online_devices": len([d for d in devices if d.state == "device"]),
            "offline_devices": len([d for d in devices if d.state == "offline"]),
            "unauthorized_devices": len(
                [d for d in devices if d.state == "unauthorized"]
            ),
            "device_details": [
                {
                    "serial": d.serial,
                    "state": d.state,
                    "battery": d.battery_level,
                    "type": d.device_type,
                }
                for d in devices
            ],
        }
