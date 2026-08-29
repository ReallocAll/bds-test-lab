from __future__ import annotations

import os

from endstone.plugin import Plugin
from packaging.specifiers import SpecifierSet
from packaging.version import Version


class DependencyPlugin(Plugin):
    """Keep a real third-party pure-Python dependency active during sampling."""

    api_version = "0.11"

    def on_enable(self) -> None:
        self.iterations = max(100, int(os.environ.get("SPARK_PYTHON_DEPENDENCY_ITERATIONS", "1800")))
        self.specifier = SpecifierSet(">=1.0,<3.0")
        self.server.scheduler.run_task(self, self.dependency_tick, delay=0, period=1)
        self.logger.info(f"Spark Python dependency test enabled: iterations={self.iterations}")

    def dependency_tick(self) -> int:
        matched = 0
        specifier = self.specifier
        for index in range(self.iterations):
            version = Version(f"{1 + (index & 1)}.{index % 100}.{(index * 7) % 1000}")
            if specifier.contains(version, prereleases=True):
                matched += 1
        return matched
