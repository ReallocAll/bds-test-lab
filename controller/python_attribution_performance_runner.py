#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import shutil
import sys

from controller.python_attribution_performance import PythonAttributionPerformance, main
from controller.python_attribution_validation import PLUGIN_SOURCE
from controller.run_test import run_checked


def _install_real_endstone_plugin(self: PythonAttributionPerformance) -> None:
    # Keep the benchmark deployment identical to the correctness E2E: Spark and
    # Endstone first, then hand the hotspot wheel to Endstone's normal plugin
    # loader instead of importing it from the runner's global site-packages.
    super(PythonAttributionPerformance, self).install_artifacts()
    wheel_dir = self.root / "hotspot-wheel"
    shutil.rmtree(wheel_dir, ignore_errors=True)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            str(PLUGIN_SOURCE),
        ],
        timeout=180,
    )
    wheels = sorted(wheel_dir.glob("endstone_spark_python_hotspot_test-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one hotspot plugin wheel, got: {wheels}")
    plugin_dir = self.server_dir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    target = plugin_dir / wheels[0].name
    shutil.copy2(wheels[0], target)
    self.check("python-hotspot-plugin-installed", "PASS", str(target.relative_to(self.root)))


PythonAttributionPerformance.install_artifacts = _install_real_endstone_plugin

if __name__ == "__main__":
    raise SystemExit(main())
