from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List

from .adb_wrapper import AdbError, AdbWrapper

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
    def __init__(self, adb_timeout: int = 30):
        self._allocated: set[str] = set()
        self.adb_timeout = adb_timeout

    def discover_devices(self) -> List[DeviceInfo]:
        devices: List[DeviceInfo] = []
        try:
            output = AdbWrapper._run_cmd(["adb", "devices", "-l"], timeout=15)
            lines = output.splitlines()[1:]
            for line in lines:
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                serial = parts[0]
                state = parts[1]

                if state != "device":
                    devices.append(
                        DeviceInfo(serial, "", "", "", 0, 0, state, "unknown")
                    )
                    continue

                props = self.get_device_properties(serial)
                model = props.get("ro.product.model", "")
                product = props.get("ro.product.name", "")
                fingerprint = props.get("ro.build.fingerprint", "")
                sdk_str = props.get("ro.build.version.sdk", "0")
                sdk_version = int(sdk_str) if sdk_str.isdigit() else 0

                if self.is_aaos_device(serial):
                    device_type = "aaos"
                elif "emulator" in serial or serial.startswith("0.0.0.0"):
                    device_type = "emulator"
                else:
                    device_type = "phone"

                health = self.check_device_health(serial)
                devices.append(
                    DeviceInfo(
                        serial=serial,
                        model=model,
                        product=product,
                        build_fingerprint=fingerprint,
                        sdk_version=sdk_version,
                        battery_level=health.battery_level,
                        state=state,
                        device_type=device_type,
                    )
                )
        except AdbError as e:
            logger.error("Failed to discover devices: %s", e)

        return devices

    def get_device_properties(self, serial: str) -> Dict[str, str]:
        props: Dict[str, str] = {}
        try:
            output = AdbWrapper.shell(serial, "getprop", timeout=self.adb_timeout)
            for line in output.splitlines():
                if ": " not in line:
                    continue
                key, val = line.split(": ", 1)
                props[key.strip("[]")] = val.strip("[]")
        except AdbError:
            pass
        return props

    def check_device_health(self, serial: str) -> HealthReport:
        try:
            dumpsys_battery = AdbWrapper.shell(
                serial, "dumpsys battery", timeout=self.adb_timeout, silent=True
            )
            level = 50
            ac_powered = False
            usb_powered = False
            for line in dumpsys_battery.splitlines():
                stripped = line.strip()
                if stripped.startswith("level:"):
                    try:
                        level = int(stripped.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                if "AC powered:" in stripped and "true" in stripped.lower():
                    ac_powered = True
                if "USB powered:" in stripped and "true" in stripped.lower():
                    usb_powered = True

            storage_out = AdbWrapper.shell(serial, "df /data", timeout=self.adb_timeout, silent=True)
            free_mb = 1000
            try:
                lines = storage_out.splitlines()
                if len(lines) > 1:
                    parts = lines[-1].split()
                    if len(parts) >= 4:
                        # df Available column is typically in 1K blocks
                        free_mb = int(parts[3]) // 1024
            except Exception:
                pass

            has_internet = False
            try:
                ping = AdbWrapper.shell(serial, "ping -c 1 -W 2 8.8.8.8", timeout=10, silent=True)
                has_internet = (
                    "1 received" in ping
                    or "1 packets received" in ping
                    or "bytes from" in ping
                )
            except AdbError:
                has_internet = False

            is_screen_on = True
            try:
                dumpsys_power = AdbWrapper.shell(
                    serial,
                    "dumpsys power | grep mWakefulness",
                    timeout=self.adb_timeout,
                    silent=True
                )
                is_screen_on = "Awake" in dumpsys_power or "mWakefulness=Awake" in dumpsys_power
            except AdbError:
                pass

            # Cuttlefish / network ADB and emulators are exempt from battery gate
            battery_ok = level >= 20 or "0.0.0.0" in serial or "emulator" in serial
            healthy = battery_ok and free_mb > 500
            return HealthReport(
                level,
                ac_powered or usb_powered,
                free_mb,
                has_internet,
                is_screen_on,
                healthy,
            )
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
            start = time.time()
            while time.time() - start < timeout:
                prop = AdbWrapper.get_prop(serial, "sys.boot_completed")
                anim = AdbWrapper.get_prop(serial, "init.svc.bootanim")
                if prop == "1" and (anim in ("stopped", "", "unknown") or anim == "stopped"):
                    return True
                time.sleep(2)
            return False
        except AdbError:
            return False

    def is_aaos_device(self, serial: str) -> bool:
        try:
            pm_features = AdbWrapper.shell(
                serial, "pm list features", timeout=self.adb_timeout, silent=True
            )
            return "android.hardware.type.automotive" in pm_features
        except AdbError:
            return False

    def get_available_devices(self, min_battery: int = 20) -> List[DeviceInfo]:
        all_devices = self.discover_devices()
        available: List[DeviceInfo] = []
        for d in all_devices:
            if d.state != "device" or d.serial in self._allocated:
                continue
            battery_ok = (
                d.battery_level >= min_battery
                or d.device_type == "emulator"
                or "0.0.0.0" in d.serial
            )
            if battery_ok:
                available.append(d)
        return available

    def allocate_devices(self, count: int, device_type: str = "any") -> List[DeviceInfo]:
        available = self.get_available_devices()
        matching = [
            d
            for d in available
            if device_type == "any" or d.device_type == device_type
        ]
        if len(matching) < count:
            raise ValueError(
                f"Not enough available {device_type} devices. "
                f"Requested: {count}, Available: {len(matching)}"
            )

        allocated = matching[:count]
        for d in allocated:
            self._allocated.add(d.serial)
        return allocated

    def release_devices(self, serials: List[str]) -> None:
        for s in serials:
            self._allocated.discard(s)

    def health_check_all(self, reboot_unhealthy: bool = False) -> Dict[str, HealthReport]:
        reports: Dict[str, HealthReport] = {}
        for device in self.discover_devices():
            if device.state != "device":
                reports[device.serial] = HealthReport(0, False, 0, False, False, False)
                continue
            report = self.check_device_health(device.serial)
            reports[device.serial] = report
            if reboot_unhealthy and not report.healthy:
                logger.warning("Rebooting unhealthy device %s", device.serial)
                self.reboot_device(device.serial)
                self.wait_for_device(device.serial)
        return reports
