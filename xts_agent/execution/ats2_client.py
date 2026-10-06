"""OmniLab ATS 2.0 API client."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class RunStatus:
    run_id: str
    state: str
    progress_pct: float
    elapsed_time: float
    device_serials: List[str]


class ATS2Client:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_secs: int = 30,
        enabled: bool = True,
        upload_path: str = "/api/v1/results/upload",
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout_secs = timeout_secs
        self.enabled = enabled and bool(self.base_url)
        self.upload_path = "/" + upload_path.lstrip("/")
        if self.enabled:
            logger.warning(
                "ATS 2.0 upload is EXPERIMENTAL: endpoint %s%s has not been verified against "
                "an OmniLab ATS deployment; confirm the API with your ATS team",
                self.base_url,
                self.upload_path,
            )
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})

    def trigger_test_run(
        self, suite: str, plan: str, device_serials: List[str], shard_count: int
    ) -> str:
        if not self.enabled:
            raise RuntimeError("ATS2 client is disabled")
        url = f"{self.base_url}/api/v1/runs"
        payload = {
            "suite": suite,
            "plan": plan,
            "devices": device_serials,
            "shard_count": shard_count,
        }
        response = self.session.post(url, json=payload, timeout=self.timeout_secs)
        response.raise_for_status()
        return response.json().get("run_id", "")

    def get_run_status(self, run_id: str) -> RunStatus:
        url = f"{self.base_url}/api/v1/runs/{run_id}/status"
        response = self.session.get(url, timeout=self.timeout_secs)
        response.raise_for_status()
        data = response.json()
        return RunStatus(
            run_id=run_id,
            state=data.get("state", "UNKNOWN"),
            progress_pct=data.get("progress_pct", 0.0),
            elapsed_time=data.get("elapsed_time", 0.0),
            device_serials=data.get("device_serials", []),
        )

    def get_results(self, run_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v1/runs/{run_id}/results"
        response = self.session.get(url, timeout=self.timeout_secs)
        response.raise_for_status()
        return response.json()

    def cancel_run(self, run_id: str) -> bool:
        url = f"{self.base_url}/api/v1/runs/{run_id}/cancel"
        response = self.session.post(url, timeout=self.timeout_secs)
        return response.status_code == 200

    def list_devices(self) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/api/v1/devices"
        response = self.session.get(url, timeout=self.timeout_secs)
        response.raise_for_status()
        return response.json().get("devices", [])

    def health_check(self) -> bool:
        if not self.enabled:
            return False
        try:
            url = f"{self.base_url}/api/v1/health"
            response = self.session.get(url, timeout=min(self.timeout_secs, 10))
            return response.status_code == 200
        except requests.RequestException:
            return False

    def upload_results(self, suite: str, plan: str, results_path: str) -> bool:
        """Upload a zip (or directory zipped by caller) to OmniLab ATS 2.0."""
        if not self.enabled:
            logger.info("ATS2 upload skipped (disabled or missing base_url)")
            return False

        path = Path(results_path)
        if not path.exists():
            logger.error("ATS2 upload path does not exist: %s", path)
            return False

        url = f"{self.base_url}{self.upload_path}"
        try:
            logger.info("Uploading %s results from %s to ATS 2.0 (%s)", suite, path, url)
            with open(path, "rb") as fh:
                files = {"file": (path.name, fh, "application/zip")}
                data = {"suite": suite, "plan": plan}
                response = self.session.post(
                    url, data=data, files=files, timeout=max(self.timeout_secs, 60)
                )
            if response.status_code == 200:
                logger.info("ATS2 upload succeeded for suite %s", suite)
                return True
            logger.error(
                "ATS2 upload failed (%s): %s", response.status_code, response.text[:500]
            )
            return False
        except requests.RequestException as exc:
            logger.error("Failed to upload results to ATS 2.0: %s", exc)
            return False
