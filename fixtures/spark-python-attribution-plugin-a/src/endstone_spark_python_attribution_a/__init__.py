from __future__ import annotations

import os

from endstone.plugin import Plugin


class PluginA(Plugin):
    """Distinct user plugin A with a stable nested Python call chain."""

    api_version = "0.11"

    def on_enable(self) -> None:
        self.iterations = max(1000, int(os.environ.get("SPARK_MULTI_PLUGIN_ITERATIONS", "12000")))
        self.server.scheduler.run_task(self, self.tick_a, delay=0, period=1)
        self.logger.info(f"Spark multi-plugin A enabled: iterations={self.iterations}")

    def tick_a(self) -> int:
        return self.outer_a(self.iterations)

    def outer_a(self, iterations: int) -> int:
        return self.inner_a(iterations)

    @staticmethod
    def inner_a(iterations: int) -> int:
        value = 0x9E3779B9
        for index in range(iterations):
            value ^= (index + 0x517CC1B7) & 0xFFFFFFFF
            value = ((value << 7) | (value >> 25)) & 0xFFFFFFFF
            value = (value * 0x45D9F3B) & 0xFFFFFFFF
        return value
