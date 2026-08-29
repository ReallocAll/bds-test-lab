from __future__ import annotations

import ctypes
import os

from endstone.plugin import Plugin
from packaging.specifiers import SpecifierSet
from packaging.version import Version


class DependencyPlugin(Plugin):
    """Keep real Python and user ctypes dependency paths active during sampling."""

    api_version = "0.11"

    def on_enable(self) -> None:
        self.iterations = max(100, int(os.environ.get("SPARK_PYTHON_DEPENDENCY_ITERATIONS", "1800")))
        self.specifier = SpecifierSet(">=1.0,<3.0")
        self.usleep = ctypes.CDLL(None).usleep
        self.usleep.argtypes = [ctypes.c_uint]
        self.usleep.restype = ctypes.c_int
        self.server.scheduler.run_task(self, self.dependency_tick, delay=0, period=1)
        self.logger.info(f"Spark Python dependency test enabled: iterations={self.iterations}")

    def dependency_tick(self) -> int:
        # Keep a normal user-owned ctypes -> libffi -> libc path sampled long
        # enough to prove Spark's observer filtering does not hide it globally.
        self.usleep(3_000)

        matched = 0
        specifier = self.specifier
        for index in range(self.iterations):
            version = Version(f"{1 + (index & 1)}.{index % 100}.{(index * 7) % 1000}")
            if specifier.contains(version, prereleases=True):
                matched += 1
        return matched
