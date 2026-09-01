#!/usr/bin/env python3
"""Run a paired BDS+Endstone control and Spark monitoring comparison."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import pathlib
import re
import shutil
import statistics
import sys
import time
import traceback
from typing import Any

import psutil

from controller.bot_validation import patch_server_properties
from controller.python_evidence_provenance import (
    validate_bds_version,
    validate_component_provenance,
    validate_endstone_runtime_version,
)
from controller.run_test import (
    READY_HINTS,
    SPARK_LOAD_HINTS,
    IntegrationTest,
    ServerProcess,
    clean_line,
    locate_one,
    now_iso,
    run_checked,
    write_json,
)
from providers.artifact_provider import resolve_artifacts

DEFAULT_MEASUREMENT_SECONDS = 15
DEFAULT_WARMUP_SECONDS = 10
MIN_MEASUREMENT_SECONDS = 5
MAX_MEASUREMENT_SECONDS = 300
MAX_WARMUP_SECONDS = 300
WORKLOAD_ITERATIONS = 9000
WORKLOAD_METRICS_NAME = "comparable-workload-metrics.json"
SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
BDS_FULL_VERSION_RE = re.compile(r"^1\.[0-9]+\.[0-9]+\.[0-9]+$")
SPARK_VERSION_RE = re.compile(r"(?:endstone-spark|spark)\s+v?([0-9][0-9A-Za-z.+_-]*)", re.IGNORECASE)


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


_INACTIVE_PROFILER_RE = re.compile(
    r"(?:^|[\]:])\s*(?:the profiler isn't running!|there isn't an active profiler running\.)\s*$",
    re.IGNORECASE,
)
_AMBIGUOUS_PROFILER_RE = re.compile(
    r"(?:^|[\]:])\s*the profiler has stopped; results are still being finalized\.\s*$",
    re.IGNORECASE,
)
_ACTIVE_PROFILER_RE = re.compile(
    r"(?:^|[\]:])\s*(?:retained allocation |allocation )?profiler is (?:already )?running!?\s*$",
    re.IGNORECASE,
)
_INFO_HELP_RE = re.compile(r"to start a new one, run:.*\bprofiler start\s*$", re.IGNORECASE)
_PROFILER_TRANSITION_RE = re.compile(
    r"(?:"
    r"\b(?:retained allocation |allocation )?profiler is (?:already |now )?running\b|"
    r"\bprofiler has (?:started|stopped)\b|"
    r"\b(?:starting|stopping) (?:the )?profiler\b|"
    r"\bprofiler (?:was )?(?:cancelled|canceled)\b|"
    r"\bprofiler session\b|"
    r"^>\s*spark profiler (?:start|stop|cancel)\b"
    r")",
    re.IGNORECASE,
)


def parse_profiler_inactivity(lines: list[str]) -> dict[str, Any]:
    """Accept only an exact inactive status and reject active or ambiguous output."""

    normalized = [clean_line(str(line)) for line in lines]
    inactive = [line for line in normalized if _INACTIVE_PROFILER_RE.search(line)]
    ambiguous: list[str] = []
    for line in normalized:
        if not line or _INACTIVE_PROFILER_RE.search(line) or _INFO_HELP_RE.search(line):
            continue
        if _AMBIGUOUS_PROFILER_RE.search(line) or _ACTIVE_PROFILER_RE.search(line):
            ambiguous.append(line)
            continue
        lowered = line.casefold()
        if "profiler" in lowered and re.search(
            r"\b(?:active|running|started|stopped|finaliz(?:e|ing|ed)|cancel(?:led|ed)?|session)\b",
            lowered,
        ):
            ambiguous.append(line)
    if ambiguous:
        raise RuntimeError(f"Spark profiler inactivity evidence is active or ambiguous: {ambiguous[-10:]}")
    if len(inactive) != 1:
        raise RuntimeError(
            "Spark profiler inactivity was not proven by exactly one inactive status line: "
            f"matches={inactive[-10:]} output={normalized[-30:]}"
        )
    return {"state": "inactive", "evidence": inactive[0]}


def read_activity_snapshot(path: pathlib.Path) -> dict[str, Any]:
    """Read and validate Spark's durable activity log for interval comparison."""

    path = pathlib.Path(path)
    if path.is_symlink():
        raise RuntimeError(f"Spark activity log is symlinked: {path}")
    if not path.is_file():
        return {"present": False, "sha256": None, "bytes": 0, "entries": []}
    raw = path.read_bytes()
    if len(raw) > 8 * 1024 * 1024:
        raise RuntimeError(f"Spark activity log exceeds its bounded size: {path}")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Spark activity log is invalid JSON: {path}") from exc
    if not isinstance(decoded, list):
        raise TypeError(f"Spark activity log is not an array: {path}")
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(decoded):
        if not isinstance(entry, dict):
            raise TypeError(f"Spark activity entry {index} is not an object")
        timestamp = entry.get("time")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise RuntimeError(f"Spark activity entry {index} has an invalid monotonic timestamp")
        if not isinstance(entry.get("type"), str) or not isinstance(entry.get("data"), dict):
            raise TypeError(f"Spark activity entry {index} has an invalid type/data shape")
        data = entry["data"]
        if not isinstance(data.get("type"), str) or not isinstance(data.get("value"), str):
            raise TypeError(f"Spark activity entry {index} has invalid data fields")
        entries.append(entry)
    return {
        "present": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "entries": entries,
    }


