#!/usr/bin/env python3
from __future__ import annotations

import os
import pathlib
import shutil
import sys
from typing import Any

from controller.python_attribution_performance import PythonAttributionPerformance, main
from controller.python_attribution_validation import PLUGIN_SOURCE
from controller.run_test import IntegrationTest, run_checked

_ORIGINAL_BOOTSTRAP_SERVER = PythonAttributionPerformance.bootstrap_server


def _positive_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}")
    return value


def validate_component_provenance(metadata: dict[str, Any], component: str) -> dict[str, Any]:
    components = metadata.get("components")
    observed = components.get(component) if isinstance(components, dict) else None
    if not isinstance(observed, dict):
        raise RuntimeError(f"artifact metadata is missing component {component!r}")

    env_prefix = component.upper()
    expected_sha = os.environ.get(f"EXPECTED_{env_prefix}_SHA", "").strip().lower()
    observed_sha = str(observed.get("sha") or "").strip().lower()
    if expected_sha and observed_sha != expected_sha:
        raise RuntimeError(
            f"{component} artifact SHA mismatch: observed={observed_sha!r} expected={expected_sha!r}"
        )

    expected_run_id = _positive_int_env(f"EXPECTED_{env_prefix}_RUN_ID")
    if expected_run_id is not None and observed.get("run_id") != expected_run_id:
        raise RuntimeError(
            f"{component} artifact run mismatch: observed={observed.get('run_id')!r} "
            f"expected={expected_run_id}"
        )

    expected_artifact_id = _positive_int_env(f"EXPECTED_{env_prefix}_ARTIFACT_ID")
    artifact = observed.get("artifact")
    observed_artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
    if expected_artifact_id is not None and observed_artifact_id != expected_artifact_id:
        raise RuntimeError(
            f"{component} artifact ID mismatch: observed={observed_artifact_id!r} "
            f"expected={expected_artifact_id}"
        )
    return observed


def validate_bds_version(result: dict[str, Any]) -> str | None:
    expected = os.environ.get("EXPECTED_BDS_VERSION", "").strip()
    observed_value = result.get("bds_version")
    observed = str(observed_value).strip() if observed_value is not None else ""
    if expected and observed != expected:
        raise RuntimeError(f"BDS version mismatch: observed={observed!r} expected={expected!r}")
    return observed or None


def _install_real_endstone_plugin(self: PythonAttributionPerformance) -> None:
    # Install only Endstone + Spark through the base integration harness. Calling
    # PythonAttributionValidation.install_artifacts here would also pip-install
    # the hotspot package into the runner interpreter, then deploy the same wheel
    # to the server plugin directory and create an ambiguous duplicate plugin.
    IntegrationTest.install_artifacts(self)
    spark = validate_component_provenance(self.metadata, "spark")
    endstone = validate_component_provenance(self.metadata, "endstone")
    self.check(
        "exact-artifact-provenance",
        "PASS",
        spark_sha=spark.get("sha"),
        spark_run_id=spark.get("run_id"),
        endstone_sha=endstone.get("sha"),
        endstone_run_id=endstone.get("run_id"),
        endstone_artifact_id=(endstone.get("artifact") or {}).get("id"),
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
    observed = validate_bds_version(self.result)
    self.check(
        "exact-bds-version",
        "PASS",
        observed=observed,
        expected=os.environ.get("EXPECTED_BDS_VERSION", "").strip() or None,
    )


PythonAttributionPerformance.install_artifacts = _install_real_endstone_plugin
PythonAttributionPerformance.bootstrap_server = _bootstrap_exact_bds

if __name__ == "__main__":
    raise SystemExit(main())
