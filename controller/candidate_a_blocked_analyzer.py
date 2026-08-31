#!/usr/bin/env python3
"""Analyze deterministic Candidate A blocked benchmark evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import statistics
from collections import abc
from itertools import pairwise
from typing import Any

from controller.bstats import (
    B_STATS_CANONICAL_TOML,
    B_STATS_CONFIG_RELATIVE_PATH,
    B_STATS_EVIDENCE_PATH,
    BStatsConfigError,
    inspect_bstats_config,
)
from controller.candidate_a_blocked_benchmark import (
    AFFINITY_POLL_INTERVAL_SECONDS,
    BASELINE_SHA,
    BLOCK_SIZE,
    BOT_COUNT,
    BOT_PROGRESS_COUNTER_SCOPE,
    BOT_REF,
    BOT_SCENARIO,
    BOT_SCENARIO_SHA256,
    CANDIDATE_SHA,
    CHUNK_RADIUS,
    CPU_METRIC_RESOLUTION_LIMIT_PERCENTAGE_POINTS,
    ENDSTONE_SHA,
    EVIDENCE_MANIFEST_NAME,
    HOTSPOT_ITERATIONS,
    HOTSPOT_ITERATIONS_RATIONALE,
    HOTSPOT_MODE,
    INPUT_COUNTER_KEYS,
    LEGAL_START_BLOCKS,
    MANAGED_ROOT_TID_SCOPE,
    MAX_BLOCKS,
    MAX_EVIDENCE_BYTES,
    MAX_EVIDENCE_FILE_BYTES,
    MEASUREMENT_SECONDS,
    PROGRESS_COUNTER_KEYS,
    PROTOCOL_VERSION,
    RUNTIME_PAYLOAD_DIRS,
    SAMPLE_INTERVAL_MS,
    STATIONARY_BOUNDED_AREA_POLICY,
    TREATMENTS,
    WARMUP_SECONDS,
    WORLD_SNAPSHOT_ID,
    batch_schedule,
    block_schedule,
    case_id,
    extract_pep_events,
    validate_pep_events,
    validate_sha,
)

CPU_CI_HALF_WIDTH_LIMIT = 0.5
REJECTED_ARTIFACT_PREFIX = "candidate-a-blocked-rejected-diagnostics-"
SEQUENTIAL_CONFIDENCE = 0.99
SEQUENTIAL_FAMILYWISE_CONFIDENCE = 0.95
SEQUENTIAL_LOOKS = (4, 8, 12, 16, 20)
_T_995 = {
    4: 5.840909,
    8: 3.499483,
    12: 3.105807,
    16: 2.946714,
    20: 2.860935,
}
_T_975 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


class EvidenceError(ValueError):
    """Raised when the evidence cannot support a pre-registered estimate."""


def t_critical_975(degrees_of_freedom: int) -> float:
    if degrees_of_freedom < 1:
        raise ValueError(f"degrees of freedom must be positive: {degrees_of_freedom}")
    if degrees_of_freedom in _T_975:
        return _T_975[degrees_of_freedom]
    return 1.96


def ci_half_width(values: abc.Iterable[float], confidence: float = 0.95) -> float | None:
    """Return a descriptive two-sided 95% Student-t CI half-width."""

    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be between zero and one: {confidence}")
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if len(finite) < 2:
        return None
    deviation = statistics.stdev(finite)
    if deviation == 0.0:
        return 0.0
    if confidence != 0.95:
        raise ValueError("only the descriptive 95% confidence level is supported")
    return t_critical_975(len(finite) - 1) * deviation / math.sqrt(len(finite))


def sequential_ci(values: abc.Iterable[float]) -> dict[str, Any]:
    """Return the pre-registered 99% interval for one cumulative look."""

    finite = [float(value) for value in values if math.isfinite(float(value))]
    result: dict[str, Any] = {
        "n": len(finite),
        "degrees_of_freedom": len(finite) - 1 if finite else None,
        "confidence": SEQUENTIAL_CONFIDENCE,
        "familywise_confidence": SEQUENTIAL_FAMILYWISE_CONFIDENCE,
        "critical_value": None,
        "mean": None,
        "sd": None,
        "half_width": None,
        "lower": None,
        "upper": None,
        "precision_target_met": False,
    }
    if len(finite) < 2:
        return result
    mean = statistics.fmean(finite)
    deviation = statistics.stdev(finite)
    critical = _T_995.get(len(finite))
    result.update({"mean": mean, "sd": deviation, "critical_value": critical})
    if critical is None:
        return result
    half_width = critical * deviation / math.sqrt(len(finite))
    result.update(
        {
            "half_width": half_width,
            "lower": mean - half_width,
            "upper": mean + half_width,
            "precision_target_met": half_width <= CPU_CI_HALF_WIDTH_LIMIT,
        }
    )
    return result


def sequential_decision(
    interval: dict[str, Any], *, valid: bool, end_block: int, max_blocks: int = MAX_BLOCKS
) -> tuple[str, str]:
    """Apply the frozen five-look stopping and direction rule."""

    if not valid:
        outcome = "MAX_INCONCLUSIVE" if end_block >= max_blocks else "CONTINUE"
        return outcome, "correctness/workload/evidence checks failed; no CPU claim is permitted"
    n = interval.get("n")
    half_width = interval.get("half_width")
    if n != end_block or n not in SEQUENTIAL_LOOKS or half_width is None:
        outcome = "MAX_INCONCLUSIVE" if end_block >= max_blocks else "CONTINUE"
        return outcome, "the cumulative case count is not a complete pre-registered look"
    if half_width > CPU_CI_HALF_WIDTH_LIMIT:
        if end_block >= MAX_BLOCKS or end_block >= max_blocks:
            return "MAX_INCONCLUSIVE", "maximum of 20 blocks reached above the 0.5 percentage-point precision target"
        return "CONTINUE", "complete valid look remains above the 0.5 percentage-point precision target"
    lower = interval.get("lower")
    upper = interval.get("upper")
    if isinstance(upper, (int, float)) and upper < 0.0:
        return "KEEP", "99% confirmatory interval is wholly below zero"
    if isinstance(lower, (int, float)) and lower > 0.0:
        return "REVERT", "99% confirmatory interval is wholly above zero"
    return "INCONCLUSIVE", "99% confirmatory interval includes zero"


def summarize(values: abc.Iterable[float]) -> dict[str, Any]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "sd": None,
            "min": None,
            "max": None,
            "cv_percent": None,
            "ci95_half_width": None,
        }
    mean = statistics.fmean(finite)
    deviation = statistics.stdev(finite) if len(finite) >= 2 else None
    cv = None if deviation is None or mean == 0.0 else abs(deviation / mean) * 100.0
    return {
        "n": len(finite),
        "mean": mean,
        "median": statistics.median(finite),
        "sd": deviation,
        "min": min(finite),
        "max": max(finite),
        "cv_percent": cv,
        "ci95_half_width": ci_half_width(finite),
    }


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"{name} is not numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise EvidenceError(f"{name} is not finite: {value!r}")
    return result


def _protocol(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("protocol")
    if not isinstance(value, dict):
        raise EvidenceError("case result is missing protocol metadata")
    return value


def _performance(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("performance")
    if not isinstance(value, dict):
        raise EvidenceError("case result is missing performance metrics")
    return value


def _metric_values(result: dict[str, Any]) -> dict[str, float]:
    performance = _performance(result)
    tick = performance.get("tick_statistics")
    if not isinstance(tick, dict):
        raise EvidenceError("case result is missing tick statistics")
    return {
        "cpu_percent_of_one_core": _number(
            performance.get("process_cpu_percent_of_one_core", performance.get("cpu_percent_of_one_core")),
            "process CPU percent of one core",
        ),
        "cpu_ms_per_tick": _number(performance.get("cpu_ms_per_tick"), "CPU milliseconds per tick"),
        "mspt_mean": _number(tick.get("mspt_mean"), "MSPT mean"),
    }


def _metadata_spark_sha(result: dict[str, Any]) -> str | None:
    metadata = result.get("artifact_metadata")
    if isinstance(metadata, dict):
        components = metadata.get("components")
        if isinstance(components, dict):
            spark = components.get("spark")
            if isinstance(spark, dict) and spark.get("sha"):
                return str(spark["sha"]).lower()
    for key in ("observed_spark_sha", "spark_sha"):
        if result.get(key):
            return str(result[key]).lower()
    return None


def _check_endstone_metadata(result: dict[str, Any], errors: list[str]) -> dict[str, Any] | None:
    metadata = result.get("artifact_metadata")
    components = metadata.get("components") if isinstance(metadata, dict) else None
    endstone = components.get("endstone") if isinstance(components, dict) else None
    if not isinstance(endstone, dict):
        errors.append("Endstone artifact metadata is missing")
        return None
    observed_sha = str(endstone.get("sha") or "").lower()
    if observed_sha != ENDSTONE_SHA:
        errors.append(f"Endstone SHA mismatch: observed={observed_sha!r} expected={ENDSTONE_SHA!r}")
    if endstone.get("repository") != "EndstoneMC/endstone":
        errors.append(f"Endstone repository metadata mismatch: {endstone.get('repository')!r}")
    artifact = endstone.get("artifact")
    run_id = endstone.get("run_id")
    if not isinstance(artifact, dict):
        errors.append("Endstone artifact identity metadata is missing")
        artifact = {}
    artifact_id = artifact.get("id")
    artifact_name = artifact.get("name")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        errors.append("Endstone workflow run ID is missing or invalid")
    if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0:
        errors.append("Endstone artifact ID is missing or invalid")
    if not isinstance(artifact_name, str) or not artifact_name.strip():
        errors.append("Endstone artifact name is missing")
    protocol = result.get("protocol")
    if not isinstance(protocol, dict) or protocol.get("endstone_sha") != ENDSTONE_SHA:
        errors.append("protocol Endstone SHA is missing or mismatched")
    stable = {
        "repository": endstone.get("repository"),
        "sha": observed_sha,
        "run_id": run_id,
        "artifact": {
            key: artifact.get(key)
            for key in ("id", "name", "size_in_bytes", "expires_at")
        },
    }
    protocol_artifact = protocol.get("endstone_artifact") if isinstance(protocol, dict) else None
    if not isinstance(protocol_artifact, dict):
        errors.append("protocol Endstone artifact metadata is missing")
    else:
        for key in ("repository", "sha", "run_id", "artifact"):
            if protocol_artifact.get(key) != stable.get(key):
                errors.append(f"protocol Endstone artifact {key} does not match artifact metadata")
    return stable


def _check_bstats_config(
    result: dict[str, Any], case_path: pathlib.Path | None, errors: list[str], expected_id: str
) -> dict[str, Any] | None:
    protocol = result.get("protocol")
    metadata = protocol.get("bstats_config") if isinstance(protocol, dict) else None
    if not isinstance(metadata, dict):
        errors.append(f"{expected_id}: bStats disablement evidence is missing")
        return None
    expected_metadata = {
        "relative_path": B_STATS_CONFIG_RELATIVE_PATH,
        "evidence_path": B_STATS_EVIDENCE_PATH,
        "canonical_toml": B_STATS_CANONICAL_TOML,
        "canonical_enabled": False,
    }
    for key, expected in expected_metadata.items():
        if metadata.get(key) != expected:
            errors.append(f"{expected_id}: bStats {key} mismatch: {metadata.get(key)!r} != {expected!r}")
    if case_path is None:
        errors.append(f"{expected_id}: bStats config evidence path is unavailable")
        return None
    case_root = case_path.parent
    config_path = case_root / B_STATS_EVIDENCE_PATH
    try:
        if config_path.resolve().relative_to(case_root.resolve()) != pathlib.Path(B_STATS_EVIDENCE_PATH):
            raise BStatsConfigError("bStats evidence path escapes case directory")
        observed = inspect_bstats_config(config_path)
    except (BStatsConfigError, OSError, RuntimeError) as exc:
        errors.append(f"{expected_id}: bStats config evidence is invalid: {exc}")
        return None
    for key in ("bytes", "sha256"):
        if metadata.get(key) != observed[key]:
            errors.append(f"{expected_id}: bStats {key} mismatch: {metadata.get(key)!r} != {observed[key]!r}")
    if metadata.get("bytes") != len(B_STATS_CANONICAL_TOML.encode("utf-8")) + 1:
        errors.append(f"{expected_id}: bStats config byte count is not canonical")
    return observed


def _check_window(result: dict[str, Any], errors: list[str]) -> dict[str, Any] | None:
    performance_value = result.get("performance")
    performance = performance_value if isinstance(performance_value, dict) else {}
    windows = result.get("counter_windows") or performance.get("counter_windows")
    if not isinstance(windows, dict):
        errors.append("missing counter windows")
        return None
    warmup = windows.get("warmup")
    measurement = windows.get("measurement")
    if not isinstance(warmup, dict) or not isinstance(measurement, dict):
        errors.append("counter windows must contain warmup and measurement")
        return None
    if warmup.get("configured_seconds") != WARMUP_SECONDS:
        errors.append(f"warmup configured seconds mismatch: {warmup.get('configured_seconds')!r}")
    if measurement.get("configured_seconds") != MEASUREMENT_SECONDS:
        errors.append(f"measurement configured seconds mismatch: {measurement.get('configured_seconds')!r}")
    try:
        warmup_observed = float(warmup["observed_seconds"])
        measurement_observed = float(measurement["observed_seconds"])
        warmup_start = int(warmup["start_monotonic_ns"])
        warmup_end = int(warmup["end_monotonic_ns"])
        start = int(measurement["start_monotonic_ns"])
        end = int(measurement["end_monotonic_ns"])
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"counter window boundary is malformed: {exc}")
        return windows
    if warmup_observed < WARMUP_SECONDS - 1.0:
        errors.append(f"warmup was shorter than 60 seconds: {warmup_observed}")
    if warmup_end <= warmup_start:
        errors.append("warmup monotonic boundaries are not increasing")
    if not MEASUREMENT_SECONDS - 1.0 <= measurement_observed <= MEASUREMENT_SECONDS + 3.0:
        errors.append(f"measurement wall duration is not exact 600 seconds: {measurement_observed}")
    if end <= start:
        errors.append("measurement monotonic boundaries are not increasing")
    if start < warmup_end:
        errors.append("measurement starts before warmup ends")
    cpu_snapshots = performance.get("cpu_snapshots")
    if not isinstance(cpu_snapshots, dict):
        errors.append("CPU snapshot interval evidence is missing")
        cpu_snapshots = {}
    cpu_start_snapshot = cpu_snapshots.get("start")
    cpu_end_snapshot = cpu_snapshots.get("end")
    try:
        cpu_start_ns = int(cpu_start_snapshot["monotonic_ns"])
        cpu_end_ns = int(cpu_end_snapshot["monotonic_ns"])
        cpu_start_seconds = float(cpu_start_snapshot["cpu_seconds"])
        cpu_end_seconds = float(cpu_end_snapshot["cpu_seconds"])
        cpu_interval_ns = int(cpu_snapshots["interval_ns"])
        cpu_interval_seconds = float(cpu_snapshots["interval_seconds"])
        cpu_resolution_seconds = float(cpu_snapshots["counter_resolution_seconds"])
        metric_resolution = float(cpu_snapshots["metric_resolution_percentage_points"])
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"CPU snapshot interval evidence is malformed: {exc}")
    else:
        if any(not math.isfinite(value) for value in (cpu_start_seconds, cpu_end_seconds, cpu_interval_seconds, cpu_resolution_seconds, metric_resolution)):
            errors.append("CPU snapshot interval evidence contains a non-finite value")
        if cpu_start_ns <= 0 or cpu_end_ns <= cpu_start_ns:
            errors.append("CPU snapshot timestamps are not increasing")
        if cpu_start_ns != start or cpu_end_ns != end:
            errors.append("measurement window boundaries do not equal CPU snapshot timestamps")
        if cpu_interval_ns != cpu_end_ns - cpu_start_ns:
            errors.append("CPU snapshot interval nanoseconds do not equal its timestamps")
        if cpu_snapshots.get("denominator") != "end CPU snapshot monotonic_ns - start CPU snapshot monotonic_ns":
            errors.append("CPU snapshot denominator declaration is missing or mismatched")
        expected_interval_seconds = (cpu_end_ns - cpu_start_ns) / 1_000_000_000
        if expected_interval_seconds <= 0.0:
            errors.append("CPU snapshot interval is not positive")
        else:
            if abs(cpu_interval_seconds - expected_interval_seconds) > 1e-6:
                errors.append("CPU snapshot interval seconds do not equal its timestamps")
            if abs(measurement_observed - expected_interval_seconds) > 1e-6:
                errors.append("measurement observed seconds do not equal the CPU snapshot interval")
            try:
                performance_cpu_seconds = float(performance["cpu_seconds"])
                performance_cpu_percent = float(performance["process_cpu_percent_of_one_core"])
                performance_wall_seconds = float(performance["wall_seconds"])
            except (KeyError, TypeError, ValueError):
                errors.append("reported process CPU snapshot metrics are missing or malformed")
            else:
                expected_cpu_seconds = cpu_end_seconds - cpu_start_seconds
                expected_cpu_percent = expected_cpu_seconds / expected_interval_seconds * 100.0
                if expected_cpu_seconds < 0.0 or abs(performance_cpu_seconds - expected_cpu_seconds) > 1e-9:
                    errors.append("reported CPU seconds do not equal the CPU snapshot delta")
                if abs(performance_cpu_percent - expected_cpu_percent) > 1e-9:
                    errors.append("reported CPU percent does not use the CPU snapshot denominator")
                if abs(performance_wall_seconds - expected_interval_seconds) > 1e-6:
                    errors.append("reported wall seconds do not equal the CPU snapshot interval")
            if cpu_resolution_seconds <= 0.0:
                errors.append("CPU counter resolution is not positive")
            expected_metric_resolution = cpu_resolution_seconds / expected_interval_seconds * 100.0
            if abs(metric_resolution - expected_metric_resolution) > 1e-9:
                errors.append("recorded CPU metric resolution does not match the counter resolution and interval")
            if metric_resolution >= CPU_METRIC_RESOLUTION_LIMIT_PERCENTAGE_POINTS:
                errors.append("CPU metric resolution is not finer than the 0.5 percentage-point target")
    tick_start_ns = measurement.get("tick_start_monotonic_ns")
    tick_end_ns = measurement.get("tick_end_monotonic_ns")
    try:
        tick_start_timestamp = int(tick_start_ns)
        tick_end_timestamp = int(tick_end_ns)
    except (TypeError, ValueError):
        errors.append("tick snapshot timestamps are missing or malformed")
    else:
        if tick_start_timestamp <= 0 or tick_end_timestamp <= tick_start_timestamp:
            errors.append("tick snapshot timestamps are not increasing")
        if tick_start_timestamp > start or tick_end_timestamp > end:
            errors.append("tick snapshots extend beyond the CPU snapshot interval")
        if start - tick_start_timestamp > 5_000_000_000 or end - tick_end_timestamp > 5_000_000_000:
            errors.append("tick snapshots are not boundary-aligned within the command tolerance")
    measurement_ticks = measurement.get("ticks")
    tick_start = measurement.get("tick_start")
    tick_end = measurement.get("tick_end")
    try:
        performance_ticks = int(performance.get("ticks", -1))
        measured_ticks = int(measurement_ticks)
        measured_tick_start = int(tick_start)
        measured_tick_end = int(tick_end)
        if measured_ticks != performance_ticks:
            errors.append(
                f"measurement tick counter mismatch: window={measurement_ticks} performance={performance_ticks}"
            )
        if measured_tick_end - measured_tick_start != measured_ticks:
            errors.append(
                "measurement gametime boundaries do not equal the reported tick count"
            )
    except (TypeError, ValueError):
        errors.append(
            f"measurement tick counters are malformed: start={tick_start!r} end={tick_end!r} ticks={measurement_ticks!r}"
        )
    tick_statistics = performance.get("tick_statistics")
    tick_window = tick_statistics.get("window") if isinstance(tick_statistics, dict) else None
    if not isinstance(tick_window, dict):
        errors.append("tick statistics window evidence is missing")
    else:
        try:
            stats_start = int(tick_window["start_monotonic_ns"])
            stats_end = int(tick_window["end_monotonic_ns"])
        except (KeyError, TypeError, ValueError):
            errors.append("tick statistics window boundaries are malformed")
        else:
            if stats_start != start or stats_end != end:
                errors.append("tick statistics window does not equal the CPU snapshot interval")
            if tick_window.get("inclusive") is not True:
                errors.append("tick statistics window inclusivity is not declared")
    return windows


def _valid_cpu_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(cpu, int) and not isinstance(cpu, bool) and cpu >= 0 for cpu in value)
        and value == sorted(set(value))
    )


def _valid_tid_map(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    tids: list[int] = []
    for raw_tid, cpus in value.items():
        if isinstance(raw_tid, bool):
            return False
        try:
            tid = int(raw_tid)
        except (TypeError, ValueError):
            return False
        if tid <= 0 or not _valid_cpu_list(cpus):
            return False
        tids.append(tid)
    return len(tids) == len(set(tids))


def _is_absolute_path(value: str) -> bool:
    return pathlib.PurePosixPath(value).is_absolute() or pathlib.PureWindowsPath(value).is_absolute()


def _check_affinity_snapshot(
    snapshot: Any,
    *,
    label: str,
    expected_pid: Any,
    errors: list[str],
) -> None:
    if not isinstance(snapshot, dict):
        errors.append(f"{label} affinity snapshot is missing")
        return
    pid = snapshot.get("pid")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or pid != expected_pid
    ):
        errors.append(f"{label} affinity snapshot PID is missing or mismatched")
    create_time = snapshot.get("create_time")
    if (
        isinstance(create_time, bool)
        or not isinstance(create_time, (int, float))
        or not math.isfinite(float(create_time))
    ):
        errors.append(f"{label} affinity snapshot create-time is missing or invalid")
    if not _valid_cpu_list(snapshot.get("process_affinity")):
        errors.append(f"{label} original process affinity is missing or malformed")
    if not _valid_tid_map(snapshot.get("tid_affinities")):
        errors.append(f"{label} original per-TID affinity is missing or malformed")


def _check_affinity_restoration(affinity: dict[str, Any], errors: list[str]) -> None:
    original = affinity.get("original_affinity")
    if not isinstance(original, dict):
        errors.append("original process and per-TID affinity evidence is missing")
        return
    expected_pids = {
        "controller": affinity.get("controller_pid"),
        "bds": affinity.get("bds_pid"),
        "load_generator": affinity.get("load_generator_pid"),
    }
    for label, expected_pid in expected_pids.items():
        _check_affinity_snapshot(original.get(label), label=label, expected_pid=expected_pid, errors=errors)
    restoration = affinity.get("restoration")
    if not isinstance(restoration, dict) or restoration.get("status") != "PASS" or restoration.get("verified") is not True:
        errors.append("affinity restoration was not proven")
        return
    restored = restoration.get("restored")
    if not isinstance(restored, dict):
        errors.append("restored affinity evidence is missing")
        return
    for label, expected_pid in expected_pids.items():
        snapshot = original.get(label)
        restored_snapshot = restored.get(label)
        _check_affinity_snapshot(
            restored_snapshot,
            label=f"restored {label}",
            expected_pid=expected_pid,
            errors=errors,
        )
        if not isinstance(snapshot, dict) or not isinstance(restored_snapshot, dict):
            continue
        if restored_snapshot.get("process_affinity") != snapshot.get("process_affinity"):
            errors.append(f"{label} process affinity was not restored to its original value")
        original_tids = snapshot.get("tid_affinities")
        restored_tids = restored_snapshot.get("tid_affinities")
        if isinstance(original_tids, dict) and isinstance(restored_tids, dict):
            original_by_tid = {str(raw_tid): cpus for raw_tid, cpus in original_tids.items()}
            restored_by_tid = {str(raw_tid): cpus for raw_tid, cpus in restored_tids.items()}
            for raw_tid, cpus in original_by_tid.items():
                restored_cpus = restored_by_tid.get(raw_tid)
                if restored_cpus is not None and restored_cpus != cpus:
                    errors.append(f"{label} TID {raw_tid} affinity was not restored to its original value")
            process_affinity = snapshot.get("process_affinity")
            for raw_tid, cpus in restored_by_tid.items():
                if raw_tid not in original_by_tid and cpus != process_affinity:
                    errors.append(f"{label} new TID {raw_tid} affinity was not restored to process affinity")


def _topology_signature(affinity: Any) -> str | None:
    if not isinstance(affinity, dict):
        return None
    topology = affinity.get("runner_cpu_topology")
    if not isinstance(topology, dict):
        return None
    signature = {
        "allowed_cpus": topology.get("allowed_cpus"),
        "controlled_cpu": topology.get("controlled_cpu", affinity.get("controlled_cpu")),
        "load_cpus": topology.get("load_cpus", affinity.get("load_generator_affinity")),
        "cpu_count": topology.get("cpu_count"),
        "controlled_process_isolation": topology.get("controlled_process_isolation"),
        "host_work_excluded": topology.get("host_work_excluded"),
        "kernel_and_unrelated_host_work_excluded": topology.get("kernel_and_unrelated_host_work_excluded"),
    }
    original = affinity.get("original_affinity")
    if isinstance(original, dict):
        signature["original_process_affinity"] = {
            label: value.get("process_affinity") if isinstance(value, dict) else None
            for label, value in sorted(original.items())
        }
    return json.dumps(signature, sort_keys=True, separators=(",", ":"))


def _check_affinity(result: dict[str, Any], errors: list[str]) -> dict[str, Any] | None:
    affinity = result.get("affinity")
    if not isinstance(affinity, dict):
        errors.append("missing CPU affinity evidence")
        return None
    if affinity.get("verified") is not True:
        errors.append("controlled-process CPU affinity was not verified")
    if affinity.get("bds_tid_scope") != MANAGED_ROOT_TID_SCOPE:
        errors.append("BDS TID affinity scope is not the managed Endstone/BDS root")
    identity = affinity.get("managed_root_identity")
    if not isinstance(identity, dict):
        errors.append("managed Endstone/BDS root process identity is missing")
    else:
        identity_pid = identity.get("pid")
        server_pid = identity.get("server_process_pid")
        if (
            isinstance(identity_pid, bool)
            or not isinstance(identity_pid, int)
            or identity_pid <= 0
            or isinstance(server_pid, bool)
            or not isinstance(server_pid, int)
            or server_pid != identity_pid
        ):
            errors.append("managed root identity does not match the ServerProcess PID")
        for field in ("create_time", "server_process_create_time"):
            value = identity.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                errors.append(f"managed root identity {field} is missing or invalid")
        if (
            isinstance(identity.get("create_time"), (int, float))
            and isinstance(identity.get("server_process_create_time"), (int, float))
            and abs(float(identity["create_time"]) - float(identity["server_process_create_time"])) > 0.01
        ):
            errors.append("managed root identity create-time does not match ServerProcess create-time")
        if identity.get("role") != "managed_endstone_bds_root":
            errors.append("managed root identity role is missing or invalid")
        if not isinstance(identity.get("name"), str) or not identity["name"].strip():
            errors.append("managed root process name is missing")
        if not isinstance(identity.get("exe"), str) or not identity["exe"].strip():
            errors.append("managed root process executable is missing")
        interpreter = identity.get("interpreter")
        server_command = identity.get("server_process_command")
        if not isinstance(interpreter, str) or not interpreter.strip() or not _is_absolute_path(interpreter):
            errors.append("managed root Python interpreter identity is missing or not absolute")
        if not isinstance(server_command, list) or not server_command or not all(
            isinstance(argument, str) for argument in server_command
        ):
            errors.append("managed root ServerProcess command is missing")
        elif isinstance(interpreter, str) and (server_command[0] != interpreter):
            errors.append("managed root ServerProcess command does not start with the recorded interpreter")
        cmdline = identity.get("cmdline")
        folder = identity.get("server_folder")
        if not isinstance(cmdline, list) or not all(isinstance(argument, str) for argument in cmdline):
            errors.append("managed root process command line is missing")
        elif not isinstance(folder, str) or not folder:
            errors.append("managed root server folder is missing")
        else:
            if not _is_absolute_path(folder):
                errors.append("managed root server folder is not absolute")
            module_present = [
                index
                for index, argument in enumerate(cmdline)
                if argument == "-m" and index + 1 < len(cmdline) and cmdline[index + 1] == "endstone"
            ]
            folder_present = [
                index
                for index, argument in enumerate(cmdline)
                if argument == "--server-folder" and index + 1 < len(cmdline) and cmdline[index + 1] == folder
            ]
            if len(module_present) != 1 or len(folder_present) != 1:
                errors.append("managed root command line lacks exact -m endstone/--server-folder identity")
        if isinstance(server_command, list) and isinstance(cmdline, list) and cmdline != server_command:
            errors.append("managed root command line does not match the recorded ServerProcess command")
        if (
            isinstance(interpreter, str)
            and isinstance(identity.get("exe"), str)
            and os.path.realpath(identity["exe"]) != os.path.realpath(interpreter)
        ):
            errors.append("managed root executable does not match the recorded Python interpreter")
        if identity.get("alive") is not True:
            errors.append("managed root liveness was not verified")
    controlled = affinity.get("controlled_cpu")
    bds = affinity.get("bds_affinity_after", affinity.get("bds_affinity"))
    load = affinity.get("load_generator_affinity")
    controller = affinity.get("controller_affinity")
    def canonical_cpu_list(value: Any) -> bool:
        return (
            isinstance(value, list)
            and all(isinstance(cpu, int) and not isinstance(cpu, bool) and cpu >= 0 for cpu in value)
            and value == sorted(set(value))
        )

    def valid_tid_map(value: Any) -> bool:
        if not isinstance(value, dict) or not value:
            return False
        tids: list[int] = []
        for raw_tid in value:
            if isinstance(raw_tid, bool):
                return False
            try:
                tid = int(raw_tid)
            except (TypeError, ValueError):
                return False
            if tid <= 0:
                return False
            tids.append(tid)
        return len(tids) == len(set(tids))
    if (
        isinstance(controlled, bool)
        or not isinstance(controlled, int)
        or controlled < 0
        or not canonical_cpu_list(bds)
        or bds != [controlled]
    ):
        errors.append(f"BDS affinity does not identify one controlled CPU: {affinity}")
    if not canonical_cpu_list(load) or not load:
        errors.append("load-generator affinity is missing")
    elif isinstance(controlled, int) and controlled in load:
        errors.append(f"load generator shares controlled BDS CPU {controlled}")
    if not canonical_cpu_list(controller) or not controller:
        errors.append("controller affinity is missing")
    elif isinstance(controlled, int) and controlled in controller:
        errors.append(f"controller shares controlled BDS CPU {controlled}")
    if canonical_cpu_list(load) and canonical_cpu_list(controller) and load != controller:
        errors.append("controller and load-generator CPU sets are not the same disjoint control set")
    available = affinity.get("available_cpus")
    if not canonical_cpu_list(available) or not available:
        errors.append("runner CPU topology evidence is missing")
    elif isinstance(controlled, int) and (
        controlled not in available
        or (canonical_cpu_list(bds) and any(cpu not in available for cpu in bds))
        or (canonical_cpu_list(load) and any(cpu not in available for cpu in load))
        or (canonical_cpu_list(controller) and any(cpu not in available for cpu in controller))
    ):
        errors.append("affinity evidence contains a CPU outside the recorded runner topology")
    bds_tids = affinity.get("bds_tid_affinities")
    load_tids = affinity.get("load_generator_tid_affinities")
    controller_tids = affinity.get("controller_tid_affinities")
    if isinstance(available, list):
        for label, mapping in (
            ("BDS", bds_tids),
            ("load generator", load_tids),
            ("controller", controller_tids),
        ):
            if isinstance(mapping, dict) and any(
                not canonical_cpu_list(values) or any(cpu not in available for cpu in values)
                for values in mapping.values()
            ):
                errors.append(f"{label} TID affinity contains a CPU outside the recorded topology")
    if not valid_tid_map(bds_tids):
        errors.append("BDS per-TID affinity evidence is missing")
    elif isinstance(controlled, int) and any(
        not canonical_cpu_list(values) or values != [controlled] for values in bds_tids.values()
    ):
        errors.append("one or more BDS TIDs are outside the controlled CPU")
    if not valid_tid_map(load_tids):
        errors.append("load-generator per-TID affinity evidence is missing")
    elif isinstance(controlled, int) and any(
        not canonical_cpu_list(values) or controlled in values for values in load_tids.values()
    ):
        errors.append("one or more load-generator TIDs include the controlled CPU")
    if not valid_tid_map(controller_tids):
        errors.append("controller per-TID affinity evidence is missing")
    elif isinstance(controlled, int) and any(
        not canonical_cpu_list(values) or controlled in values for values in controller_tids.values()
    ):
        errors.append("one or more controller TIDs include the controlled CPU")
    if canonical_cpu_list(load) and isinstance(load_tids, dict) and any(
        not canonical_cpu_list(values) or values != load for values in load_tids.values()
    ):
        errors.append("load-generator TID affinity does not match the recorded load CPU set")
    if canonical_cpu_list(controller) and isinstance(controller_tids, dict) and any(
        not canonical_cpu_list(values) or values != controller for values in controller_tids.values()
    ):
        errors.append("controller TID affinity does not match the recorded controller CPU set")
    for label, tids, mapping in (
        ("BDS", affinity.get("bds_tids"), bds_tids),
        ("load generator", affinity.get("load_generator_tids"), load_tids),
        ("controller", affinity.get("controller_tids"), controller_tids),
    ):
        if not isinstance(tids, list) or not isinstance(mapping, dict):
            continue
        try:
            listed = {int(tid) for tid in tids}
            mapped = {int(tid) for tid in mapping}
        except (TypeError, ValueError):
            errors.append(f"{label} TID list is malformed")
        else:
            if listed != mapped:
                errors.append(f"{label} TID list does not match exact affinity records")
    topology = affinity.get("runner_cpu_topology")
    if not isinstance(topology, dict):
        errors.append("runner CPU topology model is missing")
    else:
        if topology.get("controlled_process_isolation") is not True:
            errors.append("controlled-process isolation was not declared")
        if topology.get("host_work_excluded") is not False or topology.get(
            "kernel_and_unrelated_host_work_excluded"
        ) is not False:
            errors.append("affinity evidence overclaims host or kernel isolation")
        if canonical_cpu_list(topology.get("allowed_cpus")) and canonical_cpu_list(available):
            if topology["allowed_cpus"] != available:
                errors.append("runner topology allowed CPU set differs from affinity evidence")
        else:
            errors.append("runner topology allowed CPU set is missing")
        if topology.get("controlled_cpu", controlled) != controlled:
            errors.append("runner topology controlled CPU differs from affinity evidence")
        if topology.get("load_cpus", load) != load:
            errors.append("runner topology load CPU set differs from affinity evidence")
        cpu_count = topology.get("cpu_count")
        if isinstance(cpu_count, bool) or not isinstance(cpu_count, int) or cpu_count < 1:
            errors.append("runner topology CPU count is missing or invalid")
    verification_count = affinity.get("verification_count")
    verification_samples = affinity.get("verification_samples")
    if isinstance(verification_count, bool) or not isinstance(verification_count, int) or verification_count < 2:
        errors.append("repeated per-TID affinity verification is missing")
    if not isinstance(verification_samples, list) or len(verification_samples) < 2:
        errors.append("per-TID affinity verification samples are missing")
    elif isinstance(verification_count, int) and verification_count != len(verification_samples):
        errors.append("per-TID affinity verification count does not match its samples")
    elif isinstance(verification_samples, list):
        phases = {sample.get("phase") for sample in verification_samples if isinstance(sample, dict)}
        if not {"warmup", "measurement"}.issubset(phases):
            errors.append("per-TID affinity samples do not cover warmup and measurement")
        sample_times: list[int] = []
        for index, sample in enumerate(verification_samples):
            if not isinstance(sample, dict):
                errors.append(f"per-TID affinity sample {index} is malformed")
                continue
            timestamp = sample.get("monotonic_ns")
            if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
                errors.append(f"per-TID affinity sample {index} timestamp is malformed")
            else:
                sample_times.append(timestamp)
            for label in ("bds_tids", "load_generator_tids", "controller_tids"):
                if not valid_tid_map(sample.get(label)):
                    errors.append(f"per-TID affinity sample {index} lacks exact {label}")
            sample_bds = sample.get("bds_tids")
            sample_load = sample.get("load_generator_tids")
            sample_controller = sample.get("controller_tids")
            if isinstance(controlled, int) and isinstance(sample_bds, dict) and any(
                not canonical_cpu_list(values) or values != [controlled] for values in sample_bds.values()
            ):
                errors.append(f"per-TID affinity sample {index} has a BDS TID outside the controlled CPU")
            if isinstance(controlled, int) and isinstance(sample_load, dict) and any(
                not canonical_cpu_list(values) or controlled in values for values in sample_load.values()
            ):
                errors.append(f"per-TID affinity sample {index} has a load TID on the controlled CPU")
            if canonical_cpu_list(load) and isinstance(sample_load, dict) and any(
                not canonical_cpu_list(values) or values != load for values in sample_load.values()
            ):
                errors.append(f"per-TID affinity sample {index} does not match the load CPU set")
            if isinstance(controlled, int) and isinstance(sample_controller, dict) and any(
                not canonical_cpu_list(values) or controlled in values for values in sample_controller.values()
            ):
                errors.append(f"per-TID affinity sample {index} has a controller TID on the controlled CPU")
            if canonical_cpu_list(controller) and isinstance(sample_controller, dict) and any(
                not canonical_cpu_list(values) or values != controller for values in sample_controller.values()
            ):
                errors.append(f"per-TID affinity sample {index} does not match the controller CPU set")
        if any(left >= right for left, right in pairwise(sample_times)):
            errors.append("per-TID affinity sample timestamps are not increasing")
    bds_pid = affinity.get("bds_pid")
    load_pid = affinity.get("load_generator_pid")
    controller_pid = affinity.get("controller_pid")
    if (
        isinstance(bds_pid, bool)
        or not isinstance(bds_pid, int)
        or bds_pid <= 0
        or isinstance(load_pid, bool)
        or not isinstance(load_pid, int)
        or load_pid <= 0
        or isinstance(controller_pid, bool)
        or not isinstance(controller_pid, int)
        or controller_pid <= 0
    ):
        errors.append("BDS/load-generator/controller process IDs are missing from affinity evidence")
    elif len({bds_pid, load_pid, controller_pid}) != 3:
        errors.append("BDS, load-generator, and controller process IDs are not distinct")
    initial_pid = affinity.get("initial_bds_pid")
    if isinstance(initial_pid, bool) or not isinstance(initial_pid, int) or initial_pid <= 0:
        errors.append("initial BDS process ID is missing from affinity evidence")
    elif isinstance(bds_pid, int) and initial_pid == bds_pid:
        errors.append("measurement BDS process was not fresh after bootstrap")
    initial_create_time = affinity.get("initial_bds_create_time")
    if (
        isinstance(initial_create_time, bool)
        or not isinstance(initial_create_time, (int, float))
        or not math.isfinite(float(initial_create_time))
    ):
        errors.append("initial managed root create-time is missing from affinity evidence")
    if (
        isinstance(identity, dict)
        and isinstance(identity.get("create_time"), (int, float))
        and abs(float(identity["create_time"]) - float(affinity.get("bds_create_time", float("nan")))) > 0.01
    ):
        errors.append("BDS affinity create-time does not match managed root identity")
    if isinstance(identity, dict) and isinstance(bds_pid, int) and identity.get("pid") != bds_pid:
        errors.append("managed root identity PID does not match BDS affinity evidence")
    _check_affinity_restoration(affinity, errors)
    return affinity


def _check_workload(result: dict[str, Any], errors: list[str]) -> dict[str, Any] | None:
    workload = result.get("workload")
    if not isinstance(workload, dict):
        errors.append("missing workload evidence")
        return None
    if workload.get("bot_count") != BOT_COUNT:
        errors.append(f"workload bot count mismatch: {workload.get('bot_count')!r}")
    if workload.get("scenario") != BOT_SCENARIO:
        errors.append(f"workload scenario mismatch: {workload.get('scenario')!r}")
    if workload.get("chunk_radius") != CHUNK_RADIUS:
        errors.append(f"workload chunk radius mismatch: {workload.get('chunk_radius')!r}")
    if workload.get("progress_counter_scope") != BOT_PROGRESS_COUNTER_SCOPE:
        errors.append("workload progress counter scope is missing or mismatched")
    online = workload.get("fleet_online")
    shutdown = workload.get("fleet_shutdown")
    if not isinstance(online, dict) or online.get("online") != BOT_COUNT or online.get("count") != BOT_COUNT:
        errors.append(f"fleet-online evidence mismatch: {online!r}")
    if (
        not isinstance(shutdown, dict)
        or shutdown.get("graceful_shutdown") is not True
        or shutdown.get("launched") != BOT_COUNT
        or shutdown.get("online") != BOT_COUNT
    ):
        errors.append(f"fleet-shutdown evidence is not graceful: {shutdown!r}")
    boundaries = workload.get("boundaries")
    required_boundaries = (
        "online",
        "warmup_start",
        "warmup_end",
        "measurement_start",
        "measurement_end",
        "before_disconnect",
    )
    if not isinstance(boundaries, dict):
        errors.append("workload boundary evidence is missing")
    else:
        boundary_times: list[int] = []
        for name in required_boundaries:
            boundary = boundaries.get(name)
            if not isinstance(boundary, dict):
                errors.append(f"workload boundary is missing: {name}")
                continue
            try:
                timestamp = int(boundary["monotonic_ns"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"workload boundary timestamp is malformed: {name}")
                continue
            if timestamp <= 0:
                errors.append(f"workload boundary timestamp is not positive: {name}")
            boundary_times.append(timestamp)
        if any(left >= right for left, right in pairwise(boundary_times)):
            errors.append("workload boundary timestamps are not increasing")
        progress_values: dict[str, dict[str, int]] = {}
        for name in ("warmup_start", "warmup_end", "measurement_start", "measurement_end"):
            boundary = boundaries.get(name)
            progress = boundary.get("progress_counters") if isinstance(boundary, dict) else None
            counters = progress.get("counters") if isinstance(progress, dict) else None
            if not isinstance(counters, dict):
                errors.append(f"progress counters are missing at workload boundary: {name}")
                continue
            if not isinstance(progress, dict) or progress.get("source_event") != "fleet_progress":
                errors.append(f"progress counters at workload boundary {name} are not from fleet_progress")
            source_index = progress.get("source_index") if isinstance(progress, dict) else None
            if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 0:
                errors.append(f"progress counter source index is malformed at workload boundary: {name}")
            event_count = boundary.get("event_count")
            if isinstance(event_count, bool) or not isinstance(event_count, int) or event_count < 1:
                errors.append(f"bot event count is malformed at workload boundary: {name}")
            elif isinstance(source_index, int) and source_index >= event_count:
                errors.append(f"progress counter source index is outside bot events at workload boundary: {name}")
            fleet_progress_events = boundary.get("fleet_progress_events")
            if (
                isinstance(fleet_progress_events, bool)
                or not isinstance(fleet_progress_events, int)
                or fleet_progress_events < 1
            ):
                errors.append(f"fleet_progress event count is missing at workload boundary: {name}")
            parsed: dict[str, int] = {}
            for key in PROGRESS_COUNTER_KEYS:
                value = counters.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"progress counter {key} is missing or non-numeric at {name}")
                    continue
                try:
                    integer = int(value)
                except (TypeError, ValueError, OverflowError):
                    errors.append(f"progress counter {key} is malformed at {name}")
                    continue
                if integer < 0 or float(value) != integer:
                    errors.append(f"progress counter {key} is negative or non-integral at {name}")
                    continue
                parsed[key] = integer
            if len(parsed) == len(PROGRESS_COUNTER_KEYS):
                progress_values[name] = parsed
        if len(progress_values) == 4:
            ordered = ("warmup_start", "warmup_end", "measurement_start", "measurement_end")
            for left, right in pairwise(ordered):
                for key in PROGRESS_COUNTER_KEYS:
                    if progress_values[right][key] < progress_values[left][key]:
                        errors.append(f"progress counter {key} moved backwards from {left} to {right}")
            deltas = workload.get("progress_window_deltas")
            if not isinstance(deltas, dict) or deltas.get("monotonic") is not True:
                errors.append("progress window deltas are missing or not monotonic")
            else:
                if deltas.get("counter_keys") != list(PROGRESS_COUNTER_KEYS):
                    errors.append("progress window delta counter keys are missing or mismatched")
                if deltas.get("scope") != BOT_PROGRESS_COUNTER_SCOPE:
                    errors.append("progress window delta scope is missing or mismatched")
                for window, start, end in (
                    ("warmup", "warmup_start", "warmup_end"),
                    ("measurement", "measurement_start", "measurement_end"),
                ):
                    observed = deltas.get(window)
                    if not isinstance(observed, dict):
                        errors.append(f"progress {window} deltas are missing")
                        continue
                    for key in PROGRESS_COUNTER_KEYS:
                        expected_delta = progress_values[end][key] - progress_values[start][key]
                        value = observed.get(key)
                        if value != expected_delta:
                            errors.append(
                                f"progress {window} delta mismatch for {key}: {value!r} != {expected_delta}"
                            )
    counters = workload.get("input_counters")
    if not isinstance(counters, dict):
        errors.append("workload input counters are missing")
    else:
        for key in INPUT_COUNTER_KEYS:
            value = counters.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"workload counter {key} is missing or non-numeric")
                continue
            try:
                integer = int(value)
            except (TypeError, ValueError, OverflowError):
                errors.append(f"workload counter {key} is malformed")
                continue
            if integer < 0 or float(value) != integer:
                errors.append(f"workload counter {key} is negative or non-integral: {value!r}")
        if isinstance(shutdown, dict):
            for key in INPUT_COUNTER_KEYS:
                if shutdown.get(key) != counters.get(key):
                    errors.append(f"workload counter {key} disagrees with fleet-shutdown evidence")
    return workload


def validate_case(
    result: dict[str, Any],
    case_path: pathlib.Path | None,
    *,
    expected_block: int,
    expected_position: int,
    expected_treatment: str,
    baseline_sha: str,
    candidate_sha: str,
    bot_ref: str,
    expected_scenario_sha256: str | None,
    world_snapshot_id: str | None,
) -> tuple[dict[str, float] | None, dict[str, Any]]:
    """Validate one case and return metrics plus a compact evidence report."""

    errors: list[str] = []
    protocol = _protocol(result)
    expected_id = case_id(expected_block, expected_position, expected_treatment)
    expected_mode, expected_revision = expected_treatment.split("-", 1)
    for key, expected in {
        "protocol_version": PROTOCOL_VERSION,
        "case_id": expected_id,
        "block_index": expected_block,
        "position": expected_position,
        "treatment": expected_treatment,
        "mode": expected_mode,
        "revision": expected_revision,
        "platform": "linux",
        "baseline_sha": baseline_sha,
        "candidate_sha": candidate_sha,
        "endstone_sha": ENDSTONE_SHA,
        "bot_ref": bot_ref,
        "bot_count": BOT_COUNT,
        "bot_scenario": BOT_SCENARIO,
        "hotspot_mode": HOTSPOT_MODE,
        "hotspot_iterations": HOTSPOT_ITERATIONS,
        "hotspot_iterations_rationale": HOTSPOT_ITERATIONS_RATIONALE,
        "warmup_seconds": WARMUP_SECONDS,
        "measurement_seconds": MEASUREMENT_SECONDS,
        "sample_interval_ms": SAMPLE_INTERVAL_MS,
        "cpu_metric_resolution_limit_percentage_points": CPU_METRIC_RESOLUTION_LIMIT_PERCENTAGE_POINTS,
        "affinity_poll_interval_seconds": AFFINITY_POLL_INTERVAL_SECONDS,
        "chunk_radius": CHUNK_RADIUS,
        "world_snapshot_id": WORLD_SNAPSHOT_ID,
        "bot_progress_counter_keys": list(PROGRESS_COUNTER_KEYS),
        "bot_progress_counter_scope": BOT_PROGRESS_COUNTER_SCOPE,
        "pep_event_scope": "full-profile-cumulative; not window-aligned",
        "affinity_model": "controlled-process CPU isolation; host and kernel work are not excluded",
        "measurement_process_scope": "managed Endstone/BDS root process (python -m endstone); descendants excluded",
        "managed_root_tid_scope": MANAGED_ROOT_TID_SCOPE,
    }.items():
        if protocol.get(key) != expected:
            errors.append(f"{expected_id}: protocol {key} mismatch: {protocol.get(key)!r} != {expected!r}")
    if result.get("status") != "PASS":
        errors.append(f"{expected_id}: controller status is {result.get('status')!r}")
    observed_sha = _metadata_spark_sha(result)
    expected_sha = baseline_sha if expected_treatment.endswith("-B") else candidate_sha
    if protocol.get("expected_spark_sha") != expected_sha:
        errors.append(
            f"{expected_id}: protocol expected Spark SHA mismatch: "
            f"{protocol.get('expected_spark_sha')!r} != {expected_sha!r}"
        )
    if observed_sha != expected_sha:
        errors.append(f"{expected_id}: Spark SHA mismatch: observed={observed_sha!r} expected={expected_sha!r}")
    endstone_metadata = _check_endstone_metadata(result, errors)
    bstats_config = _check_bstats_config(result, case_path, errors, expected_id)
    scenario = protocol.get("scenario")
    if (
        not isinstance(scenario, dict)
        or scenario.get("name") != BOT_SCENARIO
        or scenario.get("sha256") != BOT_SCENARIO_SHA256
        or scenario.get("steps") != 1
        or scenario.get("actions") != ["idle"]
        or scenario.get("indefinite_idle") is not True
        or scenario.get("bounded_area_policy") != STATIONARY_BOUNDED_AREA_POLICY
    ):
        errors.append(f"{expected_id}: scenario contract mismatch")
    elif expected_scenario_sha256 and scenario.get("sha256") != expected_scenario_sha256:
        errors.append(f"{expected_id}: scenario SHA drift")
    protocol_world = protocol.get("world") or protocol.get("world_snapshot")
    result_world = result.get("world") or result.get("world_snapshot")
    world = protocol_world if isinstance(protocol_world, dict) else result_world
    if not isinstance(world, dict):
        errors.append(f"{expected_id}: world evidence is missing")
        world = {}
    observed_world_id = world.get("snapshot_id")
    if observed_world_id != WORLD_SNAPSHOT_ID:
        errors.append(f"{expected_id}: world snapshot mismatch: {observed_world_id!r}")
    if world_snapshot_id is not None and observed_world_id != world_snapshot_id:
        errors.append(f"{expected_id}: world snapshot drift: {observed_world_id!r} != {world_snapshot_id!r}")
    if isinstance(world, dict):
        expected_world = {
            "level_type": "FLAT",
            "level_seed": "8675309",
            "view_distance": CHUNK_RADIUS,
            "tick_distance": 4,
            "world_recreated": True,
        }
        for key, expected in expected_world.items():
            if world.get(key) != expected:
                errors.append(f"{expected_id}: world {key} mismatch: {world.get(key)!r} != {expected!r}")

    windows = _check_window(result, errors)
    affinity = _check_affinity(result, errors)
    workload = _check_workload(result, errors)
    metrics: dict[str, float] | None = None
    if not errors:
        try:
            metrics = _metric_values(result)
        except EvidenceError as exc:
            errors.append(f"{expected_id}: {exc}")
    if metrics is not None:
        performance = _performance(result)
        tick = performance.get("tick_statistics") or {}
        try:
            sample_count = int(tick.get("samples", 0))
        except (TypeError, ValueError):
            sample_count = 0
        if sample_count < 100:
            errors.append(f"{expected_id}: too few tick samples: {tick.get('samples')!r}")
        try:
            tick_count = int(performance.get("ticks", 0))
        except (TypeError, ValueError):
            tick_count = 0
        if tick_count <= 0:
            errors.append(f"{expected_id}: non-positive tick count")
        try:
            wall_seconds = float(performance.get("wall_seconds", 0.0))
        except (TypeError, ValueError):
            wall_seconds = 0.0
        if wall_seconds <= 0.0:
            errors.append(f"{expected_id}: non-positive wall duration")

    mode = expected_treatment.split("-", 1)[0]
    pep_events: dict[str, int | None] | None = None
    if mode == "full":
        performance_value = result.get("performance")
        performance = performance_value if isinstance(performance_value, dict) else {}
        pep_events = performance.get("pep_events")
        if not isinstance(pep_events, dict):
            summary = performance.get("profile_summary")
            pep_events = extract_pep_events(summary if isinstance(summary, dict) else None)
        try:
            pep_report = validate_pep_events(pep_events, require_events=True)
        except RuntimeError as exc:
            errors.append(f"{expected_id}: {exc}")
            pep_report = {"required_zero": {}, "reported": {}}
        pep_window = performance.get("pep_event_window")
        if not isinstance(pep_window, dict) or pep_window.get("scope") != "full-profile-cumulative; not window-aligned":
            errors.append(f"{expected_id}: PEP event scope is not declared cumulative and non-window-aligned")
        viewer_url = performance.get("viewer_url")
        if not isinstance(viewer_url, str) or not viewer_url.startswith("https://spark.lucko.me/"):
            errors.append(f"{expected_id}: full case has no Spark viewer URL")
        profile_name = performance.get("profile_file") or performance.get("profile_path")
        if case_path is None or not isinstance(profile_name, str) or not (case_path.parent / profile_name).is_file():
            errors.append(f"{expected_id}: full profile file was not preserved")
        else:
            profile_path = case_path.parent / profile_name
            try:
                profile_bytes = int(performance["profile_file_bytes"])
                actual_bytes = profile_path.stat().st_size
                if profile_bytes <= 0:
                    errors.append(f"{expected_id}: full profile file is empty")
                elif profile_bytes != actual_bytes:
                    errors.append(
                        f"{expected_id}: full profile byte count mismatch: {profile_bytes} != {actual_bytes}"
                    )
            except (KeyError, OSError, TypeError, ValueError):
                errors.append(f"{expected_id}: full profile byte count is malformed")
            profile_sha = performance.get("profile_file_sha256")
            if not isinstance(profile_sha, str) or len(profile_sha) != 64:
                errors.append(f"{expected_id}: full profile SHA-256 is missing")
            else:
                try:
                    actual_sha = hashlib.sha256(profile_path.read_bytes()).hexdigest()
                except OSError as exc:
                    errors.append(f"{expected_id}: full profile cannot be read: {exc}")
                else:
                    if actual_sha != profile_sha.lower():
                        errors.append(f"{expected_id}: full profile SHA-256 mismatch")
    else:
        pep_report = {"required_zero": {}, "reported": {}}

    pep_event_rate: float | None = None
    if pep_events:
        profile_summary = performance.get("profile_summary") if isinstance(performance, dict) else None
        if isinstance(profile_summary, dict):
            try:
                duration = float(profile_summary["duration_seconds"])
                if duration > 0.0 and pep_events.get("py_start") is not None:
                    pep_event_rate = float(pep_events["py_start"]) / duration
            except (KeyError, TypeError, ValueError):
                pass

    if errors:
        metrics = None
    report = {
        "case_id": expected_id,
        "block_index": expected_block,
        "position": expected_position,
        "treatment": expected_treatment,
        "status": result.get("status"),
        "valid": not errors,
        "errors": errors,
        "metrics": metrics,
        "counter_windows": windows,
        "affinity": affinity,
        "workload": workload,
        "pep_events": pep_events,
        "pep_diagnostics": pep_report,
        "pep_event_rate_per_second": pep_event_rate,
        "endstone_metadata": endstone_metadata,
        "bstats_config": bstats_config,
    }
    return metrics, report


def _verify_evidence_manifest(manifest_path: pathlib.Path, errors: list[str]) -> None:
    if manifest_path.is_symlink():
        errors.append(f"evidence manifest is a symlink: {manifest_path}")
        return
    block_dir = manifest_path.parent
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"unable to parse evidence manifest {manifest_path}: {exc}")
        return
    if not isinstance(payload, dict):
        errors.append(f"evidence manifest is not an object: {manifest_path}")
        return
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"evidence manifest protocol mismatch: {manifest_path}")
    if payload.get("max_file_bytes") != MAX_EVIDENCE_FILE_BYTES:
        errors.append(f"evidence manifest file-size limit mismatch: {manifest_path}")
    if payload.get("max_total_bytes") != MAX_EVIDENCE_BYTES:
        errors.append(f"evidence manifest total-size limit mismatch: {manifest_path}")
    if payload.get("runtime_payload_dirs_pruned") != list(RUNTIME_PAYLOAD_DIRS):
        errors.append(f"evidence manifest runtime payload declaration mismatch: {manifest_path}")
    entries = payload.get("files")
    if not isinstance(entries, list):
        errors.append(f"evidence manifest file list is missing: {manifest_path}")
        return
    expected_count = payload.get("allowed_file_count")
    total_bytes = payload.get("total_bytes")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count != len(entries):
        errors.append(f"evidence manifest file count is malformed: {manifest_path}")
    if isinstance(total_bytes, bool) or not isinstance(total_bytes, int) or total_bytes < 0:
        errors.append(f"evidence manifest total byte count is malformed: {manifest_path}")
        total_bytes = -1
    if isinstance(total_bytes, int) and total_bytes > MAX_EVIDENCE_BYTES:
        errors.append(f"evidence manifest exceeds total byte limit: {manifest_path}")
    listed_paths: set[str] = set()
    listed_total = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"evidence manifest entry {index} is malformed: {manifest_path}")
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
            errors.append(f"evidence manifest entry {index} path is malformed: {manifest_path}")
            continue
        relative = pathlib.PurePosixPath(raw_path)
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            errors.append(f"evidence manifest entry {index} escapes its block: {manifest_path}")
            continue
        normalized_path = relative.as_posix()
        if normalized_path in listed_paths or normalized_path == EVIDENCE_MANIFEST_NAME:
            errors.append(f"evidence manifest contains duplicate or self-referential path: {manifest_path}")
            continue
        listed_paths.add(normalized_path)
        size = entry.get("bytes")
        digest = entry.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_EVIDENCE_FILE_BYTES:
            errors.append(f"evidence manifest entry {normalized_path} has an invalid size")
            continue
        if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            errors.append(f"evidence manifest entry {normalized_path} has an invalid SHA-256")
            continue
        listed_total += size
        target = block_dir.joinpath(*relative.parts)
        try:
            contained = target.resolve().relative_to(block_dir.resolve())
        except (OSError, ValueError):
            errors.append(f"evidence manifest entry {normalized_path} escapes its block")
            continue
        if contained != pathlib.Path(*relative.parts):
            errors.append(f"evidence manifest entry {normalized_path} is not contained in its block")
            continue
        if target.is_symlink() or not target.is_file():
            errors.append(f"evidence manifest entry is missing or symlinked: {manifest_path.parent.name}/{normalized_path}")
            continue
        try:
            actual_size = target.stat().st_size
            actual_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f"evidence manifest entry cannot be read: {manifest_path.parent.name}/{normalized_path}: {exc}")
            continue
        if actual_size != size:
            errors.append(f"evidence manifest byte mismatch for {manifest_path.parent.name}/{normalized_path}")
        if actual_digest != digest:
            errors.append(f"evidence manifest SHA-256 mismatch for {manifest_path.parent.name}/{normalized_path}")
    if isinstance(total_bytes, int) and total_bytes != listed_total:
        errors.append(f"evidence manifest total does not equal listed files: {manifest_path}")
    actual_paths: set[str] = set()
    try:
        for path in block_dir.rglob("*"):
            relative = path.relative_to(block_dir)
            if path.is_symlink():
                errors.append(f"evidence tree contains a symlink: {manifest_path.parent.name}/{relative.as_posix()}")
            elif path.is_file():
                normalized_path = relative.as_posix()
                if normalized_path != EVIDENCE_MANIFEST_NAME:
                    actual_paths.add(normalized_path)
    except OSError as exc:
        errors.append(f"unable to inspect evidence tree for {manifest_path}: {exc}")
    for missing in sorted(actual_paths - listed_paths):
        errors.append(f"evidence manifest omits file: {manifest_path.parent.name}/{missing}")
    for treatment in TREATMENTS:
        for directory in RUNTIME_PAYLOAD_DIRS:
            payload_dir = block_dir / treatment / directory
            if payload_dir.exists() or payload_dir.is_symlink():
                errors.append(f"evidence runtime payload was not pruned: {manifest_path.parent.name}/{treatment}/{directory}")


def _verify_evidence_manifests(
    roots: abc.Iterable[pathlib.Path], *, expected_blocks: abc.Iterable[int]
) -> int:
    errors: list[str] = []
    expected = set(expected_blocks)
    manifests_by_block: dict[int, pathlib.Path] = {}
    candidates: list[pathlib.Path] = []
    for root in roots:
        if any(part.startswith(REJECTED_ARTIFACT_PREFIX) for part in root.parts):
            continue
        if root.is_file():
            if root.name == EVIDENCE_MANIFEST_NAME:
                candidates.append(root)
        elif root.is_dir():
            candidates.extend(
                path
                for path in sorted(root.rglob(EVIDENCE_MANIFEST_NAME))
                if not any(part.startswith(REJECTED_ARTIFACT_PREFIX) for part in path.parts)
            )
        else:
            continue
    if not candidates:
        errors.append("no evidence manifests found; exactly one is required per expected block")
    for manifest_path in candidates:
        parent_name = manifest_path.parent.name
        if not re.fullmatch(r"block-[0-9]{2}", parent_name):
            errors.append(f"evidence manifest is not inside a block directory: {manifest_path}")
            continue
        block_index = int(parent_name.removeprefix("block-"))
        if block_index not in expected:
            errors.append(f"evidence manifest is outside expected evaluated blocks: {manifest_path}")
            continue
        if block_index in manifests_by_block:
            errors.append(
                f"multiple evidence manifests found for block {block_index:02d}: "
                f"{manifests_by_block[block_index]} and {manifest_path}"
            )
            continue
        manifests_by_block[block_index] = manifest_path
    for block_index in sorted(expected - set(manifests_by_block)):
        errors.append(f"evidence manifest is missing for expected block {block_index:02d}")
    for manifest_path in manifests_by_block.values():
        _verify_evidence_manifest(manifest_path, errors)
    if errors:
        raise EvidenceError("; ".join(errors))
    return len(manifests_by_block)


def _iter_case_files(roots: abc.Iterable[pathlib.Path]) -> list[tuple[pathlib.Path, dict[str, Any]]]:
    files: list[tuple[pathlib.Path, dict[str, Any]]] = []
    for root in roots:
        if not root.exists():
            raise EvidenceError(f"evidence root does not exist: {root}")
        if any(part.startswith(REJECTED_ARTIFACT_PREFIX) for part in root.parts):
            continue
        candidates = (
            [root]
            if root.is_file()
            else [
                path
                for path in sorted(root.rglob("candidate-a-blocked-result.json"))
                if not any(part.startswith(REJECTED_ARTIFACT_PREFIX) for part in path.parts)
            ]
        )
        for path in candidates:
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EvidenceError(f"unable to parse evidence {path}: {exc}") from exc
            if not isinstance(payload, dict):
                raise EvidenceError(f"case evidence is not an object: {path}")
            files.append((path, payload))
    return files


def _workload_balance(reports: list[dict[str, Any]]) -> dict[str, Any]:
    names = tuple(INPUT_COUNTER_KEYS) + tuple(f"measurement_delta_{key}" for key in PROGRESS_COUNTER_KEYS)
    values: dict[str, list[float]] = {name: [] for name in names}
    pep_rates: list[float] = []
    for report in reports:
        counters = (report.get("workload") or {}).get("input_counters") or {}
        deltas = (report.get("workload") or {}).get("progress_window_deltas") or {}
        measurement_deltas = deltas.get("measurement") if isinstance(deltas, dict) else {}
        for name in INPUT_COUNTER_KEYS:
            try:
                value = float(counters[name])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                values[name].append(value)
        for key in PROGRESS_COUNTER_KEYS:
            try:
                value = float(measurement_deltas[key])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                values[f"measurement_delta_{key}"].append(value)
        rate = report.get("pep_event_rate_per_second")
        if isinstance(rate, (int, float)) and math.isfinite(float(rate)) and float(rate) > 0:
            pep_rates.append(float(rate))
    summaries = {name: summarize(items) for name, items in values.items()}
    errors: list[str] = []
    for name, items in values.items():
        if len(items) != len(reports):
            errors.append(f"workload counter {name} is missing or invalid in one or more cases")
            continue
        median = statistics.median(items)
        if max(items) - min(items) > max(1.0, median * 0.50):
            errors.append(f"workload counter {name} varies by more than 50 percent")
    if pep_rates:
        median = statistics.median(pep_rates)
        if max(pep_rates) - min(pep_rates) > max(1.0, median * 0.50):
            errors.append("PEP PY_START event rate varies by more than 50 percent")
    return {
        "checks_pass": not errors,
        "errors": errors,
        "counters": summaries,
        "pep_event_rate_per_second": summarize(pep_rates),
        "used_as_regression_covariate": False,
        "role": "balance check only",
    }


def _did_by_block(
    case_metrics: dict[tuple[int, str], dict[str, float]], metric: str, blocks: list[int]
) -> dict[int, float]:
    values: dict[int, float] = {}
    for block in blocks:
        try:
            off_b = case_metrics[(block, "off-B")][metric]
            off_c = case_metrics[(block, "off-C")][metric]
            full_b = case_metrics[(block, "full-B")][metric]
            full_c = case_metrics[(block, "full-C")][metric]
        except KeyError:
            continue
        values[block] = (full_c - full_b) - (off_c - off_b)
    return values


def _did_values(case_metrics: dict[tuple[int, str], dict[str, float]], metric: str, blocks: list[int]) -> list[float]:
    by_block = _did_by_block(case_metrics, metric, blocks)
    return [by_block[block] for block in blocks if block in by_block]


def analyze_evidence(
    evidence_roots: abc.Iterable[pathlib.Path | str],
    *,
    start_block: int,
    batch_size: int = BLOCK_SIZE,
    baseline_sha: str = BASELINE_SHA,
    candidate_sha: str = CANDIDATE_SHA,
    bot_ref: str = BOT_REF,
    max_blocks: int = MAX_BLOCKS,
) -> dict[str, Any]:
    """Validate and summarize one cumulative batch (up to five runs)."""

    errors: list[str] = []
    roots = [pathlib.Path(root).resolve() for root in evidence_roots]
    if not roots:
        roots = [pathlib.Path("evidence").resolve()]
    if len(roots) > 5:
        errors.append(f"at most five batch evidence roots may be combined: {len(roots)}")
    try:
        baseline_sha = validate_sha(baseline_sha, BASELINE_SHA, "baseline_sha")
        candidate_sha = validate_sha(candidate_sha, CANDIDATE_SHA, "candidate_sha")
        bot_ref = validate_sha(bot_ref, BOT_REF, "bot_ref")
        batch_schedule(start_block, batch_size)
    except (TypeError, ValueError) as exc:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "valid": False,
            "outcome": "MAX_INCONCLUSIVE",
            "errors": [f"configuration: {type(exc).__name__}: {exc}"],
            "primary_estimand": "within-block-DID",
            "primary_metric": "process_cpu_percent_of_one_core",
        }
    end_block = start_block + batch_size - 1
    expected_blocks = tuple(range(1, end_block + 1))
    if end_block > max_blocks:
        errors.append(f"batch end block {end_block} exceeds maximum {max_blocks}")
    if start_block not in LEGAL_START_BLOCKS:
        errors.append(f"start block {start_block} is not one of {LEGAL_START_BLOCKS}")

    try:
        evidence_manifest_count = _verify_evidence_manifests(roots, expected_blocks=expected_blocks)
    except EvidenceError as exc:
        evidence_manifest_count = 0
        errors.append(str(exc))
    try:
        files = _iter_case_files(roots)
    except EvidenceError as exc:
        files = []
        errors.append(str(exc))
    by_id: dict[str, tuple[pathlib.Path, dict[str, Any]]] = {}
    for path, result in files:
        try:
            identifier = str(_protocol(result).get("case_id", ""))
        except EvidenceError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if not identifier:
            errors.append(f"{path}: missing case id")
        elif identifier in by_id:
            errors.append(f"duplicate case evidence: {identifier} ({by_id[identifier][0]} and {path})")
        else:
            by_id[identifier] = (path, result)

    expected_ids = {
        case_id(block, position, treatment)
        for block in range(1, end_block + 1)
        for position, treatment in enumerate(block_schedule(block))
    }
    actual_ids = set(by_id)
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    if missing:
        errors.append(f"missing cases: {missing}")
    if extra:
        errors.append(f"unexpected cases: {extra}")

    reports: list[dict[str, Any]] = []
    metrics_by_case: dict[tuple[int, str], dict[str, float]] = {}
    scenario_sha: str | None = None
    world_id: str | None = None
    endstone_signatures: set[str] = set()
    topology_signatures: set[str] = set()
    expected_cases = []
    for block in range(1, end_block + 1):
        for position, treatment in enumerate(block_schedule(block)):
            expected_cases.append((block, position, treatment))
    for block, position, treatment in expected_cases:
        identifier = case_id(block, position, treatment)
        if identifier not in by_id:
            continue
        path, result = by_id[identifier]
        protocol = result.get("protocol") or {}
        scenario = protocol.get("scenario") if isinstance(protocol, dict) else None
        candidate_scenario_sha = scenario.get("sha256") if isinstance(scenario, dict) else None
        if scenario_sha is None and candidate_scenario_sha:
            scenario_sha = str(candidate_scenario_sha)
        elif candidate_scenario_sha and candidate_scenario_sha != scenario_sha:
            errors.append(f"{identifier}: scenario SHA differs across cases")
        try:
            report_metrics, report = validate_case(
                result,
                path,
                expected_block=block,
                expected_position=position,
                expected_treatment=treatment,
                baseline_sha=baseline_sha,
                candidate_sha=candidate_sha,
                bot_ref=bot_ref,
                expected_scenario_sha256=scenario_sha,
                world_snapshot_id=world_id,
            )
        except EvidenceError as exc:
            report_metrics = None
            report = {
                "case_id": identifier,
                "block_index": block,
                "position": position,
                "treatment": treatment,
                "valid": False,
                "errors": [str(exc)],
                "metrics": None,
                "workload": None,
            }
        result_world = result.get("world")
        protocol_world = protocol.get("world") if isinstance(protocol, dict) else None
        world = result_world if isinstance(result_world, dict) else protocol_world or {}
        candidate_world_id = world.get("snapshot_id") if isinstance(world, dict) else None
        if world_id is None and candidate_world_id:
            world_id = str(candidate_world_id)
        elif candidate_world_id and candidate_world_id != world_id:
            errors.append(f"{identifier}: world snapshot differs across cases")
        reports.append(report)
        endstone_metadata = report.get("endstone_metadata")
        if isinstance(endstone_metadata, dict):
            endstone_signatures.add(json.dumps(endstone_metadata, sort_keys=True, separators=(",", ":")))
        topology_signature = _topology_signature(report.get("affinity"))
        if topology_signature is not None:
            topology_signatures.add(topology_signature)
        if report_metrics is not None:
            metrics_by_case[(block, treatment)] = report_metrics
        errors.extend(report.get("errors") or [])

    if len(endstone_signatures) != 1:
        errors.append("Endstone artifact metadata is missing or drifts across the cumulative experiment")
    if len(topology_signatures) > 1:
        errors.append("runner CPU topology differs across cases or blocks")
    workload_balance = _workload_balance(reports)
    errors.extend(workload_balance["errors"])
    did: dict[str, dict[str, Any]] = {}
    for metric in ("cpu_percent_of_one_core", "cpu_ms_per_tick", "mspt_mean"):
        blocks = list(range(1, end_block + 1))
        values_by_block = _did_by_block(metrics_by_case, metric, blocks)
        values = [values_by_block[block] for block in blocks if block in values_by_block]
        item = summarize(values)
        item["values"] = values
        item["by_block"] = {str(block): value for block, value in values_by_block.items()}
        item["confirmatory_ci"] = sequential_ci(values)
        did[metric] = item
    primary = did["cpu_percent_of_one_core"]
    descriptive_half_width = primary.get("ci95_half_width")
    valid = not errors and len(primary["values"]) == end_block
    confirmatory = primary["confirmatory_ci"]
    outcome, stop_reason = sequential_decision(
        confirmatory,
        valid=valid,
        end_block=end_block,
        max_blocks=max_blocks,
    )

    ci_crosses_zero = (
        primary.get("mean") is not None
        and descriptive_half_width is not None
        and primary["mean"] - descriptive_half_width <= 0.0 <= primary["mean"] + descriptive_half_width
    )
    per_treatment: dict[str, dict[str, Any]] = {}
    for treatment in TREATMENTS:
        per_treatment[treatment] = {}
        for metric in ("cpu_percent_of_one_core", "cpu_ms_per_tick", "mspt_mean"):
            values = [
                metrics_by_case[(block, treatment)][metric]
                for block in range(1, end_block + 1)
                if (block, treatment) in metrics_by_case
            ]
            per_treatment[treatment][metric] = summarize(values)

    return {
        "protocol_version": PROTOCOL_VERSION,
        "valid": valid,
        "errors": sorted(set(errors)),
        "outcome": outcome,
        "stop_reason": stop_reason,
        "start_block": start_block,
        "end_block": end_block,
        "blocks_evaluated": list(range(1, end_block + 1)),
        "batch_size": batch_size,
        "case_count": len(expected_ids),
        "observed_case_count": len(actual_ids),
        "evidence_manifest_count": evidence_manifest_count,
        "schedule": {
            str(block): list(block_schedule(block)) for block in range(1, end_block + 1)
        },
        "primary_estimand": "within-block-DID",
        "primary_formula": "(full-C - full-B) - (off-C - off-B)",
        "primary_metric": "process_cpu_percent_of_one_core",
        "ci_confidence": 0.95,
        "ci_role": "descriptive_only",
        "ci_half_width_limit_percentage_points": CPU_CI_HALF_WIDTH_LIMIT,
        "confirmatory_confidence": SEQUENTIAL_CONFIDENCE,
        "confirmatory_familywise_confidence": SEQUENTIAL_FAMILYWISE_CONFIDENCE,
        "confirmatory_method": "two-sided Student-t intervals at five cumulative looks with Bonferroni adjustment",
        "confirmatory_critical_values": {str(n): critical for n, critical in _T_995.items()},
        "sequential_looks": list(SEQUENTIAL_LOOKS),
        "sequential_inference": {
            "marginal_confidence": SEQUENTIAL_CONFIDENCE,
            "familywise_confidence": SEQUENTIAL_FAMILYWISE_CONFIDENCE,
            "method": "Bonferroni over cumulative looks n=4,8,12,16,20",
            "critical_values": {str(n): critical for n, critical in _T_995.items()},
            "precision_target_percentage_points": CPU_CI_HALF_WIDTH_LIMIT,
            "interval": confirmatory,
            "decision": outcome,
        },
        "significance_test_used": False,
        "ci_crosses_zero": ci_crosses_zero,
        "did": did,
        "per_treatment": per_treatment,
        "workload_balance": workload_balance,
        "correctness_checks": {
            "all_case_checks_pass": not errors,
            "endstone_sha": ENDSTONE_SHA,
            "endstone_metadata_consistent": len(endstone_signatures) == 1,
            "endstone_metadata_signatures": sorted(endstone_signatures),
            "topology_consistent": len(topology_signatures) == 1,
            "topology_signatures": sorted(topology_signatures),
            "scenario_sha256": scenario_sha,
            "world_snapshot_id": world_id or WORLD_SNAPSHOT_ID,
            "native_boundary_misses": sorted(
                {
                    report.get("pep_diagnostics", {}).get("reported", {}).get("native_boundary_misses")
                    for report in reports
                    if report.get("pep_diagnostics")
                }
                - {None}
            ),
            "snapshot_failures": sorted(
                {
                    report.get("pep_diagnostics", {}).get("reported", {}).get("snapshot_failures")
                    for report in reports
                    if report.get("pep_diagnostics")
                }
                - {None}
            ),
            "thread_mismatches": sorted(
                {
                    report.get("pep_diagnostics", {}).get("reported", {}).get("thread_mismatches")
                    for report in reports
                    if report.get("pep_diagnostics")
                }
                - {None}
            ),
        },
        "cases": reports,
    }


def _markdown(summary: dict[str, Any]) -> str:
    primary = summary.get("did", {}).get("cpu_percent_of_one_core", {})
    return "\n".join(
        [
            "# Candidate A blocked benchmark",
            "",
            f"Outcome: **{summary.get('outcome')}**",
            f"Blocks: {summary.get('start_block')}–{summary.get('end_block')}",
            f"Valid: `{summary.get('valid')}`",
            f"CPU DID mean: `{primary.get('mean')}` percentage points",
            f"CPU DID descriptive 95% CI half-width: `{primary.get('ci95_half_width')}` percentage points",
            f"CPU DID confirmatory 99% CI: `{primary.get('confirmatory_ci')}`",
            f"Descriptive 95% CI crosses zero: `{summary.get('ci_crosses_zero')}`",
            "",
            summary.get("stop_reason", ""),
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", action="append", dest="evidence_roots", default=None)
    parser.add_argument("--start-block", required=True, type=int)
    parser.add_argument("--batch-size", type=int, default=BLOCK_SIZE, choices=[BLOCK_SIZE])
    parser.add_argument("--baseline-sha", default=BASELINE_SHA)
    parser.add_argument("--candidate-sha", default=CANDIDATE_SHA)
    parser.add_argument("--bot-ref", default=BOT_REF)
    parser.add_argument("--output", default="candidate-a-blocked-summary.json")
    parser.add_argument("--markdown-output", default="candidate-a-blocked-summary.md")
    args = parser.parse_args()
    roots = [pathlib.Path(path) for path in (args.evidence_roots or ["evidence"])]
    summary = analyze_evidence(
        roots,
        start_block=args.start_block,
        batch_size=args.batch_size,
        baseline_sha=args.baseline_sha,
        candidate_sha=args.candidate_sha,
        bot_ref=args.bot_ref,
    )
    pathlib.Path(args.output).write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    pathlib.Path(args.markdown_output).write_text(_markdown(summary), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not summary.get("valid"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
