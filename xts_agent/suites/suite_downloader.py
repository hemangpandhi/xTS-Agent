from __future__ import annotations
import urllib.request
import zipfile
import hashlib
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class SuiteDownloader:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def download(self, url: str, expected_sha256: str = "") -> Path:
        filename = url.split('/')[-1]
        local_path = self.cache_dir / filename
        
        if local_path.exists():
            if expected_sha256:
                if self._verify_hash(local_path, expected_sha256):
                    logger.info(f"Using cached file {filename}")
                    return local_path
                else:
                    logger.warning(f"Hash mismatch for {filename}, re-downloading")
            else:
                logger.info(f"Using cached file {filename} (no hash verification)")
                return local_path

        logger.info(f"Downloading {url} to {local_path}")
        urllib.request.urlretrieve(url, local_path)
        
        if expected_sha256 and not self._verify_hash(local_path, expected_sha256):
            local_path.unlink()
            raise ValueError(f"Downloaded file {filename} failed hash verification")
            
        return local_path

    def _verify_hash(self, path: Path, expected_hash: str) -> bool:
        sha256_hash = hashlib.sha256()
        with open(path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest() == expected_hash

    def extract(self, zip_path: Path, target_dir: Path) -> None:
        logger.info(f"Extracting {zip_path} to {target_dir}")
        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(target_dir)
