from __future__ import annotations

import os
import shutil
import sys

from controller.python_attribution_performance import PythonAttributionPerformance, main
from controller.python_attribution_validation import PLUGIN_SOURCE
from controller.python_evidence_provenance import (
    validate_bds_version,
    validate_component_provenance,
    validate_endstone_runtime_version,
)
from controller.run_test import IntegrationTest, run_checked

_ORIGINAL_BOOTSTRAP_SERVER = PythonAttributionPerformance.bootstrap_server


def _install_real_endstone_plugin(self: PythonAttributionPerformance) -> None:
    self.disable_bstats = True
    # Install only Endstone + Spark through the base integration harness. Calling
    # PythonAttributionValidation.install_artifacts here would also pip-install
    # the hotspot package into the runner interpreter, then deploy the same wheel
    # to the server plugin directory and create an ambiguous duplicate plugin.
    IntegrationTest.install_artifacts(self)
    spark = validate_component_provenance(self.metadata, "spark")
    endstone = validate_component_provenance(self.metadata, "endstone")
    runtime_version = validate_endstone_runtime_version()
    self.check(
        "exact-artifact-provenance",
        "PASS",
        spark_sha=spark.get("sha"),
        spark_run_id=spark.get("run_id"),
        endstone_sha=endstone.get("sha"),
        endstone_run_id=endstone.get("run_id"),
        endstone_artifact_id=(endstone.get("artifact") or {}).get("id"),
        endstone_runtime_version=runtime_version,
    )

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


def _bootstrap_exact_bds(self: PythonAttributionPerformance) -> None:
    _ORIGINAL_BOOTSTRAP_SERVER(self)
    assert self.server is not None
    observed = validate_bds_version(self.result, self.server.snapshot())
    self.check(
        "exact-bds-version",
        "PASS",
        observed_protocol=observed,
        expected_protocol=os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION", "").strip() or None,
        expected_full=os.environ.get("EXPECTED_BDS_VERSION", "").strip() or None,
    )


PythonAttributionPerformance.install_artifacts = _install_real_endstone_plugin
PythonAttributionPerformance.bootstrap_server = _bootstrap_exact_bds

if __name__ == "__main__":
    raise SystemExit(main())
