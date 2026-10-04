from __future__ import annotations
import threading
import time
import logging
from typing import Dict, Any
from .device_manager import DeviceManager, DeviceInfo

logger = logging.getLogger(__name__)

class DeviceMonitor(threading.Thread):
    def __init__(self, device_manager: DeviceManager, interval: int = 60):
        super().__init__(daemon=True)
        self.device_manager = device_manager
        self.interval = interval
        self._stop_event = threading.Event()
        self.device_history: Dict[str, list] = {}
        
    def run(self) -> None:
        logger.info("Device monitor started.")
        while not self._stop_event.is_set():
            self._check_devices()
            self._stop_event.wait(self.interval)
            
    def stop(self) -> None:
        self._stop_event.set()
        
    def _check_devices(self) -> None:
        devices = self.device_manager.discover_devices()
        for d in devices:
            if d.serial not in self.device_history:
                self.device_history[d.serial] = []
            
            history = self.device_history[d.serial]
            
            # Simple state change detection
            if history and history[-1].state != d.state:
                logger.warning(f"Device {d.serial} state changed from {history[-1].state} to {d.state}")
                if d.state == "offline":
                    logger.info(f"Attempting recovery for {d.serial}...")
                    self.device_manager.reboot_device(d.serial)
            
            history.append(d)
            # Keep last 10 records
            if len(history) > 10:
                history.pop(0)

    def get_status(self) -> Dict[str, Any]:
        devices = self.device_manager.discover_devices()
        return {
            "total_devices": len(devices),
            "online_devices": len([d for d in devices if d.state == "device"]),
            "offline_devices": len([d for d in devices if d.state == "offline"]),
            "unauthorized_devices": len([d for d in devices if d.state == "unauthorized"]),
            "device_details": [
                {
                    "serial": d.serial,
                    "state": d.state,
                    "battery": d.battery_level,
                    "type": d.device_type
                } for d in devices
            ]
        }
