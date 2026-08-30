from __future__ import annotations

import importlib.metadata
import os
import re
from collections.abc import Iterable
from typing import Any

_BDS_VERSION_LINE_RE = re.compile(r"\bVersion:\s*([0-9]+(?:\.[0-9]+)+)\s*$")


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


def _observed_full_bds_version(server_lines: Iterable[str]) -> str | None:
    versions: list[str] = []
    for line in server_lines:
        match = _BDS_VERSION_LINE_RE.search(str(line).strip())
        if match:
            versions.append(match.group(1))
    if not versions:
        return None
    unique = list(dict.fromkeys(versions))
    if len(unique) != 1:
        raise RuntimeError(f"BDS runtime reported multiple full versions: {unique}")
    return unique[0]


def _derived_bds_protocol_version(full_version: str) -> str:
    parts = full_version.split(".")
    if len(parts) != 4 or parts[0] != "1" or not all(part.isdigit() for part in parts):
        raise RuntimeError(f"cannot derive BDS protocol version from full version {full_version!r}")
    return f"{int(parts[1])}.{int(parts[2])}"


def validate_bds_version(
    result: dict[str, Any],
    server_lines: Iterable[str] | None = None,
) -> str | None:
    """Validate Endstone's protocol value and, with runtime evidence, the full BDS version.

    Endstone writes the Bedrock protocol/marketing version (for example ``26.44``)
    to ``version.txt``. The BDS process logs its full package version (for example
    ``1.26.44.3``). Exact evidence runners pass the live server log, so both values
    are checked and the protocol value is never mistaken for the full version.
    """

    expected_full = os.environ.get("EXPECTED_BDS_VERSION", "").strip()
    explicit_protocol = os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION", "").strip()
    observed_value = result.get("bds_version")
    observed_protocol = str(observed_value).strip() if observed_value is not None else ""

    if server_lines is not None:
        expected_protocol = explicit_protocol or (
            _derived_bds_protocol_version(expected_full) if expected_full else ""
        )
        if expected_protocol and observed_protocol != expected_protocol:
            raise RuntimeError(
                "BDS protocol version mismatch: "
                f"observed={observed_protocol!r} expected={expected_protocol!r}"
            )
        if expected_full:
            observed_full = _observed_full_bds_version(server_lines)
            if observed_full != expected_full:
                raise RuntimeError(
                    f"BDS full version mismatch: observed={observed_full!r} expected={expected_full!r}"
                )
        return observed_protocol or None

    if explicit_protocol:
        if observed_protocol != explicit_protocol:
            raise RuntimeError(
                "BDS protocol version mismatch: "
                f"observed={observed_protocol!r} expected={explicit_protocol!r}"
            )
        if expected_full:
            raise RuntimeError("BDS full-version runtime evidence is required for exact provenance")
        return observed_protocol or None

    # Legacy callers historically stored a full version directly in bds_version.
    # Preserve that behavior when no live server-log evidence is supplied.
    if expected_full and observed_protocol != expected_full:
        raise RuntimeError(
            f"BDS version mismatch: observed={observed_protocol!r} expected={expected_full!r}"
        )
    return observed_protocol or None
