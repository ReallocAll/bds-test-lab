from __future__ import annotations

import os

from endstone.plugin import Plugin


class PluginB(Plugin):
    """Distinct user plugin B with a separate stable nested Python call chain."""

    api_version = "0.11"

    def on_enable(self) -> None:
        self.iterations = max(1000, int(os.environ.get("SPARK_MULTI_PLUGIN_ITERATIONS", "12000")))
        self.server.scheduler.run_task(self, self.tick_b, delay=0, period=1)
        self.logger.info(f"Spark multi-plugin B enabled: iterations={self.iterations}")

    def tick_b(self) -> int:
        return self.outer_b(self.iterations)

    def outer_b(self, iterations: int) -> int:
        return self.inner_b(iterations)

    @staticmethod
    def inner_b(iterations: int) -> int:
        value = 0x243F6A88
        for index in range(iterations):
            value ^= (index + 0x85A308D3) & 0xFFFFFFFF
            value = ((value << 11) | (value >> 21)) & 0xFFFFFFFF
            value = (value * 0x27D4EB2D) & 0xFFFFFFFF
        return value
