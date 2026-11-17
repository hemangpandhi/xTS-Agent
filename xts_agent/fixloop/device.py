"""DUT access for the fix loop: health, recovery and a read-only state snapshot."""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Read-only probes captured before every diagnosis. They are evidence for the
# report and context for the LLM; none of them changes device state.
SNAPSHOT_PROBES: Dict[str, str] = {
    "fingerprint": "getprop ro.build.fingerprint",
    "build_type": "getprop ro.build.type",
    "boot_completed": "getprop sys.boot_completed",
    "current_user": "am get-current-user",
    # Default display only: secondary displays (cluster) are listed first
    "focus": "dumpsys window displays 2>/dev/null | awk '/Display: mDisplayId=0/{d=1} d&&/mCurrentFocus|mFocusedApp/{print; n++} n==2{exit}'",
    # The focused window's type and flags show a system window that takes
    # focus (e.g. a bar without FLAG_NOT_FOCUSABLE); its owner uid names the app
    "focused_window": (
        "f=$(dumpsys window displays 2>/dev/null | awk '/Display: mDisplayId=0/{d=1} d&&/mCurrentFocus/{print; exit}' "
        "| grep -oE 'Window\\{[^}]*\\}'); echo \"mCurrentFocus=${f:-null}\"; "
        "t=${f##* }; t=${t%\\}}; [ -n \"$t\" ] && dumpsys window windows 2>/dev/null | grep -A40 -F \" $t}:\" "
        "| grep -m3 -E 'ty=|fl=|mOwnerUid' | cut -c1-220; "
        "u=$(dumpsys window windows 2>/dev/null | grep -A40 -F \" $t}:\" | grep -m1 -oE 'mOwnerUid=[0-9]+' | cut -d= -f2); "
        "[ -n \"$u\" ] && echo \"owner packages: $(pm list packages --uid $u 2>/dev/null | head -3 | tr '\\n' ' ')\""
    ),
    "top_activity": "dumpsys activity activities | grep -E 'topResumedActivity|ResumedActivity' | head -3",
    "keyguard": "dumpsys window | grep -iE 'isKeyguardShowing|mShowingLockscreen|mDreamingLockscreen' | head -3",
    "wakefulness": "dumpsys power | grep -E 'mWakefulness=' | head -1",
    "wifi": "cmd wifi status | head -2",
    "network": "dumpsys connectivity | grep -E 'Active default network|VALIDATED' | head -2 | cut -c1-300",
    "dns": "ping -c1 -W3 www.google.com 2>&1 | head -2",
}


@dataclass
class ShellResult:
    rc: int
    out: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class DeviceSession:
    """One leased DUT. ``runner`` is injectable so tests never need adb."""

    def __init__(
        self,
        serial: str,
        adb: str = "adb",
        restart_command: str = "",
        boot_timeout_secs: int = 300,
        reconnect_wait_secs: int = 60,
        poll_secs: float = 5,
        runner: Optional[Callable[[List[str], int], Tuple[int, str]]] = None,
    ):
        self.serial = serial
        self.adb = adb
        self.restart_command = restart_command
        self.boot_timeout_secs = boot_timeout_secs
        self.reconnect_wait_secs = reconnect_wait_secs
        self.poll_secs = poll_secs
        self._runner = runner or self._subprocess_runner

    @staticmethod
    def _subprocess_runner(argv: List[str], timeout: int) -> Tuple[int, str]:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
            return proc.returncode, (proc.stdout + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            return 124, f"timeout after {timeout}s: {' '.join(argv)}"
        except OSError as exc:
            return 127, str(exc)

    def adb_cmd(self, *args: str, timeout: int = 30) -> ShellResult:
        rc, out = self._runner([self.adb, "-s", self.serial, *args], timeout)
        return ShellResult(rc, out)

    def shell(self, command: str, timeout: int = 30) -> ShellResult:
        return self.adb_cmd("shell", command, timeout=timeout)

    def healthy(self) -> Tuple[bool, str]:
        state = self.adb_cmd("get-state", timeout=10)
        if not state.ok or state.out.strip() != "device":
            return False, f"adb state: {state.out.strip() or 'unknown'}"
        boot = self.shell("getprop sys.boot_completed", timeout=10)
        if boot.out.strip() != "1":
            return False, "boot not completed"
        return True, "online"

    def wait_boot(self, timeout: Optional[int] = None) -> bool:
        deadline = time.monotonic() + (self.boot_timeout_secs if timeout is None else timeout)
        while True:
            ok, _ = self.healthy()
            if ok:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll_secs)

    def recover(self) -> Tuple[bool, str]:
        """Bring the DUT back: reconnect, then the configured restart command."""
        ok, why = self.healthy()
        if ok:
            return True, "online"
        logger.warning("%s unhealthy (%s); reconnecting", self.serial, why)
        if ":" in self.serial:  # adb over TCP (Cuttlefish, remote labs)
            self._runner([self.adb, "disconnect", self.serial], 15)
            self._runner([self.adb, "connect", self.serial], 15)
        else:
            self.adb_cmd("reconnect", timeout=15)
        if self.wait_boot(self.reconnect_wait_secs):
            return True, f"recovered after reconnect ({why})"
        if self.restart_command:
            cmd = self.restart_command.format(serial=self.serial, cvd_instance=self.cvd_instance())
            logger.warning("%s still unhealthy; running restart command: %s", self.serial, cmd)
            rc, out = self._runner(shlex.split(cmd), 600)
            if rc == 0 and self.wait_boot():
                return True, f"recovered after restart ({why})"
            return False, f"restart failed rc={rc}: {out[-300:]}"
        return False, f"device unhealthy: {why}"

    def cvd_instance(self) -> str:
        """Cuttlefish instance number for an adb port serial (0.0.0.0:6520 -> 1)."""
        m = re.search(r":(65\d\d)$", self.serial)
        return str(int(m.group(1)) - 6519) if m else ""

    def reboot(self) -> bool:
        self.adb_cmd("reboot", timeout=60)
        time.sleep(10)
        return self.wait_boot()

    def snapshot(self) -> Dict[str, str]:
        snap: Dict[str, str] = {}
        for key, cmd in SNAPSHOT_PROBES.items():
            res = self.shell(cmd, timeout=20)
            snap[key] = res.out.strip()[:600]
        return snap


PACKAGE_RE = re.compile(r"\b([a-z][a-z0-9_]*(?:\.[a-zA-Z0-9_]+){2,})\b")


def packages_in(text: str) -> List[str]:
    """Java package / app ids mentioned in text, in order of appearance."""
    seen: List[str] = []
    for m in PACKAGE_RE.finditer(text or ""):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen
