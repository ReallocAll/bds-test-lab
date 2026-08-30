from __future__ import annotations

import importlib.metadata
import os
from typing import Any


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
        raise TypeError(f"artifact metadata is missing component {component!r}")

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
            f"{component} artifact run mismatch: observed={observed.get('run_id')!r} expected={expected_run_id}"
        )

    expected_artifact_id = _positive_int_env(f"EXPECTED_{env_prefix}_ARTIFACT_ID")
    artifact = observed.get("artifact")
    observed_artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
    if expected_artifact_id is not None and observed_artifact_id != expected_artifact_id:
        raise RuntimeError(
            f"{component} artifact ID mismatch: observed={observed_artifact_id!r} expected={expected_artifact_id}"
        )
    return observed


def validate_endstone_runtime_version() -> str | None:
    expected = os.environ.get("EXPECTED_ENDSTONE_VERSION", "").strip()
    if not expected:
        return None
    try:
        observed = importlib.metadata.version("endstone")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("Endstone runtime package is not installed") from exc
    if observed != expected:
        raise RuntimeError(f"Endstone runtime version mismatch: observed={observed!r} expected={expected!r}")
    return observed


def validate_bds_version(result: dict[str, Any]) -> str | None:
    expected = os.environ.get("EXPECTED_BDS_VERSION", "").strip()
    observed_value = result.get("bds_version")
    observed = str(observed_value).strip() if observed_value is not None else ""
    if expected and observed != expected:
        raise RuntimeError(f"BDS version mismatch: observed={observed!r} expected={expected!r}")
    return observed or None
