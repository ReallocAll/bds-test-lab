#!/usr/bin/env python3
"""Validate attribution for two independently packaged Python plugins."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import pathlib
import re
import shutil
import sys
from dataclasses import dataclass
from typing import Any

from controller.python_attribution_validation import PythonAttributionValidation
from controller.python_evidence_provenance import (
    validate_bds_version,
    validate_component_provenance,
    validate_endstone_runtime_version,
)
from controller.python_profile_payload import (
    ProfilePayload,
    fetch_viewer_payload,
    iter_leaf_paths,
    parse_sampler_data,
    profile_summary,
    python_nodes,
)
from controller.run_test import IntegrationTest, run_checked


@dataclass(frozen=True)
class MultiPluginSpec:
    name: str
    source_path: pathlib.Path
    wheel_prefix: str
    source_id: str
    module: str
    class_name: str
    chain: tuple[str, ...]
    log_marker: str


FIXTURE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
PLUGIN_SPECS = (
    MultiPluginSpec(
        name="plugin-a",
        source_path=FIXTURE_ROOT / "spark-python-attribution-plugin-a",
        wheel_prefix="spark_python_attribution_plugin_a",
        source_id="spark-python-attribution-plugin-a",
        module="endstone_spark_python_attribution_a",
        class_name="PluginA",
        chain=("PluginA.tick_a", "PluginA.outer_a", "PluginA.inner_a"),
        log_marker="spark multi-plugin a enabled",
    ),
    MultiPluginSpec(
        name="plugin-b",
        source_path=FIXTURE_ROOT / "spark-python-attribution-plugin-b",
        wheel_prefix="spark_python_attribution_plugin_b",
        source_id="spark-python-attribution-plugin-b",
        module="endstone_spark_python_attribution_b",
        class_name="PluginB",
        chain=("PluginB.tick_b", "PluginB.outer_b", "PluginB.inner_b"),
        log_marker="spark multi-plugin b enabled",
    ),
)
OBSERVER_TOKENS = (
    "pystartthunk",
    "pyresumethunk",
    "pythrowthunk",
    "pyreturnthunk",
    "pyyieldthunk",
    "pyunwindthunk",
    "pystartnativecallback",
    "pyresumenativecallback",
    "pythrownativecallback",
    "pyreturnnativecallback",
    "pyyieldnativecallback",
    "pyunwindnativecallback",
    "nativeeventcallback",
    "_endstone_spark_monitor",
)
ZERO_FAILURE_DIAGNOSTICS = (
    "Python monitoring callback failures",
    "Python shadow snapshot failures",
    "Python shadow overflows",
)
NONNEGATIVE_DIAGNOSTICS = (
    "Python native boundary misses",
    "Python thread mismatches",
    "Python unknown code IDs",
)
SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
BDS_FULL_VERSION_RE = re.compile(r"^1\.[0-9]+\.[0-9]+\.[0-9]+$")


def _exact_sha(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not SHA1_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a full 40-character commit SHA")
    return normalized


def _required_bds_version(value: str | None = None) -> tuple[str, str]:
    full = (value or os.environ.get("EXPECTED_BDS_VERSION", "")).strip()
    if not BDS_FULL_VERSION_RE.fullmatch(full):
        raise ValueError(
            "EXPECTED_BDS_VERSION must be an exact full BDS version in the form 1.<major>.<minor>.<revision>"
        )
    parts = full.split(".")
    protocol = f"{int(parts[1])}.{int(parts[2])}"
    explicit_protocol = os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION", "").strip()
    if explicit_protocol and explicit_protocol != protocol:
        raise ValueError(
            f"EXPECTED_BDS_PROTOCOL_VERSION mismatch: observed={explicit_protocol!r} expected={protocol!r}"
        )
    return full, protocol


def _installed_endstone_bds_target(expected_protocol: str) -> str:
    try:
        import endstone
    except ImportError as exc:
        raise RuntimeError("installed Endstone package is unavailable for exact BDS selection") from exc
    observed = str(getattr(endstone, "__minecraft_version__", "")).strip()
    if observed != expected_protocol:
        raise RuntimeError(
            "Endstone BDS download target mismatch: "
            f"observed={observed!r} expected_protocol={expected_protocol!r}"
        )
    return observed


def _validate_exact_bds_evidence(
    result: dict[str, Any], server_lines: list[str], expected_full: str, expected_protocol: str
) -> str:
    previous_full = os.environ.get("EXPECTED_BDS_VERSION")
    previous_protocol = os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION")
    os.environ["EXPECTED_BDS_VERSION"] = expected_full
    os.environ["EXPECTED_BDS_PROTOCOL_VERSION"] = expected_protocol
    try:
        observed = validate_bds_version(result, server_lines)
    finally:
        if previous_full is None:
            os.environ.pop("EXPECTED_BDS_VERSION", None)
        else:
            os.environ["EXPECTED_BDS_VERSION"] = previous_full
        if previous_protocol is None:
            os.environ.pop("EXPECTED_BDS_PROTOCOL_VERSION", None)
        else:
            os.environ["EXPECTED_BDS_PROTOCOL_VERSION"] = previous_protocol
    if observed != expected_protocol:
        raise RuntimeError(f"BDS protocol version mismatch: observed={observed!r} expected={expected_protocol!r}")
    return observed


def _canonical_plugin_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _diagnostic_count(diagnostics: dict[str, str], key: str) -> int:
    raw = diagnostics.get(key)
    if raw is None:
        raise RuntimeError(f"missing Python attribution diagnostic: {key}")
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"Python attribution diagnostic is not an integer: {key}={raw!r}") from exc
    if value < 0:
        raise RuntimeError(f"Python attribution diagnostic is negative: {key}={value}")
    return value


def _metadata_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    return decoded if isinstance(decoded, str) else value


def _contains_plugin_chain(profile: ProfilePayload, class_name: str, expected: tuple[str, ...]) -> bool:
    for _thread, path in iter_leaf_paths(profile):
        methods = [node.method_name for node in path if node.class_name == class_name]
        cursor = 0
        for method in methods:
            if cursor < len(expected) and method == expected[cursor]:
                cursor += 1
        if cursor == len(expected):
            return True
    return False


def validate_multi_plugin_profile(profile: ProfilePayload, profile_seconds: int = 60) -> dict[str, Any]:
    """Validate both plugin identities and source chains in one decoded profile."""

    if profile.sampler_mode != 0:
        raise RuntimeError(f"expected execution sampler mode 0, got {profile.sampler_mode}")
    if profile.duration_seconds < profile_seconds - 5:
        raise RuntimeError(f"profile too short: {profile.duration_seconds:.3f}s for requested {profile_seconds}s")
    root_weight = sum(thread.weight for thread in profile.threads)
    if not profile.threads or root_weight <= 0:
        raise RuntimeError("profile thread tree is empty or has zero root weight")

    diagnostics = profile.extra_metadata
    backend = _metadata_text(diagnostics.get("Python attribution backend"))
    enabled = _metadata_text(diagnostics.get("Python function attribution enabled"))
    if backend != "PEP669" or enabled != "true":
        raise RuntimeError(f"Python attribution is not active: backend={backend!r} enabled={enabled!r}")
    py_start_events = _diagnostic_count(diagnostics, "Python PY_START events")
    shadow_snapshot_attempts = _diagnostic_count(diagnostics, "Python shadow snapshot attempts")
    if py_start_events <= 0 or shadow_snapshot_attempts <= 0:
        raise RuntimeError(
            "Python attribution diagnostics report no active events: "
            f"PY_START={py_start_events} snapshots={shadow_snapshot_attempts}"
        )
    attribution_samples = _diagnostic_count(diagnostics, "Python attributed samples")
    if attribution_samples <= 0:
        raise RuntimeError("Python attribution diagnostics report no attributed samples")
    failure_counts = {key: _diagnostic_count(diagnostics, key) for key in ZERO_FAILURE_DIAGNOSTICS}
    if any(value != 0 for value in failure_counts.values()):
        raise RuntimeError(f"Python callback/shadow failures were observed: {failure_counts}")
    nonnegative_counts = {key: _diagnostic_count(diagnostics, key) for key in NONNEGATIVE_DIAGNOSTICS}

    all_python_nodes = python_nodes(profile)
    observer_nodes = [
        (thread.name, node.method_name)
        for thread in profile.threads
        for node in thread.nodes
        if any(token in f"{node.method_name} {node.method_desc}".lower() for token in OBSERVER_TOKENS)
    ]
    if observer_nodes:
        raise RuntimeError(f"observer frames leaked into the exported profile: {observer_nodes[:10]}")

    identities: dict[str, dict[str, Any]] = {}
    for spec in PLUGIN_SPECS:
        class_name = f"[Python] {spec.module}"
        plugin_nodes = [node for _thread, node in all_python_nodes if node.class_name == class_name]
        if not plugin_nodes:
            raise RuntimeError(f"profile contains no nodes for {spec.name}: {class_name}")
        if not any(node.line_number > 0 for node in plugin_nodes):
            raise RuntimeError(f"{spec.name} nodes are missing co_firstlineno")
        if not any(spec.module in node.method_desc for node in plugin_nodes):
            raise RuntimeError(f"{spec.name} nodes have no fixture source descriptor")
        source = profile.class_sources.get(class_name)
        if source is None or _canonical_plugin_key(source) != _canonical_plugin_key(spec.source_id):
            raise RuntimeError(f"{spec.name} class source mismatch: expected {spec.source_id!r}, got {source!r}")
        if not _contains_plugin_chain(profile, class_name, spec.chain):
            raise RuntimeError(f"missing {spec.name} Python chain: {' -> '.join(spec.chain)}")
        source_matches = [
            key
            for key, display_name in profile.sources.items()
            if _canonical_plugin_key(key) == _canonical_plugin_key(spec.source_id)
            or _canonical_plugin_key(display_name) == _canonical_plugin_key(spec.source_id)
        ]
        if len(source_matches) != 1:
            raise RuntimeError(f"{spec.name} source identity is ambiguous: matches={source_matches} all={profile.sources}")
        identities[spec.name] = {
            "source_id": spec.source_id,
            "observed_class_source": source,
            "module": spec.module,
            "class_name": class_name,
            "source_matches": source_matches,
            "node_count": len(plugin_nodes),
            "line_numbers": sorted({node.line_number for node in plugin_nodes}),
            "chain": list(spec.chain),
        }

    source_values = {_canonical_plugin_key(value) for value in profile.class_sources.values()}
    expected_sources = {_canonical_plugin_key(spec.source_id) for spec in PLUGIN_SPECS}
    if not expected_sources.issubset(source_values):
        raise RuntimeError(f"class source mappings lost one of the plugin identities: {profile.class_sources}")
    observed_identity_sources = {
        _canonical_plugin_key(str(identities[spec.name]["observed_class_source"])) for spec in PLUGIN_SPECS
    }
    if len(observed_identity_sources) != len(PLUGIN_SPECS):
        raise RuntimeError("plugin source identities are not distinct")

    validation = {
        "profile_shape": {
            "duration_seconds": profile.duration_seconds,
            "thread_count": len(profile.threads),
            "root_weight": root_weight,
            "python_node_count": len(all_python_nodes),
        },
        "identities": identities,
        "sources": profile.sources,
        "class_sources": profile.class_sources,
        "diagnostics": {
            "backend": backend,
            "enabled": enabled,
            "py_start_events": py_start_events,
            "shadow_snapshot_attempts": shadow_snapshot_attempts,
            "attributed_samples": attribution_samples,
            "zero_failure_counts": failure_counts,
            "nonnegative_counts": nonnegative_counts,
        },
        "assertions": {
            "nonempty_profile": bool(profile.threads and root_weight > 0),
            "both_identities": len(identities) == len(PLUGIN_SPECS),
            "both_source_chains": True,
            "distinct_sources": len(observed_identity_sources) == len(PLUGIN_SPECS),
            "observer_frames_filtered": not observer_nodes,
            "callback_shadow_failures_zero": all(value == 0 for value in failure_counts.values()),
        },
    }
    if not all(validation["assertions"].values()):
        raise RuntimeError(f"multi-plugin assertions are not all true: {validation['assertions']}")
    return validation


class PythonMultiPluginValidation(PythonAttributionValidation):
    """One Linux profile with two separately named active Python plugin wheels."""

    disable_bstats = True

    def __init__(
        self,
        spark_sha: str,
        *,
        bds_version: str | None = None,
        profile_seconds: int = 60,
    ) -> None:
        super().__init__("linux", None, 0, "idle", "multi-plugin", profile_seconds)
        self.spark_sha = _exact_sha(spark_sha, "spark_sha")
        self.expected_bds_version, self.expected_bds_protocol = _required_bds_version(bds_version)
        self.multi_raw_profile_path = self.root / "python-multi-plugin.sparkprofile"
        self.multi_summary_path = self.root / "python-multi-plugin-profile-summary.json"
        self.result.update(
            {
                "test_kind": "spark-python-multi-plugin-attribution",
                "spark_sha": self.spark_sha,
                "expected_bds_version": self.expected_bds_version,
                "expected_bds_protocol_version": self.expected_bds_protocol,
                "spark_run_id": None,
                "spark_artifact_id": None,
                "spark_artifact_name": None,
                "endstone_sha": None,
                "endstone_run_id": None,
                "endstone_artifact_id": None,
                "endstone_artifact_name": None,
                "artifact_metadata": None,
                "versions": {"bds": None, "endstone": None, "spark": None},
                "plugin_fixtures": [
                    {
                        "name": spec.name,
                        "source": str(spec.source_path.relative_to(FIXTURE_ROOT.parent)),
                        "source_id": spec.source_id,
                        "module": spec.module,
                        "class_name": spec.class_name,
                        "chain": list(spec.chain),
                    }
                    for spec in PLUGIN_SPECS
                ],
                "multi_plugin_validation": None,
            }
        )
        self._write_results()

    def _write_results(self) -> None:
        super()._write_results()

    def install_artifacts(self) -> None:
        previous = os.environ.get("EXPECTED_SPARK_SHA")
        os.environ["EXPECTED_SPARK_SHA"] = self.spark_sha
        try:
            IntegrationTest.install_artifacts(self)
        finally:
            if previous is None:
                os.environ.pop("EXPECTED_SPARK_SHA", None)
            else:
                os.environ["EXPECTED_SPARK_SHA"] = previous

        spark = validate_component_provenance(self.metadata, "spark")
        endstone = validate_component_provenance(self.metadata, "endstone")
        for component_name, component in (("spark", spark), ("endstone", endstone)):
            observed_sha = str(component.get("sha") or "").strip().lower()
            artifact = component.get("artifact")
            if not SHA1_RE.fullmatch(observed_sha) or not component.get("run_id"):
                raise RuntimeError(f"{component_name} artifact provenance is incomplete: {component}")
            if not isinstance(artifact, dict) or not artifact.get("id") or not artifact.get("name"):
                raise RuntimeError(f"{component_name} artifact identity is incomplete: {component}")
        observed_spark = str(spark.get("sha") or "").strip().lower()
        if observed_spark != self.spark_sha:
            raise RuntimeError(f"Spark artifact SHA mismatch: observed={observed_spark!r} expected={self.spark_sha!r}")
        expected_full, expected_protocol = _required_bds_version(self.expected_bds_version)
        bds_target = _installed_endstone_bds_target(expected_protocol)
        runtime_version = validate_endstone_runtime_version()
        if runtime_version is None:
            try:
                runtime_version = importlib.metadata.version("endstone")
            except importlib.metadata.PackageNotFoundError as exc:
                raise RuntimeError("installed Endstone package has no discoverable version") from exc
        self.result["provenance"] = {
            "spark": {
                "repository": spark.get("repository"),
                "sha": spark.get("sha"),
                "run_id": spark.get("run_id"),
                "artifact_id": (spark.get("artifact") or {}).get("id"),
                "artifact_name": (spark.get("artifact") or {}).get("name"),
            },
            "endstone": {
                "repository": endstone.get("repository"),
                "sha": endstone.get("sha"),
                "run_id": endstone.get("run_id"),
                "artifact_id": (endstone.get("artifact") or {}).get("id"),
                "artifact_name": (endstone.get("artifact") or {}).get("name"),
                "installed_version": runtime_version,
            },
            "bds_version": None,
            "bds_requested_full_version": expected_full,
            "bds_requested_protocol_version": expected_protocol,
            "bds_endstone_target": bds_target,
            "spark_version": None,
            "artifact_metadata": self.metadata,
        }
        self.result["versions"]["endstone"] = runtime_version
        self.result["artifact_metadata"] = self.metadata
        self.result["spark_run_id"] = spark.get("run_id")
        self.result["spark_artifact_id"] = (spark.get("artifact") or {}).get("id")
        self.result["spark_artifact_name"] = (spark.get("artifact") or {}).get("name")
        self.result["endstone_sha"] = endstone.get("sha")
        self.result["endstone_run_id"] = endstone.get("run_id")
        self.result["endstone_artifact_id"] = (endstone.get("artifact") or {}).get("id")
        self.result["endstone_artifact_name"] = (endstone.get("artifact") or {}).get("name")
        self.check(
            "exact-bds-acquisition-target",
            "PASS",
            "Endstone's installed Minecraft target controls the BDS download selection",
            requested_full=expected_full,
            requested_protocol=expected_protocol,
            selected_protocol=bds_target,
        )
        self.check(
            "exact-artifact-provenance",
            "PASS",
            spark_sha=observed_spark,
            spark_run_id=spark.get("run_id"),
            endstone_sha=endstone.get("sha"),
            endstone_run_id=endstone.get("run_id"),
            endstone_artifact_id=(endstone.get("artifact") or {}).get("id"),
            endstone_runtime_version=runtime_version,
        )

        plugin_dir = self.server_dir / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        os.environ["SPARK_MULTI_PLUGIN_ITERATIONS"] = "12000"
        for spec in PLUGIN_SPECS:
            wheel_dir = self.root / f"{spec.name}-wheel"
            if wheel_dir.exists() or wheel_dir.is_symlink():
                raise RuntimeError(f"multi-plugin wheel directory is not fresh: {wheel_dir}")
            wheel_dir.mkdir()
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
                    str(spec.source_path),
                ],
                timeout=180,
            )
            wheels = sorted(wheel_dir.glob(f"{spec.wheel_prefix}-*.whl"))
            if len(wheels) != 1:
                raise RuntimeError(f"Expected one {spec.name} wheel, got: {wheels}")
            target = plugin_dir / wheels[0].name
            if target.exists() or target.is_symlink():
                raise RuntimeError(f"{spec.name} plugin target is not fresh: {target}")
            shutil.copy2(wheels[0], target)
            self.result["plugin_fixtures"][next(i for i, item in enumerate(self.result["plugin_fixtures"]) if item["name"] == spec.name)][
                "installed_path"
            ] = str(target.relative_to(self.root))
            self.check("python-plugin-installed", "PASS", str(target.relative_to(self.root)), plugin=spec.name)
        self._write_results()

    def start_server(self) -> None:
        super().start_server()
        assert self.server is not None
        version_file = self.server_dir / "version.txt"
        if version_file.is_symlink() or not version_file.is_file():
            raise RuntimeError(f"BDS runtime version.txt is missing or symlinked: {version_file}")
        observed_protocol = version_file.read_text(encoding="utf-8").strip()
        if observed_protocol != self.expected_bds_protocol:
            raise RuntimeError(
                "BDS runtime version.txt mismatch: "
                f"observed={observed_protocol!r} expected={self.expected_bds_protocol!r}"
            )
        self.result["bds_version"] = observed_protocol
        version_lines = self.server.wait_for(
            lambda lines: any("Version:" in line for line in lines),
            30,
            "exact BDS full version",
        )
        observed = _validate_exact_bds_evidence(
            self.result,
            version_lines,
            self.expected_bds_version,
            self.expected_bds_protocol,
        )
        self.result["provenance"]["bds_version"] = self.result.get("bds_version")
        self.result["provenance"]["bds_protocol_version"] = observed
        self.result["provenance"]["spark_version"] = self._spark_version(self.server.snapshot())
        self.result["versions"]["bds"] = self.result.get("bds_version")
        self.result["versions"]["spark"] = self.result["provenance"]["spark_version"]
        self._write_results()
        self.check(
            "exact-bds-version",
            "PASS",
            observed_protocol=observed,
            expected_protocol=os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION", "").strip() or None,
            expected_full=os.environ.get("EXPECTED_BDS_VERSION", "").strip() or None,
        )

    @staticmethod
    def _spark_version(lines: list[str]) -> str | None:
        for line in reversed(lines):
            lowered = line.lower()
            marker = "spark v"
            if marker in lowered:
                return line[lowered.index(marker) + len(marker) :].strip().split()[0].strip(")[],:;")
        return None

    def wait_plugin(self) -> None:
        assert self.server is not None
        for spec in PLUGIN_SPECS:
            self.server.wait_for(
                lambda lines, marker=spec.log_marker: any(marker in line.lower() for line in lines),
                30,
                f"Python {spec.name} plugin enable",
            )
            self.check("python-plugin-enabled", "PASS", plugin=spec.name)

    def validate_profile(self, url: str) -> dict[str, object]:
        self.check("viewer-url-emitted", "PASS", viewer_url=url)
        raw = fetch_viewer_payload(url)
        if len(raw) < 64:
            raise RuntimeError(f"raw Spark payload is unexpectedly small: {len(raw)} bytes")
        self.multi_raw_profile_path.write_bytes(raw)
        self.check("raw-payload-open", "PASS", bytes=len(raw), viewer_url=url)
        profile = parse_sampler_data(raw)
        summary = profile_summary(profile)
        self.multi_summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

        validation = validate_multi_plugin_profile(profile, self.profile_seconds)
        self.result["multi_plugin_validation"] = validation
        self.result["profile_summary"] = summary
        self.result["spark_profile_viewer_url"] = url
        self._write_results()
        self.check("multi-plugin-profile-validation", "PASS", **validation["assertions"])
        return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spark-sha", default=os.environ.get("EXPECTED_SPARK_SHA", ""))
    parser.add_argument("--bds-version", default=os.environ.get("EXPECTED_BDS_VERSION", ""))
    parser.add_argument("--profile-seconds", type=int, default=60)
    args = parser.parse_args()
    validator = PythonMultiPluginValidation(
        args.spark_sha,
        bds_version=args.bds_version,
        profile_seconds=args.profile_seconds,
    )
    code = validator.execute()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
