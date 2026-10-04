"""Shard management logic for multi-device test execution."""
from __future__ import annotations
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class ShardManager:
    """Manages sharding across available devices."""
    
    SUITE_ESTIMATES = {
        "CTS": 40.0,
        "VTS": 20.0,
        "STS": 8.0,
        "GTS": 15.0,
        "ATS": 10.0,
        "CATBOX": 12.0
    }
    
    def __init__(self, device_manager: Any):
        self.device_manager = device_manager

    def calculate_shard_count(self, available_devices: int, suite_config: Dict[str, Any]) -> int:
        max_efficient_shards = suite_config.get("max_shards", 10)
        config_shards = suite_config.get("shard_count")
        
        if config_shards is not None:
            return min(available_devices, config_shards)
        
        return min(available_devices, max_efficient_shards)

    def validate_sharding(self, suite_name: str, shard_count: int) -> bool:
        if shard_count <= 1:
            return True
        if suite_name.upper() in self.SUITE_ESTIMATES:
            return True
        return False

    def estimate_execution_time(self, suite_name: str, shard_count: int) -> float:
        baseline = self.SUITE_ESTIMATES.get(suite_name.upper(), 10.0)
        if shard_count <= 0:
            shard_count = 1
        
        overhead_factor = 1.0 + (0.05 * (shard_count - 1))
        return (baseline / shard_count) * overhead_factor