def validate_profiler_window(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    start_ns: int,
    end_ns: int,
    start_unix_ms: int | None = None,
    end_unix_ms: int | None = None,
    log_lines: list[str] | None = None,
) -> dict[str, Any]:
    """Prove that no profiler transition occurred anywhere in a measured interval."""

    if end_ns <= start_ns:
        raise RuntimeError(f"profiler measurement interval is not monotonic: {start_ns} -> {end_ns}")
    log_lines = log_lines or []
    transitions = [clean_line(str(line)) for line in log_lines if _PROFILER_TRANSITION_RE.search(clean_line(str(line)))]
    if transitions:
        raise RuntimeError(f"profiler transition evidence occurred during measurement: {transitions[-10:]}")
    before_hash = before.get("sha256")
    after_hash = after.get("sha256")
    if before.get("present") != after.get("present") or before_hash != after_hash:
        raise RuntimeError(
            "Spark activity log changed during profiler measurement: "
            f"before={before_hash!r} after={after_hash!r}"
        )
    interval_entries: list[dict[str, Any]] = []
    if start_unix_ms is not None and end_unix_ms is not None:
        if end_unix_ms < start_unix_ms:
            raise RuntimeError(f"activity interval is not monotonic: {start_unix_ms} -> {end_unix_ms}")
        interval_entries = [
            entry
            for entry in after.get("entries", [])
            if start_unix_ms <= int(entry["time"]) <= end_unix_ms
        ]
        if interval_entries:
            raise RuntimeError(f"Spark activity entries fall inside measurement: {interval_entries[-10:]}")
    return {
        "durable": True,
        "start_monotonic_ns": start_ns,
        "end_monotonic_ns": end_ns,
        "start_monotonic_unix_ms": start_unix_ms,
        "end_monotonic_unix_ms": end_unix_ms,
        "log_lines_checked": len(log_lines),
        "transition_lines": transitions,
        "activity_before": {
            "present": before.get("present"),
            "sha256": before_hash,
            "bytes": before.get("bytes", 0),
        },
        "activity_after": {
            "present": after.get("present"),
            "sha256": after_hash,
            "bytes": after.get("bytes", 0),
        },
        "activity_entries_in_window": interval_entries,
    }


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _exact_sha(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not SHA1_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a full 40-character commit SHA")
    return normalized


class FinalControlMonitoringCase(IntegrationTest):
    """One fresh Linux BDS case with an identical workload contract."""

    disable_bstats = True

    def __init__(
        self,
        case_name: str,
        spark_enabled: bool,
        spark_sha: str,
        *,
        bds_version: str | None = None,
        measurement_seconds: int = DEFAULT_MEASUREMENT_SECONDS,
        warmup_seconds: int = DEFAULT_WARMUP_SECONDS,
    ) -> None:
        if case_name not in {"control", "monitoring"}:
            raise ValueError(f"unknown comparison case: {case_name}")
        if spark_enabled != (case_name == "monitoring"):
            raise ValueError(f"Spark deployment does not match comparison case: {case_name}")
        self.case_name = case_name
        self.spark_enabled = spark_enabled
        self.spark_sha = _exact_sha(spark_sha, "spark_sha")
        self.expected_bds_version, self.expected_bds_protocol = _required_bds_version(bds_version)
        self.measurement_seconds = self._bounded_duration(
            measurement_seconds, MIN_MEASUREMENT_SECONDS, MAX_MEASUREMENT_SECONDS, "measurement_seconds"
        )
        self.warmup_seconds = self._bounded_duration(warmup_seconds, 0, MAX_WARMUP_SECONDS, "warmup_seconds")
        super().__init__("linux")
        self.workload_metrics_path = self.root / WORKLOAD_METRICS_NAME
        self.workload_wheel: pathlib.Path | None = None
        self.measure_start_ns: int | None = None
        self.measure_end_ns: int | None = None
        self.measure_start_unix_ms: int | None = None
        self.measure_end_unix_ms: int | None = None
        self.profiler_activity_path = self.server_dir / "plugins" / "spark" / "activity.json"
        self.profiler_activity_before: dict[str, Any] | None = None
        self.profiler_window_log_start: int | None = None
        self.profiler_clock_offset_ns: int | None = None
        self.result.update(
            {
                "test_kind": "spark-control-monitoring-comparison",
                "comparison_case": case_name,
                "spark_enabled": spark_enabled,
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
                "measurement_contract": {
                    "platform": "linux",
                    "warmup_seconds": self.warmup_seconds,
                    "measurement_seconds": self.measurement_seconds,
                    "workload_iterations": WORKLOAD_ITERATIONS,
                    "workload_fixture": "endstone-final-comparable-workload",
                    "metrics_clock": "monotonic_ns",
                    "measurement_metric_fields": [
                        "cpu_percent_of_one_core",
                        "cpu_ms_per_tick",
                        "observed_tps",
                        "rss_p95_bytes",
                        "rss_peak_bytes",
                        "context_switches_delta",
                    ],
                    "workload_metric_fields": [
                        "mspt_mean",
                        "mspt_p95",
                        "mspt_p99",
                        "tps_mean",
                        "tps_min",
                        "tps_p95",
                    ],
                },
                "provenance": {
                    "spark_sha": self.spark_sha,
                    "spark_deployed": spark_enabled,
                    "spark_artifact": None,
                    "endstone": None,
                    "bds_version": None,
                    "bds_requested_full_version": self.expected_bds_version,
                    "bds_requested_protocol_version": self.expected_bds_protocol,
                    "bds_endstone_target": None,
                    "spark_version": None,
                },
                "versions": {"bds": None, "endstone": None, "spark": None},
                "deployment": {
                    "spark_plugin": None,
                    "spark_absence_proof": None
                    if spark_enabled
                    else {
                        "plugin_directory": "work/linux/bedrock_server/plugins",
                        "spark_binary_name": "endstone_spark.so",
                        "spark_binary_present": False,
                        "spark_enabled": False,
                    },
                    "workload_plugin": None,
                },
                "measurement": {
                    "warmup_started_at": None,
                    "warmup_completed_at": None,
                    "started_at": None,
                    "completed_at": None,
                    "start_monotonic_ns": None,
                    "end_monotonic_ns": None,
                    "start_monotonic_unix_ms": None,
                    "end_monotonic_unix_ms": None,
                    "profiler_window": None,
                    "metrics": None,
                    "workload_metrics": None,
                },
            }
        )
        self._write_results()

    @staticmethod
    def _bounded_duration(value: int, minimum: int, maximum: int, label: str) -> int:
        try:
            integer = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be an integer") from exc
        if integer < minimum or integer > maximum:
            raise ValueError(f"{label} must be between {minimum} and {maximum}")
        return integer

    def _write_results(self) -> None:
        write_json(self.result_path, self.result)

    def check(self, name: str, status: str, detail: str | None = None, **extra: Any) -> None:
        super().check(name, status, detail, **extra)
        self._write_results()

    def install_artifacts(self) -> None:
        previous = os.environ.get("EXPECTED_SPARK_SHA")
        os.environ["EXPECTED_SPARK_SHA"] = self.spark_sha
        try:
            self.metadata = resolve_artifacts("linux", self.downloads, self.metadata_path)
        finally:
            if previous is None:
                os.environ.pop("EXPECTED_SPARK_SHA", None)
            else:
                os.environ["EXPECTED_SPARK_SHA"] = previous
        self.check("artifact-discovery", "PASS")

        components = self.metadata.get("components")
        if not isinstance(components, dict):
            raise TypeError("artifact metadata has no components")
        spark = validate_component_provenance(self.metadata, "spark")
        endstone = validate_component_provenance(self.metadata, "endstone")
        for component_name, component in (("spark", spark), ("endstone", endstone)):
            observed_sha = str(component.get("sha") or "").strip().lower()
            artifact = component.get("artifact")
            if not SHA1_RE.fullmatch(observed_sha) or not component.get("run_id"):
                raise RuntimeError(f"{component_name} artifact provenance is incomplete: {component}")
            if not isinstance(artifact, dict) or not artifact.get("id") or not artifact.get("name"):
                raise RuntimeError(f"{component_name} artifact identity is incomplete: {component}")
        observed_spark_sha = str(spark.get("sha") or "").strip().lower()
        if observed_spark_sha != self.spark_sha:
            raise RuntimeError(f"Spark artifact SHA mismatch: observed={observed_spark_sha!r} expected={self.spark_sha!r}")
        spark_provenance = {
            "repository": spark.get("repository"),
            "sha": spark.get("sha"),
            "run_id": spark.get("run_id"),
            "artifact_id": (spark.get("artifact") or {}).get("id"),
            "artifact_name": (spark.get("artifact") or {}).get("name"),
            "deployed": self.spark_enabled,
        }
        self.result["provenance"]["endstone"] = {
            "repository": endstone.get("repository"),
            "sha": endstone.get("sha"),
            "run_id": endstone.get("run_id"),
            "artifact_id": (endstone.get("artifact") or {}).get("id"),
            "artifact_name": (endstone.get("artifact") or {}).get("name"),
            "installed_version": None,
        }
        self.result["provenance"]["spark"] = spark_provenance
        self.result["provenance"]["spark_artifact"] = spark
        self.result["versions"]["endstone"] = None
        self.result["spark_run_id"] = spark.get("run_id")
        self.result["spark_artifact_id"] = (spark.get("artifact") or {}).get("id")
        self.result["spark_artifact_name"] = (spark.get("artifact") or {}).get("name")
        self.result["endstone_sha"] = endstone.get("sha")
        self.result["endstone_run_id"] = endstone.get("run_id")
        self.result["endstone_artifact_id"] = (endstone.get("artifact") or {}).get("id")
        self.result["endstone_artifact_name"] = (endstone.get("artifact") or {}).get("name")
        self.result["artifact_metadata"] = self.metadata
        self.check(
            "exact-artifact-provenance",
            "PASS",
            spark_sha=observed_spark_sha,
            spark_run_id=spark.get("run_id"),
            endstone_sha=endstone.get("sha"),
            endstone_run_id=endstone.get("run_id"),
            endstone_artifact_id=(endstone.get("artifact") or {}).get("id"),
            endstone_runtime_version=None,
        )

        endstone_root = self.downloads / "endstone" / "payload"
        wheel = locate_one(endstone_root, ["endstone-*-cp313-cp313-*.whl", "endstone-*.whl"])
        run_checked(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--force-reinstall",
                str(wheel),
            ],
            timeout=300,
        )
        expected_full, expected_protocol = _required_bds_version(self.expected_bds_version)
        bds_target = _installed_endstone_bds_target(expected_protocol)
        runtime_version = validate_endstone_runtime_version()
        if runtime_version is None:
            try:
                runtime_version = importlib.metadata.version("endstone")
            except importlib.metadata.PackageNotFoundError as exc:
                raise RuntimeError("installed Endstone package has no discoverable version") from exc
        self.result["provenance"].update(
            {
                "bds_requested_full_version": expected_full,
                "bds_requested_protocol_version": expected_protocol,
                "bds_endstone_target": bds_target,
            }
        )
        self.result["provenance"]["endstone"]["installed_version"] = runtime_version
        self.result["versions"]["endstone"] = runtime_version
        self.check(
            "exact-bds-acquisition-target",
            "PASS",
            "Endstone's installed Minecraft target controls the BDS download selection",
            requested_full=expected_full,
            requested_protocol=expected_protocol,
            selected_protocol=bds_target,
        )
        self.check("endstone-wheel-located", "PASS", str(wheel.relative_to(self.root)))

        self.server_dir.mkdir(parents=True, exist_ok=True)
        plugin_dir = self.server_dir / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        self._prepare_bstats_before_start()

        spark_root = self.downloads / "spark" / "payload"
        spark_binary = locate_one(spark_root, ["endstone_spark.so"])
        spark_target = plugin_dir / spark_binary.name
        if self.spark_enabled:
            if spark_target.exists() or spark_target.is_symlink():
                raise RuntimeError(f"Spark plugin target is not fresh: {spark_target}")
            shutil.copy2(spark_binary, spark_target)
            self.result["deployment"]["spark_plugin"] = str(spark_target.relative_to(self.root))
            self.check("spark-plugin-deployed", "PASS", str(spark_target.relative_to(self.root)))
        else:
            if spark_target.exists() or spark_target.is_symlink():
                raise RuntimeError(f"control case contains a Spark plugin: {spark_target}")
            self.result["deployment"]["spark_absence_proof"] = {
                "plugin_directory": str(plugin_dir.relative_to(self.root)),
                "spark_binary_name": spark_binary.name,
                "spark_binary_present": False,
                "spark_enabled": False,
            }
            self.check("spark-plugin-absent", "PASS", "control case omitted the Spark native plugin")

        fixture_source = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "endstone-final-comparable-workload"
        wheel_dir = self.root / "comparable-workload-wheel"
        if wheel_dir.exists() or wheel_dir.is_symlink():
            raise RuntimeError(f"comparable workload wheel directory is not fresh: {wheel_dir}")
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
                str(fixture_source),
            ],
            timeout=180,
        )
        wheels = sorted(wheel_dir.glob("endstone_final_comparable_workload-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected one comparable workload wheel, got: {wheels}")
        workload_target = plugin_dir / wheels[0].name
        if workload_target.exists() or workload_target.is_symlink():
            raise RuntimeError(f"comparable workload target is not fresh: {workload_target}")
        shutil.copy2(wheels[0], workload_target)
        self.workload_wheel = workload_target
        os.environ["ENDSTONE_COMPARABLE_WORKLOAD_ITERATIONS"] = str(WORKLOAD_ITERATIONS)
        os.environ["ENDSTONE_COMPARABLE_WORKLOAD_METRICS"] = str(self.workload_metrics_path)
        self.result["deployment"]["workload_plugin"] = str(workload_target.relative_to(self.root))
        self.check("comparable-workload-plugin-installed", "PASS", str(workload_target.relative_to(self.root)))
        self._write_results()

    def start_server(self) -> None:
        cmd = [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(self.server_dir)]
        self.server = ServerProcess(cmd, self.root, self.log_path)
        self._prepare_bstats_before_start()
        self.server.start()
        self.server.wait_for(
            lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines),
            240,
            "BDS ready",
        )
        self.check("bds-start", "PASS")
        self.check("ready", "PASS")
        if self.spark_enabled:
            self.server.wait_for(
                lambda lines: any(
                    "spark" in line.lower() and any(hint in line.lower() for hint in SPARK_LOAD_HINTS)
                    for line in lines
                ),
                30,
                "Spark enable",
            )
            self.check("spark-load-enable", "PASS")
        else:
            lines = self.server.snapshot()
            loaded = [line for line in lines if any(hint in line.lower() for hint in SPARK_LOAD_HINTS)]
            if loaded:
                raise RuntimeError(f"control case observed Spark load evidence: {loaded[-10:]}")
            proof = self.result["deployment"]["spark_absence_proof"]
            proof["startup_log_spark_load_lines"] = loaded
            proof["plugin_directory_entries"] = sorted(
                str(path.relative_to(self.server_dir / "plugins"))
                for path in (self.server_dir / "plugins").iterdir()
            )
            self.check("spark-load-absent", "PASS", "no Spark load evidence in control startup log")
        self.server.wait_for(
            lambda lines: any("endstone comparable workload enabled" in line.lower() for line in lines),
            30,
            "comparable workload enable",
        )
        self.check("comparable-workload-plugin-enabled", "PASS")
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
        self.result["provenance"]["bds_version"] = observed_protocol
        self.result["versions"]["bds"] = observed_protocol
        self._write_results()
        spark_version = self._spark_version(self.server.snapshot()) if self.spark_enabled else None
        self.result["provenance"]["spark_version"] = spark_version
        self.result["versions"]["spark"] = spark_version
        if self.spark_enabled and not spark_version:
            raise RuntimeError("Spark did not expose an exact runtime version in the BDS log")
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
        self.result["provenance"]["bds_protocol_version"] = observed
        self.check(
            "exact-bds-version",
            "PASS",
            observed_protocol=observed,
            expected_protocol=os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION", "").strip() or None,
            expected_full=os.environ.get("EXPECTED_BDS_VERSION", "").strip() or None,
        )

    def run_sanity(self) -> None:
        if self.spark_enabled:
            self.run_basic_commands()
            return
        self.command_check("control-list", "list")
        self.command_check("control-help", "help")

    def bootstrap_server(self) -> None:
        self.start_server()
        self.run_sanity()
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            self.record_server_lifecycle()
            raise RuntimeError("BDS did not stop gracefully during comparison bootstrap")
        self.record_server_lifecycle()
        self.server.close()
        self.server = None
        patch_server_properties(self.server_dir / "server.properties")
        self.result["bds_lifecycle"] = {"bootstrap_restart": True}
        self.check("offline-server-properties", "PASS", "offline mode and bounded workload settings installed")
        self.start_server()
        self.run_sanity()

    def profiler_inactive(self, label: str) -> None:
        if not self.spark_enabled:
            return
        output = self.command_check(f"profiler-inactive-{label}", "spark profiler info")
        parsed = parse_profiler_inactivity(output)
        self.result.setdefault("profiler_state", {})[label] = {
            "output": output[-30:],
            "parsed": parsed,
        }
        self._write_results()

    @staticmethod
    def _spark_version(lines: list[str]) -> str | None:
        for line in reversed(lines):
            match = SPARK_VERSION_RE.search(line)
            if match:
                return match.group(1)
        return None

    def _processes(self) -> list[psutil.Process]:
        assert self.server is not None and self.server.process is not None
        try:
            root = psutil.Process(self.server.process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            raise RuntimeError("managed Endstone process is unavailable") from exc
        try:
            return [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return [root]

    def _cpu_seconds(self) -> float:
        total = 0.0
        for process in self._processes():
            try:
                times = process.cpu_times()
                total += float(times.user) + float(times.system)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total

    def _rss_bytes(self) -> int:
        total = 0
        for process in self._processes():
            try:
                total += int(process.memory_info().rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total

    def _context_switches(self) -> dict[str, int]:
        voluntary = 0
        involuntary = 0
        for process in self._processes():
            try:
                switches = process.num_ctx_switches()
                voluntary += int(switches.voluntary)
                involuntary += int(switches.involuntary)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return {"voluntary": voluntary, "involuntary": involuntary}

    def _gametime(self) -> int:
        assert self.server is not None
        start = self.server.command("time query gametime")
        output = self.server.wait_command_output(start, 5)
        for line in reversed(output):
            numbers = re.findall(r"-?\d+", line)
            if numbers:
                return int(numbers[-1])
        raise RuntimeError(f"Unable to parse gametime: {output[-20:]}")

    def measure(self) -> None:
        assert self.server is not None
        self.profiler_inactive("before")
        if self.warmup_seconds:
            self.result["measurement"]["warmup_started_at"] = now_iso()
            self._write_results()
            deadline = time.monotonic() + self.warmup_seconds
            while time.monotonic() < deadline:
                if not self.server.is_alive():
                    raise RuntimeError("BDS exited during comparison warmup")
                time.sleep(min(1.0, deadline - time.monotonic()))
            self.result["measurement"]["warmup_completed_at"] = now_iso()
            self._write_results()
        gametime_start = self._gametime()
        cpu_start = self._cpu_seconds()
        rss_start = self._rss_bytes()
        context_start = self._context_switches()
        if self.spark_enabled:
            self.profiler_window_log_start = len(self.server.snapshot())
            self.profiler_activity_before = read_activity_snapshot(self.profiler_activity_path)
        wall_start = time.monotonic()
        self.measure_start_ns = time.monotonic_ns()
        if self.spark_enabled:
            self.profiler_clock_offset_ns = time.time_ns() - self.measure_start_ns
            self.measure_start_unix_ms = (self.measure_start_ns + self.profiler_clock_offset_ns) // 1_000_000
        self.result["measurement"].update(
            {
                "started_at": now_iso(),
                "start_monotonic_ns": self.measure_start_ns,
                "start_monotonic_unix_ms": self.measure_start_unix_ms,
            }
        )
        self._write_results()
        rss_samples = [rss_start]
        deadline = wall_start + self.measurement_seconds
        while time.monotonic() < deadline:
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during comparison measurement")
            rss_samples.append(self._rss_bytes())
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        self.measure_end_ns = time.monotonic_ns()
        if self.spark_enabled:
            assert self.profiler_activity_before is not None
            assert self.profiler_window_log_start is not None
            assert self.profiler_clock_offset_ns is not None
            self.measure_end_unix_ms = (self.measure_end_ns + self.profiler_clock_offset_ns) // 1_000_000
            activity_after = read_activity_snapshot(self.profiler_activity_path)
            log_snapshot = self.server.snapshot()
            window_evidence = validate_profiler_window(
                self.profiler_activity_before,
                activity_after,
                start_ns=self.measure_start_ns,
                end_ns=self.measure_end_ns,
                start_unix_ms=self.measure_start_unix_ms,
                end_unix_ms=self.measure_end_unix_ms,
                log_lines=log_snapshot[self.profiler_window_log_start :],
            )
            self.result["measurement"]["profiler_window"] = window_evidence
            self.check(
                "profiler-window-no-transitions",
                "PASS",
                "durable activity and server-log evidence covered the monotonic measurement interval",
                evidence=window_evidence,
            )
        self.result["measurement"].update(
            {
                "end_monotonic_ns": self.measure_end_ns,
                "end_monotonic_unix_ms": self.measure_end_unix_ms,
                "completed_at": now_iso(),
            }
        )
        wall_seconds = (self.measure_end_ns - self.measure_start_ns) / 1_000_000_000
        gametime_end = self._gametime()
        cpu_end = self._cpu_seconds()
        rss_end = self._rss_bytes()
        context_end = self._context_switches()
        ticks = gametime_end - gametime_start
        if ticks <= 0:
            raise RuntimeError(f"Non-positive gametime delta: {gametime_start} -> {gametime_end}")
        cpu_seconds = max(0.0, cpu_end - cpu_start)
        context_delta = {
            key: context_end[key] - context_start[key] for key in ("voluntary", "involuntary")
        }
        if any(value < 0 for value in context_delta.values()):
            raise RuntimeError(f"Context-switch counters went backwards: {context_start} -> {context_end}")
        if not rss_samples:
            raise RuntimeError("No RSS samples were collected")
        metrics = {
            "ticks": ticks,
            "wall_seconds": wall_seconds,
            "cpu_seconds": cpu_seconds,
            "cpu_percent_of_one_core": cpu_seconds / wall_seconds * 100.0,
            "cpu_ms_per_tick": cpu_seconds * 1000.0 / ticks,
            "observed_tps": ticks / wall_seconds,
            "rss_start_bytes": rss_start,
            "rss_end_bytes": rss_end,
            "rss_mean_bytes": int(statistics.fmean(rss_samples)),
            "rss_p95_bytes": _percentile([float(value) for value in rss_samples], 0.95),
            "rss_peak_bytes": max(rss_samples),
            "context_switches_start": context_start,
            "context_switches_end": context_end,
            "context_switches_delta": context_delta,
            "rss_samples": len(rss_samples),
        }
        if not all(math.isfinite(float(metrics[key])) for key in ("wall_seconds", "cpu_seconds", "observed_tps")):
            raise RuntimeError(f"Comparison metrics contain non-finite values: {metrics}")
        self.result["measurement"]["metrics"] = metrics
        self._write_results()
        self.profiler_inactive("after")

    def validate_workload_metrics(self) -> None:
        if self.measure_start_ns is None or self.measure_end_ns is None:
            raise RuntimeError("measurement boundaries are missing")
        path = self.workload_metrics_path
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"comparable workload metrics are missing or symlinked: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"comparable workload metrics are unreadable: {path}") from exc
        if not isinstance(payload, dict) or payload.get("metric") != "endstone_server_current_mspt_tps":
            raise RuntimeError(f"invalid comparable workload metric payload: {payload}")
        if payload.get("iterations") != WORKLOAD_ITERATIONS:
            raise RuntimeError(f"workload iterations drifted: {payload.get('iterations')}")
        raw_samples = payload.get("samples")
        if not isinstance(raw_samples, list):
            raise TypeError("comparable workload samples are missing")
        samples = []
        for item in raw_samples:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = int(item["monotonic_ns"])
                mspt = float(item["mspt"])
                tps = float(item["tps"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"invalid comparable workload sample: {item}") from exc
            if self.measure_start_ns <= timestamp <= self.measure_end_ns:
                if not math.isfinite(mspt) or not math.isfinite(tps) or mspt < 0 or tps < 0:
                    raise RuntimeError(f"invalid MSPT/TPS sample: {item}")
                samples.append((timestamp, mspt, tps))
        if len(samples) < max(5, self.measurement_seconds // 2):
            raise RuntimeError(f"too few comparable MSPT/TPS samples: {len(samples)}")
        samples.sort()
        mspt = [item[1] for item in samples]
        tps = [item[2] for item in samples]
        observed = {
            "samples": len(samples),
            "mspt_mean": statistics.fmean(mspt),
            "mspt_p50": _percentile(mspt, 0.50),
            "mspt_p95": _percentile(mspt, 0.95),
            "mspt_p99": _percentile(mspt, 0.99),
            "mspt_max": max(mspt),
            "tps_mean": statistics.fmean(tps),
            "tps_p50": _percentile(tps, 0.50),
            "tps_p95": _percentile(tps, 0.95),
            "tps_p99": _percentile(tps, 0.99),
            "tps_min": min(tps),
            "tps_max": max(tps),
            "window_start_ns": self.measure_start_ns,
            "window_end_ns": self.measure_end_ns,
        }
        self.result["measurement"]["workload_metrics"] = observed
        self.check("comparable-workload-metrics", "PASS", **observed)

    def execute_case(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-install"
            self.install_artifacts()
            stage = "server-bootstrap"
            self.bootstrap_server()
            stage = "measurement"
            self.measure()
            stage = "shutdown"
            self.shutdown()
            stage = "workload-validation"
            self.validate_workload_metrics()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            return 0
        except Exception as exc:  # noqa: BLE001 - integration failures become evidence
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1200]
            diagnostic = traceback.format_exc()
            try:
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.record_server_lifecycle()
                    self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:  # noqa: BLE001 - preserve primary failure
                diagnostic += "\n\nCleanup failure:\n" + traceback.format_exc()
            last_lines = self.server.snapshot()[-250:] if self.server is not None else []
            self.diagnostics.write_text(
                diagnostic + "\n\nLast BDS log lines:\n" + "\n".join(last_lines), encoding="utf-8"
            )
            return 1
        finally:
            self.result["completed_at"] = now_iso()
            self.split_logs()
            self._write_results()
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def _case_directory(root: pathlib.Path, name: str) -> pathlib.Path:
    root = pathlib.Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    if path.exists():
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise RuntimeError(f"comparison case directory is not fresh: {path}")
    else:
        path.mkdir()
    return path


def _numeric_deltas(results: dict[str, dict[str, Any]], sections: tuple[str, ...]) -> dict[str, float]:
    values: dict[str, dict[str, Any]] = {}
    for case_name in ("control", "monitoring"):
        value: Any = results.get(case_name, {})
        for section in sections:
            value = value.get(section) if isinstance(value, dict) else None
        if isinstance(value, dict):
            values[case_name] = value
    control = values.get("control")
    monitoring = values.get("monitoring")
    if not isinstance(control, dict) or not isinstance(monitoring, dict):
        return {}
    deltas: dict[str, float] = {}
    for key in sorted(set(control) & set(monitoring)):
        before = control[key]
        after = monitoring[key]
        if (
            isinstance(before, (int, float))
            and not isinstance(before, bool)
            and isinstance(after, (int, float))
            and not isinstance(after, bool)
            and math.isfinite(float(before))
            and math.isfinite(float(after))
        ):
            deltas[key] = float(after) - float(before)
    return deltas


def run_pair(
    evidence_root: pathlib.Path,
    spark_sha: str,
    *,
    bds_version: str,
    measurement_seconds: int,
    warmup_seconds: int,
) -> int:
    pair_id = f"{_exact_sha(spark_sha, 'spark_sha')[:12]}-{time.time_ns()}"
    results: dict[str, dict[str, Any]] = {}
    exit_code = 0
    previous = pathlib.Path.cwd()
    for name, spark_enabled in (("control", False), ("monitoring", True)):
        case_dir = _case_directory(evidence_root, name)
        os.chdir(case_dir)
        try:
            validator = FinalControlMonitoringCase(
                name,
                spark_enabled,
                spark_sha,
                bds_version=bds_version,
                measurement_seconds=measurement_seconds,
                warmup_seconds=warmup_seconds,
            )
            code = validator.execute_case()
            exit_code = max(exit_code, code)
            results[name] = validator.result
        finally:
            os.chdir(previous)
    comparison = {
        "test_kind": "spark-control-monitoring-paired-comparison",
        "pair_id": pair_id,
        "paired": True,
        "repetition": 1,
        "platform": "linux",
        "spark_sha": _exact_sha(spark_sha, "spark_sha"),
        "workload_contract": {
            "warmup_seconds": warmup_seconds,
            "measurement_seconds": measurement_seconds,
            "workload_iterations": WORKLOAD_ITERATIONS,
            "fixture": "endstone-final-comparable-workload",
            "metrics_clock": "monotonic_ns",
        },
        "cases": {
            name: {
                "status": data.get("status"),
                "result_path": f"{name}/test-results.json",
                "spark_enabled": data.get("spark_enabled"),
                "metrics": (data.get("measurement") or {}).get("metrics"),
                "workload_metrics": (data.get("measurement") or {}).get("workload_metrics"),
            }
            for name, data in results.items()
        },
        "monitoring_minus_control": {
            "measurement_metrics": _numeric_deltas(results, ("measurement", "metrics")),
            "workload_metrics": _numeric_deltas(results, ("measurement", "workload_metrics")),
        },
        "status": "PASS" if exit_code == 0 and len(results) == 2 else "FAIL",
    }
    write_json(pathlib.Path(evidence_root).resolve() / "comparison.json", comparison)
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["both"], default="both")
    parser.add_argument("--spark-sha", default=os.environ.get("EXPECTED_SPARK_SHA", ""))
    parser.add_argument("--bds-version", default=os.environ.get("EXPECTED_BDS_VERSION", ""))
    parser.add_argument("--evidence-root", default="evidence")
    parser.add_argument("--measurement-seconds", type=int, default=DEFAULT_MEASUREMENT_SECONDS)
    parser.add_argument("--warmup-seconds", type=int, default=DEFAULT_WARMUP_SECONDS)
    args = parser.parse_args()
    return run_pair(
        pathlib.Path(args.evidence_root),
        args.spark_sha,
        bds_version=args.bds_version,
        measurement_seconds=args.measurement_seconds,
        warmup_seconds=args.warmup_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
