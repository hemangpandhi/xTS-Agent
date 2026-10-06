"""Declarative device preparation applied before each suite.

Covers the CTS "device setup" checklist items that can be automated over
adb, so every shard starts from the same state instead of whatever a lab
engineer last left on the device. Steps are idempotent; a failing step is
logged and does not abort the run.
"""

from __future__ import annotations

import logging
import shlex
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from .adb_wrapper import AdbError, AdbWrapper

logger = logging.getLogger(__name__)


class DevicePreparer:
    def __init__(self, config: Any):
        self.config = config

    def steps(self) -> List[Dict[str, Any]]:
        """Ordered (name, shell command, secret) steps for the configured profile."""
        c = self.config
        steps: List[Dict[str, Any]] = [
            # 7 = stay awake on AC | USB | wireless power
            {"name": "stay awake", "cmd": "settings put global stay_on_while_plugged_in 7"},
            {"name": "stay awake (power)", "cmd": "svc power stayon true"},
        ]
        if c.screen_timeout:
            steps.append(
                {"name": "screen timeout", "cmd": f"settings put system screen_off_timeout {int(c.screen_timeout)}"}
            )
        if c.disable_screen_lock:
            steps.append({"name": "disable screen lock", "cmd": "locksettings set-disabled true"})
        if c.enable_location:
            steps.append({"name": "location on", "cmd": "cmd location set-location-enabled true"})
        if c.disable_adb_install_verifier:
            steps.append(
                {"name": "adb install verifier off", "cmd": "settings put global verifier_verify_adb_installs 0"}
            )
        if c.connect_wifi and c.wifi_ssid:
            steps.append({"name": "wifi on", "cmd": "cmd wifi set-wifi-enabled enabled"})
            if c.wifi_password:
                connect = (
                    f"cmd wifi connect-network {shlex.quote(c.wifi_ssid)} wpa2 "
                    f"{shlex.quote(c.wifi_password)}"
                )
            else:
                connect = f"cmd wifi connect-network {shlex.quote(c.wifi_ssid)} open"
            steps.append({"name": f"join wifi {c.wifi_ssid}", "cmd": connect, "secret": True})
        for i, extra in enumerate(c.extra_commands or []):
            steps.append({"name": f"extra command {i + 1}", "cmd": str(extra)})
        return steps

    def prepare(self, serial: str) -> List[str]:
        """Apply the profile to one device; returns names of failed steps."""
        failed = []
        for step in self.steps():
            try:
                # silent: a failed wifi step must not log the password
                AdbWrapper.shell(serial, step["cmd"], timeout=30, silent=True)
            except AdbError as exc:
                detail = "" if step.get("secret") else f": {self._redact(str(exc))}"
                logger.warning("Device prep '%s' failed on %s%s", step["name"], serial, detail)
                failed.append(step["name"])
        if failed:
            logger.warning("Device %s prepared with %s failed step(s): %s", serial, len(failed), failed)
        else:
            logger.info("Device %s prepared (%s steps)", serial, len(self.steps()))
        return failed

    def _redact(self, text: str) -> str:
        secret = self.config.wifi_password
        return text.replace(secret, "***") if secret else text

    def prepare_all(self, serials: List[str]) -> Dict[str, List[str]]:
        if not serials:
            return {}
        with ThreadPoolExecutor(max_workers=min(16, len(serials))) as pool:
            return dict(zip(serials, pool.map(self.prepare, serials)))
