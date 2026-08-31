#!/usr/bin/env python3
"""Run and validate one Linux real-BDS Spark profiler mode.

The workflow fans this controller out over the fixed mode matrix.  Each
invocation owns a fresh case directory, so a profile session never shares a
server process or generated payload with another mode.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import pathlib
import re
import time
import traceback
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from controller.bot_validation import (
    BotProcess,
    patch_server_properties,
    wait_player_state,
)
from controller.python_profile_payload import (
    ProfilePayload,
    fetch_viewer_payload,
    iter_leaf_paths,
    parse_sampler_data,
    profile_summary,
)
from controller.run_test import VIEWER_RE, IntegrationTest, now_iso, write_json

DEFAULT_PROFILE_SECONDS = 15
DEFAULT_WARMUP_SECONDS = 10
MIN_PROFILE_SECONDS = 11
MAX_PROFILE_SECONDS = 300
MAX_WARMUP_SECONDS = 300
ONLY_TICKS_OVER_MS = 10
DEFAULT_ALLOCATION_INTERVAL_BYTES = 524287
SHA256_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SPARK_VERSION_RE = re.compile(r"(?:endstone-spark|spark)\s+v?([0-9][0-9A-Za-z.+_-]*)", re.IGNORECASE)


@dataclass(frozen=True)
class ProfilerModeSpec:
    """Frozen command and payload contract for one profiler mode."""

    name: str
    flags: tuple[str, ...]
    sampler_mode: int
    interval: int
    all_threads: bool
    ticked: bool
    tick_threshold_us: int
    allocation: bool
    live_only: bool


PROFILER_MODES: tuple[ProfilerModeSpec, ...] = (
    ProfilerModeSpec("default", (), 0, 4000, False, False, 0, False, False),
    ProfilerModeSpec("1ms", ("--interval", "1"), 0, 1000, False, False, 0, False, False),
    ProfilerModeSpec("all-thread", ("--thread", "*"), 0, 4000, True, False, 0, False, False),
    ProfilerModeSpec(
        "only-ticks-over",
        ("--only-ticks-over", str(ONLY_TICKS_OVER_MS)),
        0,
        4000,
        False,
        True,
        ONLY_TICKS_OVER_MS * 1000,
        False,
        False,
    ),
    ProfilerModeSpec("allocation", ("--alloc",), 1, DEFAULT_ALLOCATION_INTERVAL_BYTES, True, False, 0, True, False),
    ProfilerModeSpec(
        "alloc-live-only", ("--alloc-live-only",), 1, DEFAULT_ALLOCATION_INTERVAL_BYTES, True, False, 0, True, True
    ),
)
MODE_BY_NAME = {spec.name: spec for spec in PROFILER_MODES}
MODE_ALIASES = {
    "interval-1ms": "1ms",
    "one-ms": "1ms",
    "all_threads": "all-thread",
    "only_ticks_over": "only-ticks-over",
    "alloc": "allocation",
    "live": "alloc-live-only",
}

# BotProcess owns the actual launch implementation.  Keep this list beside
# the result contract so the workload options remain explicit evidence.
BOT_COMMAND_OPTIONS = (
    "--host",
    "127.0.0.1",
    "--port",
    "19132",
    "--name",
    "TestBot",
    "--chunk-radius",
    "8",
    "--connect-timeout",
    "15s",
    "--spawn-timeout",
    "30s",
    "--json",
)

EXECUTION_DIAGNOSTICS = (
    "Execution samples dropped",
    "Execution queue samples dropped",
    "Execution pending samples dropped",
    "Execution profile samples dropped",
    "Execution tick events dropped",
    "Execution sample queue capacity",
    "Execution tick event capacity",
    "Execution module entries",
    "Execution module capacity",
    "Execution module overflow frames",
    "Execution sampled thread roots",
    "Execution thread root capacity",
    "Execution overflow thread samples",
    "Execution pending sample capacity",
    "Execution profile node capacity",
    "Execution profile time entry capacity",
    "Execution profile storage exhausted",
    "Execution retained history windows",
    "Execution history samples pruned",
    "Execution history truncated",
    "Execution data incomplete",
)

ALLOCATION_DIAGNOSTICS = (
    "Allocation backend",
    "Allocation coverage",
    "Allocation hook calls (process-wide)",
    "Allocation successful allocation calls (process-wide)",
    "Allocation sampling points hit (process-wide)",
    "Allocation profile samples accepted",
    "Allocation samples excluded by thread selector",
    "Allocation thread name lookup failures",
    "Allocation thread identity cache drops",
    "Allocation samples dropped",
    "Allocation sample events dropped",
    "Allocation tick events dropped",
    "Allocation tick event capacity",
    "Allocation sample events enqueued",
    "Allocation event queue high-water mark",
    "Allocation event queue capacity",
    "Allocation tracked sampled frees (process-wide)",
    "Allocation tracked sampled freed bytes (process-wide)",
    "Allocation tracked live allocations (process-wide)",
    "Allocation tracked live bytes (process-wide)",
    "Allocation tracked live peak (process-wide)",
    "Allocation live index capacity",
    "Allocation sampled thread roots",
    "Allocation thread root capacity",
    "Allocation overflow threads",
    "Allocation thread state drops",
    "Allocation hooked modules",
    "Allocation attributed module entries",
    "Allocation attributed module capacity",
    "Allocation profile node capacity",
    "Allocation profile time-entry capacity",
    "Allocation profile storage sample drops",
    "Allocation profile storage exhausted",
    "Allocation pending samples dropped",
    "Allocation pending sample capacity",
    "Allocation pending capacity drops",
    "Allocation pending stale drops",
    "Allocation pending final drops",
    "Allocation module overflow frames",
    "Allocation retained history windows",
    "Allocation history samples pruned",
    "Allocation history bytes pruned",
    "Allocation history truncated",
    "Allocation skipped modules",
    "Allocation failed modules",
    "Allocation data incomplete",
    "Allocation average tracked lifetime ms (process-wide)",
    "Allocation maximum tracked lifetime ms (process-wide)",
    "Allocation lifecycle records dropped",
    "Allocation lock contention records dropped",
    "Allocation profile sampled bytes",
    "Allocation observed request bytes (process-wide)",
    "Allocation interval bytes",
    "Allocation live-only",
    "Allocation thread filter stage",
    "Allocation thread selection",
    "Allocation hook entry points total",
    "Allocation hook entry points covered",
    "Allocation hook targets installed",
    "Allocation hook aliases",
    "Allocation hook capabilities",
)

ALLOCATION_LIVE_DIAGNOSTICS = (
    "Allocation analysis",
    "Allocation retained average age ms",
    "Allocation retained maximum age ms",
)


class ProfileValidationError(ValueError):
    """Raised when raw profile bytes do not satisfy the mode contract."""


def get_mode_spec(mode: str) -> ProfilerModeSpec:
    canonical = MODE_ALIASES.get(mode, mode)
    try:
        return MODE_BY_NAME[canonical]
    except KeyError as exc:
        choices = ", ".join(spec.name for spec in PROFILER_MODES)
        raise ValueError(f"unknown profiler mode {mode!r}; expected one of: {choices}") from exc


def _bounded_duration(value: int, *, label: str, maximum: int) -> int:
    if value < (MIN_PROFILE_SECONDS if label == "profile_seconds" else 0) or value > maximum:
        if label == "profile_seconds":
            raise ValueError(f"profile_seconds must be between {MIN_PROFILE_SECONDS} and {maximum}")
        raise ValueError(f"{label} must be between 0 and {maximum}")
    return value


def build_profiler_command(mode: str, profile_seconds: int = DEFAULT_PROFILE_SECONDS) -> str:
    """Build the exact console command for one frozen mode."""

    spec = get_mode_spec(mode)
    seconds = _bounded_duration(profile_seconds, label="profile_seconds", maximum=MAX_PROFILE_SECONDS)
    tokens = ["spark", "profiler", "start", "--timeout", str(seconds), *spec.flags]
    return " ".join(tokens)


def _require_nonnegative_int(value: str, key: str) -> int:
    raw = str(value).strip()
    if not re.fullmatch(r"[0-9]+", raw):
        raise ProfileValidationError(f"diagnostic {key!r} is not a non-negative integer: {value!r}")
    return int(raw)


def _require_nonnegative_float(value: str, key: str) -> float:
    try:
        parsed = float(str(value).strip())
    except ValueError as exc:
        raise ProfileValidationError(f"diagnostic {key!r} is not a number: {value!r}") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ProfileValidationError(f"diagnostic {key!r} is not finite and non-negative: {value!r}")
    return parsed


def _require_bool(value: str, key: str) -> bool:
    raw = str(value).strip().lower()
    if raw not in {"true", "false"}:
        raise ProfileValidationError(f"diagnostic {key!r} is not true/false: {value!r}")
    return raw == "true"


def _diagnostic_summary(profile: ProfilePayload, spec: ProfilerModeSpec) -> dict[str, Any]:
    diagnostics = profile.extra_metadata
    required = ALLOCATION_DIAGNOSTICS if spec.allocation else EXECUTION_DIAGNOSTICS
    missing = [key for key in required if key not in diagnostics]
    if missing:
        raise ProfileValidationError(f"missing required diagnostics: {missing}")
    if spec.live_only:
        live_missing = [key for key in ALLOCATION_LIVE_DIAGNOSTICS if key not in diagnostics]
        if live_missing:
            raise ProfileValidationError(f"missing live-only diagnostics: {live_missing}")

    textual = {
        "Allocation backend",
        "Allocation coverage",
        "Allocation thread filter stage",
        "Allocation thread selection",
        "Allocation hook capabilities",
    }
    numeric: dict[str, int | float] = {}
    flags: dict[str, bool] = {}
    for key in required + (ALLOCATION_LIVE_DIAGNOSTICS if spec.live_only else ()):
        if key in textual or key == "Allocation analysis":
            continue
        value = diagnostics[key]
        if key in {
            "Execution profile storage exhausted",
            "Execution history truncated",
            "Execution data incomplete",
            "Allocation profile storage exhausted",
            "Allocation history truncated",
            "Allocation data incomplete",
            "Allocation live-only",
        }:
            flags[key] = _require_bool(value, key)
        elif key in {
            "Allocation average tracked lifetime ms (process-wide)",
            "Allocation maximum tracked lifetime ms (process-wide)",
            "Allocation retained average age ms",
            "Allocation retained maximum age ms",
        }:
            numeric[key] = _require_nonnegative_float(value, key)
        else:
            numeric[key] = _require_nonnegative_int(value, key)

    drops = {
        key: int(value)
        for key, value in numeric.items()
        if "drop" in key.lower() or "dropped" in key.lower() or "overflow" in key.lower()
    }
    capacities = {
        key: int(value)
        for key, value in numeric.items()
        if "capacity" in key.lower() and key not in drops
    }
    for key, value in capacities.items():
        if value <= 0:
            raise ProfileValidationError(f"diagnostic capacity {key!r} must be positive: {value}")
    for key, value in drops.items():
        if value != 0:
            raise ProfileValidationError(f"diagnostic drop {key!r} must be zero: {value}")

    if flags.get("Execution profile storage exhausted"):
        raise ProfileValidationError("Execution profile storage is exhausted")
    if flags.get("Execution data incomplete"):
        raise ProfileValidationError("Execution data is incomplete")
    if flags.get("Allocation profile storage exhausted"):
        raise ProfileValidationError("Allocation profile storage is exhausted")
    if flags.get("Allocation data incomplete"):
        raise ProfileValidationError("Allocation data is incomplete")

    queue_capacity = numeric.get("Allocation event queue capacity")
    queue_high_water = numeric.get("Allocation event queue high-water mark")
    if queue_capacity is not None and queue_high_water is not None and queue_high_water > queue_capacity:
        raise ProfileValidationError(
            f"allocation queue high-water mark exceeds capacity: {queue_high_water} > {queue_capacity}"
        )

    counters = {key: value for key, value in numeric.items() if key not in capacities and key not in drops}
    queue = {
        key: value
        for key, value in numeric.items()
        if any(term in key.lower() for term in ("queue", "pending", "thread root", "storage"))
    }
    incomplete_flags = {
        key: value
        for key, value in flags.items()
        if "incomplete" in key.lower() or "exhausted" in key.lower()
    }
    return {
        "raw": {key: diagnostics[key] for key in sorted(diagnostics)},
        "numeric": dict(sorted(numeric.items())),
        "flags": dict(sorted(flags.items())),
        "drops": dict(sorted(drops.items())),
        "capacities": dict(sorted(capacities.items())),
        "queue": dict(sorted(queue.items())),
        "counters": dict(sorted(counters.items())),
        "incomplete_flags": dict(sorted(incomplete_flags.items())),
    }


def _profile_shape(profile: ProfilePayload, spec: ProfilerModeSpec) -> dict[str, Any]:
    zero_included_ticks = spec.ticked and profile.number_of_included_ticks == 0
    if not profile.threads and not zero_included_ticks:
        raise ProfileValidationError("profile has no thread roots")
    for thread in profile.threads:
        values = [*thread.times, *(value for node in thread.nodes for value in node.times)]
        invalid = next((value for value in values if not math.isfinite(value) or value < 0), None)
        if invalid is not None:
            raise ProfileValidationError(f"profile contains an invalid sample weight: {invalid!r}")
    node_count = sum(len(thread.nodes) for thread in profile.threads)
    sample_value_count = sum(len(node.times) for thread in profile.threads for node in thread.nodes)
    root_weight = sum(thread.weight for thread in profile.threads)
    if (
        not zero_included_ticks
        and (node_count <= 0 or sample_value_count <= 0 or not math.isfinite(root_weight) or root_weight <= 0)
    ):
        raise ProfileValidationError(
            f"profile roots/samples are empty: threads={len(profile.threads)} nodes={node_count} "
            f"time_values={sample_value_count} root_weight={root_weight}"
        )

    path_count = 0
    max_depth = 0
    for _thread_name, path in iter_leaf_paths(profile):
        path_count += 1
        max_depth = max(max_depth, len(path))
    if zero_included_ticks and (sample_value_count > 0 or root_weight > 0 or path_count > 0):
        raise ProfileValidationError(
            "zero-inclusion profile contains positive samples, root weight, or reachable paths: "
            f"time_values={sample_value_count} root_weight={root_weight} paths={path_count}"
        )
    if path_count <= 0 and not zero_included_ticks:
        raise ProfileValidationError("profile has no reachable root-to-leaf sample paths")
    allocation_samples = profile.extra_metadata.get("Allocation profile samples accepted")
    sample_counts: dict[str, int | float] = {
        "decoded_time_values": sample_value_count,
        "number_of_ticks": profile.number_of_ticks,
        "included_ticks": profile.number_of_included_ticks,
    }
    if allocation_samples is not None:
        sample_counts["allocation_profile_samples_accepted"] = _require_nonnegative_int(
            allocation_samples, "Allocation profile samples accepted"
        )
    if spec.allocation and sample_counts.get("allocation_profile_samples_accepted", 0) <= 0:
        raise ProfileValidationError("allocation profile contains no accepted samples")

    duration = profile.duration_seconds
    if duration <= 0 or not math.isfinite(duration):
        raise ProfileValidationError(f"profile duration is not positive and finite: {duration}")
    return {
        "duration_seconds": duration,
        "thread_count": len(profile.threads),
        "node_count": node_count,
        "decoded_time_values": sample_value_count,
        "root_weight": root_weight,
        "root_weight_per_second": root_weight / duration,
        "reachable_leaf_paths": path_count,
        "max_stack_depth": max_depth,
        "roots_samples_required": not zero_included_ticks,
        "zero_included_ticks": zero_included_ticks,
        "sample_counts": sample_counts,
        "sample_rates": {
            "aggregate_root_weight_per_second": root_weight / duration,
            "decoded_time_values_per_second": sample_value_count / duration,
        },
    }


def validate_profile_payload(
    payload: bytes | ProfilePayload,
    mode: str,
    *,
    expected_duration_seconds: int | None = None,
) -> dict[str, Any]:
    """Decode and validate mode semantics and exported quality diagnostics."""

    spec = get_mode_spec(mode)
    if isinstance(payload, bytes):
        try:
            profile = parse_sampler_data(payload)
        except (TypeError, ValueError) as exc:
            raise ProfileValidationError(f"invalid Spark profile payload: {exc}") from exc
    elif isinstance(payload, ProfilePayload):
        profile = payload
    else:
        raise TypeError(f"payload must be bytes or ProfilePayload, got {type(payload).__name__}")
    failures: list[str] = []
    if not profile.metadata_present:
        failures.append("profile metadata is missing")
    if profile.sampler_mode != spec.sampler_mode:
        failures.append(f"sampler mode mismatch: observed={profile.sampler_mode} expected={spec.sampler_mode}")
    if profile.interval != spec.interval:
        failures.append(f"interval mismatch: observed={profile.interval} expected={spec.interval}")
    if not profile.thread_dumper_present:
        failures.append("thread dumper metadata is missing")
    else:
        expected_dumper_type = 0 if spec.all_threads else 1
        if profile.thread_dumper_type != expected_dumper_type:
            failures.append(
                f"thread dumper mismatch: observed={profile.thread_dumper_type} expected={expected_dumper_type}"
            )
        if profile.all_threads != spec.all_threads:
            failures.append(f"all_threads mismatch: observed={profile.all_threads} expected={spec.all_threads}")
    if not profile.data_aggregator_present:
        failures.append("data aggregator metadata is missing")
    else:
        expected_aggregator_type = 1 if spec.ticked else 0
        if profile.data_aggregator_type != expected_aggregator_type:
            failures.append(
                f"data aggregator mismatch: observed={profile.data_aggregator_type} "
                f"expected={expected_aggregator_type}"
            )
        if profile.ticked != spec.ticked:
            failures.append(f"ticked mismatch: observed={profile.ticked} expected={spec.ticked}")
        if profile.tick_threshold_us != spec.tick_threshold_us:
            failures.append(
                f"tick threshold mismatch: observed={profile.tick_threshold_us} expected={spec.tick_threshold_us}"
            )
        if spec.ticked:
            if profile.number_of_included_ticks < 0:
                failures.append(f"only-ticks-over included tick count is negative: {profile.number_of_included_ticks}")
            if profile.number_of_ticks < 0:
                failures.append(f"only-ticks-over completed tick count is negative: {profile.number_of_ticks}")
            if profile.number_of_included_ticks > profile.number_of_ticks:
                failures.append(
                    "included ticks exceed completed ticks: "
                    f"{profile.number_of_included_ticks} > {profile.number_of_ticks}"
                )
        elif profile.tick_threshold_us != 0 or profile.number_of_included_ticks != 0 or profile.included_ticks_present:
            failures.append("non-ticked profile exported tick metadata")
    if profile.number_of_ticks < 0:
        failures.append(f"number_of_ticks is negative: {profile.number_of_ticks}")
    if spec.allocation:
        live_value = profile.extra_metadata.get("Allocation live-only")
        if live_value is None:
            failures.append("Allocation live-only metadata is missing")
        elif live_value.strip().lower() != ("true" if spec.live_only else "false"):
            failures.append(
                f"allocation live-only mismatch: observed={live_value!r} expected={spec.live_only}"
            )
        allocation_interval = profile.extra_metadata.get("Allocation interval bytes")
        if allocation_interval is None:
            failures.append("Allocation interval bytes metadata is missing")
        else:
            try:
                if _require_nonnegative_int(allocation_interval, "Allocation interval bytes") != spec.interval:
                    failures.append(
                        f"allocation interval metadata mismatch: observed={allocation_interval!r} "
                        f"expected={spec.interval}"
                    )
            except ProfileValidationError as exc:
                failures.append(str(exc))

    duration_ok = True
    if expected_duration_seconds is not None:
        duration_ok = profile.duration_seconds >= max(1, expected_duration_seconds - 5)
        if not duration_ok:
            failures.append(
                f"profile duration too short: observed={profile.duration_seconds:.3f}s "
                f"expected_at_least={max(1, expected_duration_seconds - 5)}s"
            )

    diagnostics: dict[str, Any] | None = None
    shape: dict[str, Any] | None = None
    try:
        diagnostics = _diagnostic_summary(profile, spec)
        shape = _profile_shape(profile, spec)
    except ProfileValidationError as exc:
        failures.append(str(exc))

    if failures:
        raise ProfileValidationError("; ".join(failures))
    assert diagnostics is not None and shape is not None
    live_observed = False
    if spec.allocation:
        live_value = profile.extra_metadata.get("Allocation live-only")
        assert live_value is not None
        live_observed = _require_bool(live_value, "Allocation live-only")
    observed = {
        "sampler_mode": profile.sampler_mode,
        "interval": profile.interval,
        "all_threads": profile.all_threads,
        "ticked": profile.ticked,
        "tick_threshold_us": profile.tick_threshold_us,
        "number_of_included_ticks": profile.number_of_included_ticks,
        "number_of_ticks": profile.number_of_ticks,
        "allocation": spec.allocation,
        "allocation_live_only": live_observed,
    }
    assertions = {
        "metadata_present": profile.metadata_present,
        "mode": profile.sampler_mode == spec.sampler_mode,
        "interval": profile.interval == spec.interval,
        "all_threads": profile.all_threads == spec.all_threads,
        "tick_metadata": (
            profile.ticked == spec.ticked
            and profile.tick_threshold_us == spec.tick_threshold_us
            and profile.number_of_included_ticks >= 0
            and profile.number_of_ticks >= 0
            and (not spec.ticked or profile.number_of_included_ticks <= profile.number_of_ticks)
        ),
        "allocation_mode": (profile.sampler_mode == 1) == spec.allocation,
        "allocation_live_only": (
            not spec.allocation or live_observed == spec.live_only
        ),
        "nonempty_roots_samples": bool(
            shape["thread_count"] and shape["node_count"] and shape["decoded_time_values"]
        ) if not shape["zero_included_ticks"] else (
            shape["decoded_time_values"] == 0
            and shape["root_weight"] == 0
            and shape["reachable_leaf_paths"] == 0
        ),
        "diagnostics_complete": True,
        "incomplete_flags_clear": not any(diagnostics["incomplete_flags"].values()),
        "duration": duration_ok,
    }
    if not all(assertions.values()):
        raise ProfileValidationError(f"semantic assertions are not all true: {assertions}")
    return {
        "mode": spec.name,
        "expected": {
            "sampler_mode": spec.sampler_mode,
            "interval": spec.interval,
            "all_threads": spec.all_threads,
            "ticked": spec.ticked,
            "tick_threshold_us": spec.tick_threshold_us,
            "allocation": spec.allocation,
            "allocation_live_only": spec.live_only,
        },
        "observed": observed,
        "assertions": assertions,
        "shape": shape,
        "diagnostics": diagnostics,
        "profile_summary": profile_summary(profile),
    }


def _safe_file_bytes(path: pathlib.Path, *, root: pathlib.Path | None = None) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ProfileValidationError(f"profile payload is missing or symlinked: {path}")
    if root is not None:
        if root.is_symlink():
            raise ProfileValidationError(f"profile case root is a symlink: {root}")
        try:
            path.resolve().relative_to(root.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProfileValidationError(f"profile payload escapes case root: {path}") from exc
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProfileValidationError(f"unable to read profile payload: {path}: {exc}") from exc
    if not raw:
        raise ProfileValidationError(f"profile payload is empty: {path}")
    return raw


def read_verified_profile_payload(
    path: pathlib.Path,
    *,
    expected_sha256: str | None = None,
    root: pathlib.Path | None = None,
) -> tuple[bytes, str]:
    """Read a raw profile and reject missing, empty, escaped, or tampered bytes."""

    raw = _safe_file_bytes(pathlib.Path(path), root=root)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256.strip().lower():
        raise ProfileValidationError(
            f"profile payload SHA-256 mismatch: observed={digest} expected={expected_sha256.strip().lower()}"
        )
    return raw, digest


def _safe_case_directory(evidence_root: pathlib.Path, mode: str) -> pathlib.Path:
    raw_root = pathlib.Path(evidence_root)
    if raw_root.is_symlink():
        raise RuntimeError(f"evidence root is a symlink: {raw_root}")
    root = raw_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    case_dir = root / mode
    if case_dir.exists():
        if case_dir.is_symlink() or not case_dir.is_dir():
            raise RuntimeError(f"mode case is not a regular directory: {case_dir}")
        if any(case_dir.iterdir()):
            raise RuntimeError(f"mode case directory is not fresh: {case_dir}")
    else:
        case_dir.mkdir()
    try:
        case_dir.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"mode case escapes evidence root: {case_dir}") from exc
    return case_dir


def _validate_sha(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a full 40-character SHA-1: {value!r}")
    return normalized


class FinalProfilerMatrixCase(IntegrationTest):
    """One isolated Linux BDS case for one profiler mode."""

    disable_bstats = True

    def __init__(
        self,
        mode: str,
        bot_binary: pathlib.Path,
        spark_sha: str,
        *,
        bot_ref: str | None = None,
        bot_sha: str | None = None,
        profile_seconds: int = DEFAULT_PROFILE_SECONDS,
        warmup_seconds: int = DEFAULT_WARMUP_SECONDS,
    ) -> None:
        self.spec = get_mode_spec(mode)
        self.spark_sha = _validate_sha(spark_sha, "spark_sha")
        self.bot_ref = _validate_sha(bot_ref or os.environ.get("BOT_REF", ""), "bot_ref")
        observed_bot_sha = (bot_sha or os.environ.get("BOT_SHA", "")).strip().lower()
        if observed_bot_sha and observed_bot_sha != self.bot_ref:
            raise ValueError(f"bot SHA mismatch: observed={observed_bot_sha} expected={self.bot_ref}")
        self.bot_sha = observed_bot_sha or self.bot_ref
        self.profile_seconds = _bounded_duration(
            int(profile_seconds), label="profile_seconds", maximum=MAX_PROFILE_SECONDS
        )
        self.warmup_seconds = _bounded_duration(int(warmup_seconds), label="warmup_seconds", maximum=MAX_WARMUP_SECONDS)
        self.bot_binary = pathlib.Path(bot_binary).resolve()
        if self.bot_binary.is_symlink() or not self.bot_binary.is_file():
            raise ValueError(f"bot binary is missing or symlinked: {self.bot_binary}")

        super().__init__("linux")
        self.mode_result_path = self.root / "profiler-mode-result.json"
        self.bot: BotProcess | None = None
        self.result.update(
            {
                "test_kind": "spark-profiler-mode-matrix",
                "mode": self.spec.name,
                "spark_sha": self.spark_sha,
                "bot_ref": self.bot_ref,
                "bot_sha": self.bot_sha,
                "artifact_metadata": None,
                "options": {
                    "command": build_profiler_command(self.spec.name, self.profile_seconds),
                    "flags": list(self.spec.flags),
                    "profile_seconds": self.profile_seconds,
                    "warmup_seconds": self.warmup_seconds,
                    "requested_interval": (
                        1
                        if self.spec.name == "1ms"
                        else 4
                        if not self.spec.allocation
                        else DEFAULT_ALLOCATION_INTERVAL_BYTES
                    ),
                    "requested_interval_ms": None if self.spec.allocation else (1 if self.spec.name == "1ms" else 4),
                    "requested_interval_bytes": DEFAULT_ALLOCATION_INTERVAL_BYTES if self.spec.allocation else None,
                    "only_ticks_over_ms": ONLY_TICKS_OVER_MS if self.spec.ticked else None,
                },
                "provenance": {
                    "spark_sha": self.spark_sha,
                    "bot_ref": self.bot_ref,
                    "bot_sha": self.bot_sha,
                    "bot_binary_sha256": None,
                    "endstone": None,
                    "bds_version": None,
                    "spark_version": None,
                    "artifact_metadata": None,
                },
                "versions": {"bds": None, "endstone": None, "spark": None},
                "workload": {
                    "bot": {
                        "command": [str(self.bot_binary), *BOT_COMMAND_OPTIONS],
                        "options": list(BOT_COMMAND_OPTIONS),
                        "online_event": None,
                        "shutdown_event": None,
                    }
                },
                "warmup": {
                    "requested_seconds": self.warmup_seconds,
                    "started_at": None,
                    "completed_at": None,
                },
        "measurement": {
                    "command": build_profiler_command(self.spec.name, self.profile_seconds),
                    "started_at": None,
                    "completed_at": None,
                    "stop_command_sent": False,
                    "viewer_url": None,
            "observed_duration_seconds": None,
        },
        "bds_lifecycle": {"launches": [], "bootstrap_restart": False},
        "spark_run_id": None,
        "spark_artifact_id": None,
        "spark_artifact_name": None,
        "endstone_sha": None,
        "endstone_run_id": None,
        "endstone_artifact_id": None,
        "endstone_artifact_name": None,
        "profile": None,
                "spark_profile_viewer_url": None,
                "raw_profile_path": None,
                "raw_profile_sha256": None,
        "raw_profile_bytes": None,
        "semantic_assertions": None,
        "semantic_expected": None,
        "semantic_observed": None,
        "quality": None,
        "profile_manifest_path": None,
        "profile_manifest_sha256": None,
        "profile_manifest_bytes": None,
            }
        )
        self._write_results()

    def _write_results(self) -> None:
        write_json(self.result_path, self.result)
        write_json(self.mode_result_path, self.result)

    def check(self, name: str, status: str, detail: str | None = None, **extra: Any) -> None:
        super().check(name, status, detail, **extra)
        self._write_results()

    def install_artifacts(self) -> None:
        previous = os.environ.get("EXPECTED_SPARK_SHA")
        os.environ["EXPECTED_SPARK_SHA"] = self.spark_sha
        try:
            super().install_artifacts()
        finally:
            if previous is None:
                os.environ.pop("EXPECTED_SPARK_SHA", None)
            else:
                os.environ["EXPECTED_SPARK_SHA"] = previous

        components = self.metadata.get("components")
        if not isinstance(components, dict):
            raise TypeError("artifact metadata has no components")
        spark = components.get("spark")
        endstone = components.get("endstone")
        if not isinstance(spark, dict) or not isinstance(endstone, dict):
            raise TypeError("artifact metadata is missing Spark or Endstone component")
        observed_spark = str(spark.get("sha") or "").strip().lower()
        if observed_spark != self.spark_sha:
            raise RuntimeError(f"Spark artifact SHA mismatch: observed={observed_spark!r} expected={self.spark_sha!r}")
        for component_name, component in (("spark", spark), ("endstone", endstone)):
            observed_sha = str(component.get("sha") or "").strip().lower()
            artifact = component.get("artifact")
            if not SHA256_RE.fullmatch(observed_sha) or not component.get("run_id"):
                raise RuntimeError(f"{component_name} artifact provenance is incomplete: {component}")
            if not isinstance(artifact, dict) or not artifact.get("id") or not artifact.get("name"):
                raise RuntimeError(f"{component_name} artifact identity is incomplete: {component}")

        self.result["provenance"]["artifact_metadata"] = self.metadata
        self.result["artifact_metadata"] = self.metadata
        self.result["provenance"]["endstone"] = {
            "repository": endstone.get("repository"),
            "sha": endstone.get("sha"),
            "run_id": endstone.get("run_id"),
            "artifact_id": (endstone.get("artifact") or {}).get("id"),
            "artifact_name": (endstone.get("artifact") or {}).get("name"),
        }
        self.result["provenance"]["spark"] = {
            "repository": spark.get("repository"),
            "sha": spark.get("sha"),
            "run_id": spark.get("run_id"),
            "artifact_id": (spark.get("artifact") or {}).get("id"),
            "artifact_name": (spark.get("artifact") or {}).get("name"),
        }
        self.result["spark_run_id"] = spark.get("run_id")
        self.result["spark_artifact_id"] = (spark.get("artifact") or {}).get("id")
        self.result["spark_artifact_name"] = (spark.get("artifact") or {}).get("name")
        self.result["endstone_sha"] = endstone.get("sha")
        self.result["endstone_run_id"] = endstone.get("run_id")
        self.result["endstone_artifact_id"] = (endstone.get("artifact") or {}).get("id")
        self.result["endstone_artifact_name"] = (endstone.get("artifact") or {}).get("name")
        self.result["provenance"]["bot_binary_sha256"] = hashlib.sha256(self.bot_binary.read_bytes()).hexdigest()
        try:
            endstone_version = importlib.metadata.version("endstone")
            self.result["provenance"]["endstone"]["installed_version"] = endstone_version
            self.result["versions"]["endstone"] = endstone_version
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError("installed Endstone package has no discoverable version") from exc
        self._write_results()

    def start_server(self) -> None:
        super().start_server()
        assert self.server is not None
        launch = {
            "started_at": now_iso(),
            "pid": self.server.pid,
            "create_time": self.server.create_time,
            "command": self.server.started_command,
        }
        if not str(self.result.get("bds_version") or "").strip():
            raise RuntimeError("BDS did not expose an exact version.txt value")
        self.result["bds_lifecycle"]["launches"].append(launch)
        self.result["provenance"]["bds_version"] = self.result.get("bds_version") or None
        self.result["provenance"]["spark_version"] = self._spark_version(self.server.snapshot())
        if not self.result["provenance"]["spark_version"]:
            raise RuntimeError("Spark did not expose an exact runtime version in the BDS log")
        self.result["provenance"]["bds_version"] = self.result.get("bds_version")
        self.result["versions"]["bds"] = self.result.get("bds_version")
        self.result["versions"]["spark"] = self.result["provenance"]["spark_version"]
        self._write_results()

    @staticmethod
    def _spark_version(lines: list[str]) -> str | None:
        for line in reversed(lines):
            match = SPARK_VERSION_RE.search(line)
            if match:
                return match.group(1)
        return None

    def bootstrap_offline_server(self) -> None:
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            self.server.close()
            raise RuntimeError("BDS did not stop gracefully during bootstrap")
        self.server.close()
        self.server = None
        self.result["bds_lifecycle"]["bootstrap_restart"] = True
        patch_server_properties(self.server_dir / "server.properties")
        self.check("offline-server-properties", "PASS", "offline mode and bounded bot settings installed")

    def start_bot(self) -> None:
        assert self.server is not None
        self.bot = BotProcess(self.bot_binary, self.root / "bot.log")
        self.bot.start()
        online = self.bot.wait_event("online", 90)
        output, propagation, probes = wait_player_state(self.server, True, timeout=30)
        self.result["workload"]["bot"]["online_event"] = online
        self.check(
            "bot-online",
            "PASS",
            "real bot is visible in BDS",
            online_event=online,
            propagation_seconds=round(propagation, 3),
            probes=probes,
            output=output[-20:],
        )

    def stop_bot(self) -> None:
        if self.bot is None:
            return
        assert self.server is not None
        code = self.bot.terminate(20)
        if code != 0:
            raise RuntimeError(f"bot exited with code {code}")
        output, propagation, probes = wait_player_state(self.server, False, timeout=30)
        events = self.bot.event_snapshot()
        shutdown = next((event for event in reversed(events) if event.get("event") == "disconnected"), None)
        self.result["workload"]["bot"]["shutdown_event"] = shutdown
        self.check(
            "bot-shutdown",
            "PASS",
            "bot disconnected and BDS observed the removal",
            propagation_seconds=round(propagation, 3),
            probes=probes,
            output=output[-20:],
        )
        self.bot = None

    def run_warmup(self) -> None:
        assert self.server is not None
        started = now_iso()
        self.result["warmup"]["started_at"] = started
        self._write_results()
        deadline = time.monotonic() + self.warmup_seconds
        while time.monotonic() < deadline:
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during profiler warmup")
            time.sleep(min(1.0, deadline - time.monotonic()))
        self.result["warmup"]["completed_at"] = now_iso()
        self.command_check("warmup-profiler-info", "spark profiler info")

    def _viewer_url_from(self, lines: list[str], start_index: int) -> str | None:
        for line in lines[start_index:]:
            match = VIEWER_RE.search(line)
            if match:
                candidate = match.group(0).rstrip(").,]")
                parsed = urlparse(candidate)
                if parsed.scheme == "https" and parsed.netloc.casefold() == "spark.lucko.me":
                    key = parsed.path.strip("/")
                    if key and "/" not in key:
                        return candidate
        return None

    def _persist_profile(self, raw: bytes) -> tuple[pathlib.Path, str]:
        if not raw:
            raise ProfileValidationError("viewer returned an empty profile payload")
        path = self.root / f"{self.spec.name}.sparkprofile"
        if path.exists() or path.is_symlink():
            raise ProfileValidationError(f"profile payload path is not fresh: {path}")
        path.write_bytes(raw)
        verified, digest = read_verified_profile_payload(path, root=self.root)
        if verified != raw:
            raise ProfileValidationError("profile payload changed while being persisted")
        return path, digest

    def _write_evidence_manifest(self) -> None:
        entries: list[dict[str, Any]] = []
        candidates = [
            self.root / "metadata.json",
            self.root / "bstats-config.toml",
            self.root / f"{self.spec.name}.sparkprofile",
            self.root / f"{self.spec.name}-profile-summary.json",
        ]
        for path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            raw = path.read_bytes()
            entries.append(
                {
                    "path": str(path.relative_to(self.root)).replace("\\", "/"),
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        manifest = {
            "test_kind": self.result["test_kind"],
            "mode": self.spec.name,
            "generated_at": now_iso(),
            "files": entries,
        }
        path = self.root / "evidence-manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        self.result["profile_manifest_path"] = str(path.relative_to(self.root))
        manifest_bytes = path.read_bytes()
        self.result["profile_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
        self.result["profile_manifest_bytes"] = len(manifest_bytes)

    def run_profile(self) -> None:
        assert self.server is not None
        command = build_profiler_command(self.spec.name, self.profile_seconds)
        self.result["measurement"]["started_at"] = now_iso()
        start = self.server.command(command)
        deadline = time.monotonic() + self.profile_seconds + 90
        viewer_url: str | None = None
        while time.monotonic() < deadline:
            viewer_url = self._viewer_url_from(self.server.snapshot(), start)
            if viewer_url:
                break
            if not self.server.is_alive():
                raise RuntimeError(f"BDS exited during {self.spec.name} profiler")
            time.sleep(1)
        if viewer_url is None:
            if not self.server.is_alive():
                raise RuntimeError(f"BDS exited before {self.spec.name} profiler finalized")
            stop_at = self.server.command("spark profiler stop")
            self.result["measurement"]["stop_command_sent"] = True
            self._write_results()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                viewer_url = self._viewer_url_from(self.server.snapshot(), min(start, stop_at))
                if viewer_url:
                    break
                if not self.server.is_alive():
                    raise RuntimeError(f"BDS exited while finalizing {self.spec.name} profiler")
                time.sleep(1)
        if viewer_url is None:
            raise RuntimeError(f"{self.spec.name} profiler produced no direct viewer URL")
        self.result["measurement"]["viewer_url"] = viewer_url
        self.check("viewer-url-emitted", "PASS", viewer_url=viewer_url, command=command)

        raw = fetch_viewer_payload(viewer_url)
        profile_path, digest = self._persist_profile(raw)
        self.result["profile"] = {
            "viewer_url": viewer_url,
            "raw_path": str(profile_path.relative_to(self.root)),
            "raw_sha256": digest,
            "raw_bytes": len(raw),
            "path": str(profile_path.relative_to(self.root)),
            "sha256": digest,
            "bytes": len(raw),
            "payload_nonempty": bool(raw),
        }
        self.result["spark_profile_viewer_url"] = viewer_url
        self.result["raw_profile_path"] = str(profile_path.relative_to(self.root))
        self.result["raw_profile_sha256"] = digest
        self.result["raw_profile_bytes"] = len(raw)
        profile_url_key = "allocation_profile_viewer_url" if self.spec.allocation else "execution_profile_viewer_url"
        self.result[profile_url_key] = viewer_url
        self._write_results()

        verified_raw, _ = read_verified_profile_payload(
            profile_path,
            expected_sha256=digest,
            root=self.root,
        )
        profile = parse_sampler_data(verified_raw)
        validation = validate_profile_payload(
            profile,
            self.spec.name,
            expected_duration_seconds=self.profile_seconds,
        )
        summary_path = self.root / f"{self.spec.name}-profile-summary.json"
        summary_path.write_text(json.dumps(validation, indent=2, sort_keys=True), encoding="utf-8")
        self.result["profile"]["summary_path"] = str(summary_path.relative_to(self.root))
        self.result["semantic_assertions"] = validation["assertions"]
        self.result["semantic_expected"] = validation["expected"]
        self.result["semantic_observed"] = validation["observed"]
        self.result["quality"] = validation["shape"] | {"diagnostics": validation["diagnostics"]}
        self.result["measurement"]["observed_duration_seconds"] = validation["shape"]["duration_seconds"]
        self.result["measurement"]["completed_at"] = now_iso()
        self.check(
            "profile-payload-validation",
            "PASS",
            "raw viewer payload matches exported mode semantics and quality contract",
            raw_sha256=digest,
            raw_bytes=len(verified_raw),
            assertions=validation["assertions"],
            quality=self.result["quality"],
        )

    def execute_case(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            stage = "bds-bootstrap"
            self.start_server()
            self.run_basic_commands()
            self.bootstrap_offline_server()
            stage = "bds-start"
            self.start_server()
            self.run_basic_commands()
            stage = "bot-start"
            self.start_bot()
            stage = "warmup"
            self.run_warmup()
            stage = "profiler-mode"
            self.run_profile()
            stage = "bot-stop"
            self.stop_bot()
            stage = "shutdown"
            self.shutdown()
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
                if self.bot is not None and self.bot.is_alive():
                    self.bot.force_close()
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:  # noqa: BLE001 - preserve primary failure
                diagnostic += "\n\nCleanup failure:\n" + traceback.format_exc()
            last_lines = self.server.snapshot()[-250:] if self.server is not None else []
            (self.root / "failure-diagnostics.txt").write_text(
                diagnostic + "\n\nLast BDS log lines:\n" + "\n".join(last_lines), encoding="utf-8"
            )
            return 1
        finally:
            self.result["completed_at"] = now_iso()
            self.result["bds_lifecycle"]["shutdown_status"] = self.result.get("shutdown_status")
            self.split_logs()
            self._write_evidence_manifest()
            self._write_results()
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def _run_in_case(
    case_dir: pathlib.Path,
    *,
    mode: str,
    bot_binary: pathlib.Path,
    spark_sha: str,
    bot_ref: str,
    profile_seconds: int,
    warmup_seconds: int,
) -> int:
    previous = pathlib.Path.cwd()
    os.chdir(case_dir)
    try:
        validator = FinalProfilerMatrixCase(
            mode,
            bot_binary,
            spark_sha,
            bot_ref=bot_ref,
            profile_seconds=profile_seconds,
            warmup_seconds=warmup_seconds,
        )
        return validator.execute_case()
    finally:
        os.chdir(previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=tuple(spec.name for spec in PROFILER_MODES))
    parser.add_argument("--bot", required=True)
    parser.add_argument("--spark-sha", default=os.environ.get("EXPECTED_SPARK_SHA", ""))
    parser.add_argument("--bot-ref", default=os.environ.get("BOT_REF", ""))
    parser.add_argument("--evidence-root", default="evidence")
    parser.add_argument("--profile-seconds", type=int, default=DEFAULT_PROFILE_SECONDS)
    parser.add_argument("--warmup-seconds", type=int, default=DEFAULT_WARMUP_SECONDS)
    args = parser.parse_args()

    mode = get_mode_spec(args.mode).name
    case_dir = _safe_case_directory(pathlib.Path(args.evidence_root), mode)
    return _run_in_case(
        case_dir,
        mode=mode,
        bot_binary=pathlib.Path(args.bot).resolve(),
        spark_sha=args.spark_sha,
        bot_ref=args.bot_ref,
        profile_seconds=args.profile_seconds,
        warmup_seconds=args.warmup_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
