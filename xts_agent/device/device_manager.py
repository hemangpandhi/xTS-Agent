from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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
    MAX_PROBE_WORKERS = 16

    def __init__(self, adb_timeout: int = 30):
        self._allocated: set[str] = set()
        self.adb_timeout = adb_timeout
        self._type_cache: Dict[Tuple[str, str], str] = {}

    def discover_devices(self) -> List[DeviceInfo]:
        """List attached devices, probing them in parallel.

        Discovery only reads what allocation needs (props, type, battery); the
        full health probe (storage, network, screen) is ``check_device_health``.
        """
        try:
            output = AdbWrapper._run_cmd(["adb", "devices", "-l"], timeout=15)
        except AdbError as e:
            logger.error("Failed to discover devices: %s", e)
            return []

        entries = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                entries.append((parts[0], parts[1]))
        if not entries:
            return []
        workers = min(self.MAX_PROBE_WORKERS, len(entries))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda e: self._probe_device(*e), entries))

    def _probe_device(self, serial: str, state: str) -> DeviceInfo:
        if state != "device":
            return DeviceInfo(serial, "", "", "", 0, 0, state, "unknown")

        props = self.get_device_properties(serial)
        fingerprint = props.get("ro.build.fingerprint", "")
        sdk_str = props.get("ro.build.version.sdk", "0")

        # Device type only changes on reflash, so cache it per (serial, build)
        cache_key = (serial, fingerprint)
        device_type = self._type_cache.get(cache_key)
        if device_type is None:
            if self.is_aaos_device(serial):
                device_type = "aaos"
            elif self._props_are_virtual(props) or "emulator" in serial:
                device_type = "emulator"
            else:
                device_type = "phone"
            if fingerprint:
                self._type_cache[cache_key] = device_type

        level, _ = self._read_battery(serial)
        return DeviceInfo(
            serial=serial,
            model=props.get("ro.product.model", ""),
            product=props.get("ro.product.name", ""),
            build_fingerprint=fingerprint,
            sdk_version=int(sdk_str) if sdk_str.isdigit() else 0,
            battery_level=level,
            state=state,
            device_type=device_type,
        )

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

    def _read_battery(self, serial: str) -> Tuple[int, bool]:
        """Return (level, charging); level defaults to 50 when unreadable."""
        level = 50
        charging = False
        try:
            out = AdbWrapper.shell(serial, "dumpsys battery", timeout=self.adb_timeout, silent=True)
        except AdbError:
            return level, charging
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("level:"):
                try:
                    level = int(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass
            if ("AC powered:" in stripped or "USB powered:" in stripped) and "true" in stripped.lower():
                charging = True
        return level, charging

    def has_validated_network(self, serial: str) -> bool:
        """True when Android has validated internet on the default network.

        Uses the platform's own validation instead of pinging 8.8.8.8, which
        lab networks often block and which costs seconds per device.
        """
        try:
            out = AdbWrapper.shell(serial, "dumpsys connectivity", timeout=self.adb_timeout, silent=True)
        except AdbError:
            return False
        active = re.search(r"Active default network:\s*(\d+)", out)
        if not active:
            return False
        for line in out.splitlines():
            if f"NetworkAgentInfo{{network{{{active.group(1)}}}" in line:
                return re.search(r"Capabilities: \S*\bVALIDATED\b", line) is not None
        return False

    def check_device_health(self, serial: str) -> HealthReport:
        try:
            level, charging = self._read_battery(serial)

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

            has_internet = self.has_validated_network(serial)

            is_screen_on = True
            try:
                dumpsys_power = AdbWrapper.shell(
                    serial,
                    "dumpsys power | grep mWakefulness",
                    timeout=self.adb_timeout,
                    silent=True
                )
                is_screen_on = "Awake" in dumpsys_power
            except AdbError:
                pass

            # Cuttlefish / network ADB and emulators are exempt from battery gate
            battery_ok = level >= 20 or "0.0.0.0" in serial or "emulator" in serial
            healthy = battery_ok and free_mb > 500
            return HealthReport(level, charging, free_mb, has_internet, is_screen_on, healthy)
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

    # Hardware names reported by Cuttlefish and the goldfish/ranchu emulators
    VIRTUAL_HARDWARE = ("cutf_cvm", "ranchu", "goldfish")

    def is_virtual_device(self, serial: str) -> bool:
        """True for Cuttlefish/emulator, judged by device props rather than serial."""
        return self._props_are_virtual(self.get_device_properties(serial))

    def _props_are_virtual(self, props: Dict[str, str]) -> bool:
        if not props:
            return False
        if props.get("ro.kernel.qemu") == "1" or props.get("ro.boot.qemu") == "1":
            return True
        hardware = props.get("ro.hardware", "") or props.get("ro.boot.hardware", "")
        return hardware in self.VIRTUAL_HARDWARE

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

    def select_shard_pool(
        self,
        devices: List[DeviceInfo],
        device_type: str = "any",
        required_props: Optional[Dict[str, str]] = None,
    ) -> List[DeviceInfo]:
        """Largest set of matching devices that all run the same build.

        Sharded xTS results are only valid when every shard ran the same
        ``ro.build.fingerprint``; mixing builds silently corrupts a run.
        """
        matching = [d for d in devices if device_type == "any" or d.device_type == device_type]
        if required_props:
            matching = [d for d in matching if self._has_props(d.serial, required_props)]

        groups: Dict[str, List[DeviceInfo]] = {}
        unknown = []
        for d in matching:
            if d.build_fingerprint:
                groups.setdefault(d.build_fingerprint, []).append(d)
            else:
                unknown.append(d.serial)
        if unknown:
            logger.warning("Excluding devices with unknown build fingerprint: %s", unknown)
        if not groups:
            return []
        pool = max(groups.values(), key=len)
        if len(groups) > 1:
            logger.warning(
                "Devices run %s different builds; sharding only across %s device(s) on %s. Others: %s",
                len(groups),
                len(pool),
                pool[0].build_fingerprint,
                {fp: [d.serial for d in ds] for fp, ds in groups.items() if ds is not pool},
            )
        return pool

    def _has_props(self, serial: str, required: Dict[str, str]) -> bool:
        props = self.get_device_properties(serial)
        mismatched = {k: props.get(k) for k, v in required.items() if props.get(k) != str(v)}
        if mismatched:
            logger.info("Device %s does not match required props: %s", serial, mismatched)
        return not mismatched

    def allocate_devices(
        self,
        count: int,
        device_type: str = "any",
        candidates: Optional[List[DeviceInfo]] = None,
    ) -> List[DeviceInfo]:
        available = candidates if candidates is not None else self.get_available_devices()
        matching = [
            d
            for d in available
            if d.serial not in self._allocated
            and (device_type == "any" or d.device_type == device_type)
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

    def reboot_and_wait_all(self, serials: List[str], timeout: int = 120) -> Dict[str, bool]:
        """Reboot devices concurrently; returns serial -> came back healthy."""
        def _one(serial: str) -> bool:
            return self.reboot_device(serial) and self.wait_for_device(serial, timeout)

        if not serials:
            return {}
        with ThreadPoolExecutor(max_workers=min(self.MAX_PROBE_WORKERS, len(serials))) as pool:
            results = dict(zip(serials, pool.map(_one, serials)))
        failed = [s for s, ok in results.items() if not ok]
        if failed:
            logger.warning("Devices did not come back after reboot: %s", failed)
        return results

    def health_check_all(self, reboot_unhealthy: bool = False) -> Dict[str, HealthReport]:
        devices = self.discover_devices()
        online = [d.serial for d in devices if d.state == "device"]
        reports: Dict[str, HealthReport] = {
            d.serial: HealthReport(0, False, 0, False, False, False)
            for d in devices
            if d.state != "device"
        }
        if online:
            with ThreadPoolExecutor(max_workers=min(self.MAX_PROBE_WORKERS, len(online))) as pool:
                reports.update(zip(online, pool.map(self.check_device_health, online)))
        unhealthy = [s for s in online if not reports[s].healthy]
        if reboot_unhealthy and unhealthy:
            logger.warning("Rebooting unhealthy devices: %s", unhealthy)
            self.reboot_and_wait_all(unhealthy)
        return reports
