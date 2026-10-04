from __future__ import annotations
import subprocess
import time
import re
from pathlib import Path
from typing import Optional, List, Dict
import logging

logger = logging.getLogger(__name__)

class AdbError(Exception):
    pass

class AdbWrapper:
    @staticmethod
    def _run_cmd(cmd: list[str], timeout: Optional[int] = None) -> str:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logger.error(f"ADB command failed: {' '.join(cmd)}\nError: {e.stderr}")
            raise AdbError(f"Command failed: {e.stderr.strip()}") from e
        except subprocess.TimeoutExpired as e:
            logger.error(f"ADB command timed out: {' '.join(cmd)}")
            raise AdbError("Command timed out") from e

    @classmethod
    def devices(cls) -> list[str]:
        output = cls._run_cmd(["adb", "devices"])
        lines = output.splitlines()[1:] # Skip header
        return [line.split()[0] for line in lines if line.strip() and "device" in line]

    @classmethod
    def shell(cls, serial: str, command: str, timeout: Optional[int] = None) -> str:
        return cls._run_cmd(["adb", "-s", serial, "shell", command], timeout=timeout)

    @classmethod
    def pull(cls, serial: str, remote: str, local: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "pull", remote, local])

    @classmethod
    def push(cls, serial: str, local: str, remote: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "push", local, remote])

    @classmethod
    def install(cls, serial: str, apk_path: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "install", "-r", "-g", apk_path])

    @classmethod
    def uninstall(cls, serial: str, package: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "uninstall", package])

    @classmethod
    def reboot(cls, serial: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "reboot"])

    @classmethod
    def bugreport(cls, serial: str, output_path: str) -> None:
        cls._run_cmd(["adb", "-s", serial, "bugreport", output_path], timeout=300)

    @classmethod
    def logcat(cls, serial: str, output_path: str, duration_secs: int) -> None:
        try:
            with open(output_path, "w") as f:
                subprocess.run(["adb", "-s", serial, "logcat"], stdout=f, timeout=duration_secs)
        except subprocess.TimeoutExpired:
            pass # Expected

    @classmethod
    def screenshot(cls, serial: str, output_path: str) -> None:
        remote_path = "/data/local/tmp/screenshot.png"
        cls.shell(serial, f"screencap -p {remote_path}")
        cls.pull(serial, remote_path, output_path)
        cls.shell(serial, f"rm {remote_path}")

    @classmethod
    def get_prop(cls, serial: str, prop_name: str) -> str:
        return cls.shell(serial, f"getprop {prop_name}").strip()

    @classmethod
    def wait_for_device(cls, serial: str, timeout: int = 60) -> None:
        cls._run_cmd(["adb", "-s", serial, "wait-for-device"], timeout=timeout)

    @classmethod
    def connect(cls, host_port: str) -> None:
        cls._run_cmd(["adb", "connect", host_port])
