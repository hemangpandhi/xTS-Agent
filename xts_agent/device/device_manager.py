from __future__ import annotations
import time
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict
from .adb_wrapper import AdbWrapper, AdbError

logger = logging.getLogger(__name__)

@dataclass
class HealthReport:
    battery_level: int
    is_charging: bool
    storage_free_mb: int
    has_internet: bool
    is_screen_on: bool
    healthy: bool

@dataclass
class DeviceInfo:
    serial: str
    model: str
    product: str
    build_fingerprint: str
    sdk_version: int
    battery_level: int
    state: str
    device_type: str

class DeviceManager:
    def __init__(self):
        self._allocated: set[str] = set()

    def discover_devices(self) -> List[DeviceInfo]:
        devices = []
        try:
            output = AdbWrapper._run_cmd(["adb", "devices", "-l"])
            lines = output.splitlines()[1:]
            for line in lines:
                if not line.strip():
                    continue
                parts = line.split()
                serial = parts[0]
                state = parts[1]
                
                if state != "device":
                    devices.append(DeviceInfo(serial, "", "", "", 0, 0, state, "unknown"))
                    continue
                
                props = self.get_device_properties(serial)
                model = props.get("ro.product.model", "")
                product = props.get("ro.product.name", "")
                fingerprint = props.get("ro.build.fingerprint", "")
                sdk_str = props.get("ro.build.version.sdk", "0")
                sdk_version = int(sdk_str) if sdk_str.isdigit() else 0
                
                device_type = "aaos" if self.is_aaos_device(serial) else "emulator"
                if "emulator" not in serial and not self.is_aaos_device(serial):
                    device_type = "phone"
                
                health = self.check_device_health(serial)
                
                devices.append(DeviceInfo(
                    serial=serial,
                    model=model,
                    product=product,
                    build_fingerprint=fingerprint,
                    sdk_version=sdk_version,
                    battery_level=health.battery_level,
                    state=state,
                    device_type=device_type
                ))
        except AdbError as e:
            logger.error(f"Failed to discover devices: {e}")
            
        return devices

    def get_device_properties(self, serial: str) -> Dict[str, str]:
        props = {}
        try:
            output = AdbWrapper.shell(serial, "getprop")
            for line in output.splitlines():
                if ": " in line:
                    key, val = line.split(": ", 1)
                    key = key.strip("[]")
                    val = val.strip("[]")
                    props[key] = val
        except AdbError:
            pass
        return props

    def check_device_health(self, serial: str) -> HealthReport:
        try:
            dumpsys_battery = AdbWrapper.shell(serial, "dumpsys battery")
            level = 50
            ac_powered = False
            usb_powered = False
            for line in dumpsys_battery.splitlines():
                if line.strip().startswith("level:"):
                    level = int(line.split(":")[1].strip())
                if "AC powered:" in line and "true" in line:
                    ac_powered = True
                if "USB powered:" in line and "true" in line:
                    usb_powered = True
            
            storage_out = AdbWrapper.shell(serial, "df /data")
            free_mb = 1000
            try:
                # Parse df output
                lines = storage_out.splitlines()
                if len(lines) > 1:
                    parts = lines[1].split()
                    if len(parts) >= 4:
                        free_mb = int(parts[3]) // 1024 # Assumes 1K blocks
            except Exception:
                pass
                
            ping = ""
            has_internet = "1 packets transmitted, 1 received" in ping
            
            dumpsys_power = AdbWrapper.shell(serial, "dumpsys power | grep mWakefulness")
            is_screen_on = "Awake" in dumpsys_power
            
            healthy = (level >= 20 or "0.0.0.0" in serial) and free_mb > 500
            return HealthReport(level, ac_powered or usb_powered, free_mb, has_internet, is_screen_on, healthy)
        except AdbError:
            return HealthReport(0, False, 0, False, False, False)

    def reboot_device(self, serial: str) -> bool:
        try:
            AdbWrapper.reboot(serial)
            return True
        except AdbError:
            return False

    def wait_for_device(self, serial: str, timeout: int = 120) -> bool:
        try:
            AdbWrapper.wait_for_device(serial, timeout)
            # Wait for boot complete
            start = time.time()
            while time.time() - start < timeout:
                prop = AdbWrapper.get_prop(serial, "sys.boot_completed")
                if prop == "1" and AdbWrapper.get_prop(serial, "init.svc.bootanim") == "stopped":
                    return True
                time.sleep(2)
            return False
        except AdbError:
            return False

    def is_aaos_device(self, serial: str) -> bool:
        try:
            pm_features = AdbWrapper.shell(serial, "pm list features")
            return "android.hardware.type.automotive" in pm_features
        except AdbError:
            return False

    def get_available_devices(self, min_battery: int = 20) -> List[DeviceInfo]:
        all_devices = self.discover_devices()
        available = []
        for d in all_devices:
            if d.state == "device" and d.serial not in self._allocated and (d.battery_level >= min_battery or "emulator" in d.device_type or "0.0.0.0" in d.serial):
                available.append(d)
        return available

    def allocate_devices(self, count: int, device_type: str) -> List[DeviceInfo]:
        available = self.get_available_devices()
        matching = [d for d in available if d.device_type == device_type or device_type == "any"]
        if len(matching) < count:
            raise ValueError(f"Not enough available {device_type} devices. Requested: {count}, Available: {len(matching)}")
        
        allocated = matching[:count]
        for d in allocated:
            self._allocated.add(d.serial)
        return allocated

    def release_devices(self, serials: List[str]) -> None:
        for s in serials:
            self._allocated.discard(s)
