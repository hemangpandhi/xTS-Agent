"""Shard management logic for multi-device test execution."""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar, Dict, List, Optional, Sequence, Union

from xts_agent.config_loader import SuiteConfig

logger = logging.getLogger(__name__)


class ShardManager:
    """Manages sharding across available devices."""

    SUITE_ESTIMATES: ClassVar[Dict[str, float]] = {
        "CTS": 40.0,
        "VTS": 20.0,
        "STS": 8.0,
        "GTS": 15.0,
        "ATS": 10.0,
        "CATBOX": 12.0,
    }

    def __init__(self, device_manager: Any):
        self.device_manager = device_manager

    def calculate_shard_count(
        self,
        available_devices: int,
        suite_config: Union[SuiteConfig, dict],
    ) -> int:
        if available_devices <= 0:
            return 0

        if isinstance(suite_config, SuiteConfig):
            enabled = suite_config.sharding.enabled
            configured = suite_config.sharding.shard_count
            max_shards = suite_config.sharding.max_shards
        else:
            enabled = suite_config.get("enabled", True)
            configured = suite_config.get("shard_count", 1)
            max_shards = suite_config.get("max_shards", 16)

        if not enabled:
            return 1

        if isinstance(configured, str) and configured.lower() == "auto":
            shard_count = min(available_devices, int(max_shards))
        else:
            try:
                shard_count = int(configured)
            except (TypeError, ValueError):
                logger.warning("Invalid shard_count %r; using auto", configured)
                shard_count = min(available_devices, int(max_shards))

        shard_count = max(1, min(shard_count, available_devices, int(max_shards)))
        logger.info(
            "Shard count resolved to %s (available=%s, configured=%r)",
            shard_count,
            available_devices,
            configured,
        )
        return shard_count

    def validate_sharding(self, suite_name: str, shard_count: int) -> bool:
        if shard_count <= 1:
            return True
        return suite_name.upper() in self.SUITE_ESTIMATES

    def estimate_execution_time(self, suite_name: str, shard_count: int) -> float:
        baseline = self.SUITE_ESTIMATES.get(suite_name.upper(), 10.0)
        shard_count = max(shard_count, 1)
        overhead_factor = 1.0 + (0.05 * (shard_count - 1))
        return (baseline / shard_count) * overhead_factor

    def suite_weight(
        self, suite_name: str, history_estimate: Optional[Callable[[str], Optional[float]]] = None
    ) -> float:
        """Expected device-hours for a suite: measured history first, else static estimate."""
        if history_estimate is not None:
            measured = history_estimate(suite_name)
            if measured:
                return measured
        return self.SUITE_ESTIMATES.get(suite_name.upper(), 10.0)

    @staticmethod
    def split_devices(
        devices: Sequence[Any],
        weights: Dict[str, float],
        caps: Optional[Dict[str, int]] = None,
    ) -> Dict[str, List[Any]]:
        """Split devices across concurrently running suites in proportion to weight.

        Every suite gets at least one device; a suite never gets more than its
        cap (max_shards, or 1 when sharding is disabled). Devices a capped suite
        cannot use flow to the others. Requires len(devices) >= len(weights).
        """
        names = list(weights)
        if len(devices) < len(names):
            raise ValueError(f"{len(devices)} device(s) cannot cover {len(names)} concurrent suites")
        caps = {n: max(1, int((caps or {}).get(n, len(devices)))) for n in names}
        counts = {n: 1 for n in names}
        remaining = len(devices) - len(names)
        while remaining > 0:
            open_names = [n for n in names if counts[n] < caps[n]]
            if not open_names:
                break  # every suite is at its cap; leftover devices stay idle
            total = sum(weights[n] for n in open_names) or 1.0
            # Give the next device to the suite furthest below its fair share
            share = {n: weights[n] / total * (remaining + sum(counts[m] for m in open_names)) for n in open_names}
            pick = max(open_names, key=lambda n: (share[n] - counts[n], weights[n]))
            counts[pick] += 1
            remaining -= 1
        split: Dict[str, List[Any]] = {}
        it = iter(devices)
        for n in names:
            split[n] = [next(it) for _ in range(counts[n])]
        return split
