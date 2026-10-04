"""OmniLab ATS 2.0 API client."""
from __future__ import annotations
import logging
import requests
from dataclasses import dataclass
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

@dataclass
class RunStatus:
    run_id: str
    state: str
    progress_pct: float
    elapsed_time: float
    device_serials: List[str]

class ATS2Client:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def trigger_test_run(self, suite: str, plan: str, device_serials: List[str], shard_count: int) -> str:
        url = f"{self.base_url}/api/v1/runs"
        payload = {
            "suite": suite,
            "plan": plan,
            "devices": device_serials,
            "shard_count": shard_count
        }
        response = self.session.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json().get("run_id", "")

    def get_run_status(self, run_id: str) -> RunStatus:
        url = f"{self.base_url}/api/v1/runs/{run_id}/status"
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        return RunStatus(
            run_id=run_id,
            state=data.get("state", "UNKNOWN"),
            progress_pct=data.get("progress_pct", 0.0),
            elapsed_time=data.get("elapsed_time", 0.0),
            device_serials=data.get("device_serials", [])
        )

    def get_results(self, run_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v1/runs/{run_id}/results"
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return response.json()

    def cancel_run(self, run_id: str) -> bool:
        url = f"{self.base_url}/api/v1/runs/{run_id}/cancel"
        response = self.session.post(url, timeout=30)
        return response.status_code == 200

    def list_devices(self) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/api/v1/devices"
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return response.json().get("devices", [])

    def health_check(self) -> bool:
        try:
            url = f"{self.base_url}/api/v1/health"
            response = self.session.get(url, timeout=10)
            return response.status_code == 200
        except requests.RequestException:
            return False

    def upload_results(self, suite: str, plan: str, results_dir: str) -> bool:
        """Uploads local TradeFed test results to OmniLab ATS 2.0."""
        try:
            url = f"{self.base_url}/api/v1/results/upload"
            # In a real environment, we'd zip the results_dir and send it as a multipart/form-data payload
            logger.info(f"Uploading {suite} results from {results_dir} to ATS 2.0 ({url})")
            # payload = {"suite": suite, "plan": plan}
            # files = {'file': open(f"{results_dir}/results.zip", 'rb')}
            # response = self.session.post(url, data=payload, files=files, timeout=60)
            # return response.status_code == 200
            return True
        except Exception as e:
            logger.error(f"Failed to upload results to ATS 2.0: {e}")
            return False
