"""Optional upload of large run artifacts to S3-compatible object storage.

TradeFed result dirs reach gigabytes per CTS run; keeping them only as CI
artifacts (short expiry) or on one host's disk loses them. With
``artifacts.store: s3`` the agent uploads each suite's result zip plus the
reports and triage JSON, and records the URLs in the results database.
Works with AWS S3 or an on-prem MinIO (``endpoint_url``). Credentials come
from the standard AWS environment/config chain, never from YAML.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import Database

logger = logging.getLogger(__name__)


@dataclass
class ArtifactConfig:
    store: str = "none"  # none | s3
    bucket: str = ""
    prefix: str = "xts"
    endpoint_url: str = ""  # e.g. http://minio.lab:9000
    region: str = ""
    upload: List[str] = field(default_factory=lambda: ["results", "reports", "triage"])


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "x"


class ArtifactStore:
    def __init__(self, cfg: ArtifactConfig, client: Any = None):
        self.cfg = cfg
        self._client = client

    @property
    def enabled(self) -> bool:
        return self.cfg.store == "s3" and bool(self.cfg.bucket)

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError("S3 artifact upload needs: pip install 'xts-agent[s3]'") from exc
            kwargs: Dict[str, Any] = {}
            if self.cfg.endpoint_url:
                kwargs["endpoint_url"] = self.cfg.endpoint_url
            if self.cfg.region:
                kwargs["region_name"] = self.cfg.region
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def _put(self, path: Path, key: str) -> str:
        self.client.upload_file(str(path), self.cfg.bucket, key)
        return f"s3://{self.cfg.bucket}/{key}"

    def upload_run(
        self,
        plan_name: str,
        suites: Dict[str, Any],
        report_files: List[Path],
        triage_file: Optional[Path] = None,
    ) -> Dict[str, str]:
        """Upload a run's artifacts; returns {artifact name: s3 url}."""
        if not self.enabled:
            return {}
        base = f"{self.cfg.prefix.strip('/')}/{_slug(plan_name)}/{time.strftime('%Y%m%d_%H%M%S')}"
        urls: Dict[str, str] = {}
        if "results" in self.cfg.upload:
            for name, suite in suites.items():
                rdir = Path(suite.results_dir) if suite.results_dir else None
                if not rdir or not rdir.is_dir():
                    continue
                urls[f"{name}/results"] = self._upload_results_dir(rdir, f"{base}/{_slug(name)}")
        if "reports" in self.cfg.upload:
            for f in report_files:
                if Path(f).is_file():
                    urls[f"reports/{Path(f).name}"] = self._put(Path(f), f"{base}/reports/{Path(f).name}")
        if "triage" in self.cfg.upload and triage_file and triage_file.is_file():
            urls["triage"] = self._put(triage_file, f"{base}/triage/{triage_file.name}")
        logger.info("Uploaded %s artifact(s) to s3://%s/%s", len(urls), self.cfg.bucket, base)
        return urls

    def _upload_results_dir(self, rdir: Path, key_base: str) -> str:
        # TradeFed already writes <results_dir>.zip next to the dir; reuse it
        tf_zip = rdir.with_name(rdir.name + ".zip")
        if tf_zip.is_file():
            return self._put(tf_zip, f"{key_base}/{tf_zip.name}")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(shutil.make_archive(str(Path(tmp) / rdir.name), "zip", rdir))
            return self._put(archive, f"{key_base}/{archive.name}")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_artifacts (
    id {pk},
    plan TEXT,
    name TEXT,
    url TEXT,
    recorded_at {float}
)
"""


def record_artifacts(db: Database, plan_name: str, urls: Dict[str, str]) -> None:
    if not urls:
        return
    db.ddl(_SCHEMA)
    with db.transaction() as tx:
        tx.executemany(
            "INSERT INTO run_artifacts (plan, name, url, recorded_at) VALUES (?, ?, ?, ?)",
            [(plan_name, name, url, time.time()) for name, url in urls.items()],
        )
