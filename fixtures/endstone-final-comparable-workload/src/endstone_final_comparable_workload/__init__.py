from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from endstone.plugin import Plugin


class ComparableWorkloadPlugin(Plugin):
    """Fixed server-thread work and comparable MSPT/TPS observations."""

    api_version = "0.11"

    def on_enable(self) -> None:
        self.iterations = self._positive_env("ENDSTONE_COMPARABLE_WORKLOAD_ITERATIONS", 9000)
        raw_metrics_path = os.environ.get("ENDSTONE_COMPARABLE_WORKLOAD_METRICS", "").strip()
        self.metrics_path = Path(raw_metrics_path) if raw_metrics_path else None
        self._samples: list[dict[str, Any]] = []
        self._ticks = 0
        self._checksum = 0
        self.server.scheduler.run_task(self, self.workload_tick, delay=0, period=1)
        self.logger.info(f"Endstone comparable workload enabled: iterations={self.iterations}")

    @staticmethod
    def _positive_env(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a positive integer") from exc
        if value <= 0:
            raise RuntimeError(f"{name} must be a positive integer")
        return value

    def workload_tick(self) -> None:
        value = self._checksum
        for index in range(self.iterations):
            value ^= (index + 0x9E3779B9) & 0xFFFFFFFF
            value = ((value << 5) | (value >> 27)) & 0xFFFFFFFF
            value = (value * 0x45D9F3B) & 0xFFFFFFFF
        self._checksum = value
        self._ticks += 1
        if self.metrics_path is None:
            return
        self._samples.append(
            {
                "monotonic_ns": time.monotonic_ns(),
                "mspt": float(self.server.current_mspt),
                "tps": float(self.server.current_tps),
            }
        )

    def on_disable(self) -> None:
        if self.metrics_path is None:
            return
        payload = {
            "metric": "endstone_server_current_mspt_tps",
            "iterations": self.iterations,
            "ticks": self._ticks,
            "checksum": self._checksum,
            "samples": self._samples,
            "disabled_monotonic_ns": time.monotonic_ns(),
        }
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.metrics_path.with_suffix(self.metrics_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.metrics_path)
        self.logger.info(f"Endstone comparable workload disabled: ticks={self._ticks}")
