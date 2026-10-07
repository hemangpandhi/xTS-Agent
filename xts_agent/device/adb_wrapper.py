"""ADB command wrapper with timeouts and safe shell helpers."""

from __future__ import annotations

import logging
import shlex
import subprocess
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_ADB_TIMEOUT = 30


class AdbError(Exception):
    pass


class AdbWrapper:
    @staticmethod
    def _run_cmd(cmd: list[str], timeout: Optional[int] = DEFAULT_ADB_TIMEOUT, silent: bool = False) -> str:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or "").strip()
            if not silent:
                logger.error("ADB command failed: %s\nError: %s", " ".join(cmd), err)
            raise AdbError(f"Command failed: {err}") from e
        except subprocess.TimeoutExpired as e:
            if not silent:
                logger.error("ADB command timed out: %s", " ".join(cmd))
            raise AdbError("Command timed out") from e

    @classmethod
    def devices(cls) -> list[str]:
        output = cls._run_cmd(["adb", "devices"], timeout=15)
        serials: List[str] = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    @classmethod
    def shell(cls, serial: str, command: str, timeout: Optional[int] = DEFAULT_ADB_TIMEOUT, silent: bool = False) -> str:
        # Use sh -c so pipes/redirects work consistently
        return cls._run_cmd(
            ["adb", "-s", serial, "shell", command],
            timeout=timeout,
            silent=silent,
        )

    @classmethod
    def pull(cls, serial: str, remote: str, local: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "pull", remote, local], timeout=120)

    @classmethod
    def push(cls, serial: str, local: str, remote: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "push", local, remote], timeout=120)

    @classmethod
    def install(cls, serial: str, apk_path: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "install", "-r", "-g", apk_path], timeout=180)

    @classmethod
    def uninstall(cls, serial: str, package: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "uninstall", package], timeout=60)

    @classmethod
    def reboot(cls, serial: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "reboot"], timeout=30)

    @classmethod
    def bugreport(cls, serial: str, output_path: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "bugreport", output_path], timeout=300)

    @classmethod
    def logcat(cls, serial: str, output_path: str, duration_secs: int) -> None:
        proc = None
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                proc = subprocess.Popen(
                    ["adb", "-s", serial, "logcat"],
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                try:
                    proc.wait(timeout=duration_secs)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        finally:
            if proc and proc.poll() is None:
                proc.kill()

    @classmethod
    def screenshot(cls, serial: str, output_path: str) -> None:
        remote_path = "/data/local/tmp/screenshot.png"
        cls.shell(serial, f"screencap -p {shlex.quote(remote_path)}")
        cls.pull(serial, remote_path, output_path)
        cls.shell(serial, f"rm {shlex.quote(remote_path)}")

    @classmethod
    def get_prop(cls, serial: str, prop_name: str) -> str:
        return cls.shell(serial, f"getprop {shlex.quote(prop_name)}").strip()

    @classmethod
    def wait_for_device(cls, serial: str, timeout: int = 60) -> None:
        cls._run_cmd(["adb", "-s", serial, "wait-for-device"], timeout=timeout)

    @classmethod
    def connect(cls, host_port: str) -> None:
        cls._run_cmd(["adb", "connect", host_port], timeout=20)
