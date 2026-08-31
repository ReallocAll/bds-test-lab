#!/usr/bin/env python3
"""Run the pre-registered Candidate A blocked CPU benchmark.

The module deliberately keeps the existing Python attribution and bot harness
unchanged.  A block is four fresh BDS cases, one for each treatment.  The
companion analyzer consumes the case JSON written here.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
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
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import pairwise
from typing import Any

import psutil

from controller.bstats import (
    B_STATS_CONFIG_RELATIVE_PATH,
    B_STATS_EVIDENCE_PATH,
    BStatsConfigError,
    copy_bstats_evidence,
    write_disabled_bstats_config,
)
from controller.chunk_traversal_validation import configure_deterministic_world
from controller.fleet_spark_validation import set_server_property
from controller.python_attribution_performance import PythonAttributionPerformance
from controller.python_profile_payload import parse_sampler_data, profile_summary
from controller.run_test import now_iso, write_json

BASELINE_SHA = "15b79e814ee6542f8a2382df09353e9c2009c8d1"
CANDIDATE_SHA = "78314038b67d506ec48da9a61181c0048fb3658e"
ENDSTONE_SHA = "c76c814289ee3be8a7236389b6bdeb5728b154e4"
BOT_REF = "b8c4875bb1fafaa5dd9b8e91b16b613af47bf37a"

BLOCK_SIZE = 4
MAX_BLOCKS = 20
LEGAL_START_BLOCKS = (1, 5, 9, 13, 17)
WARMUP_SECONDS = 60
MEASUREMENT_SECONDS = 600
BOT_COUNT = 5
BOT_SCENARIO = "candidate-a-stationary"
BOT_SCENARIO_SHA256 = "169360cb46acc6dc29ed5b38e082543b12434860bb65119c519a095de2a04799"
HOTSPOT_MODE = "fleet"
HOTSPOT_ITERATIONS = 1000
HOTSPOT_ITERATIONS_RATIONALE = (
    "1000; pre-registered non-saturating setting based on the existing 1800-iteration baseline "
    "(~90% off / ~102% full before affinity)"
)
SAMPLE_INTERVAL_MS = 4
AFFINITY_POLL_INTERVAL_SECONDS = 1.0
CPU_METRIC_RESOLUTION_LIMIT_PERCENTAGE_POINTS = 0.5
CHUNK_RADIUS = 8
DETERMINISTIC_LEVEL_SEED = "8675309"
WORLD_SNAPSHOT_ID = "flat-seed-8675309-v1"
PROTOCOL_VERSION = "candidate-a-blocked-v1"
BOT_PROGRESS_COUNTER_SCOPE = "latest cumulative fleet_progress event at each boundary; deltas are boundary subtraction"
STATIONARY_BOUNDED_AREA_POLICY = "flat-world-fixed-seed; stationary after join; no movement or chunk traversal"
MANAGED_ROOT_TID_SCOPE = "all threads of the managed Endstone/BDS root process; descendants excluded"
RUNTIME_PAYLOAD_DIRS = ("downloads", "work", "hotspot-wheel")
EVIDENCE_MANIFEST_NAME = "candidate-a-evidence-manifest.json"
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_FILE_BYTES = 32 * 1024 * 1024

TREATMENTS = ("off-B", "off-C", "full-B", "full-C")
_BALANCED_SCHEDULES = (
    ("off-B", "off-C", "full-B", "full-C"),
    ("off-C", "off-B", "full-C", "full-B"),
    ("full-B", "full-C", "off-B", "off-C"),
    ("full-C", "full-B", "off-C", "off-B"),
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BLOCK_DIR_RE = re.compile(r"^block-[0-9]{2}$")
SCENARIO_FILE_ENV = "BDS_TEST_BOT_SCENARIO_FILE"


class BenchmarkConfigurationError(ValueError):
    """Raised for a malformed or non-pre-registered benchmark configuration."""


class AffinityError(RuntimeError):
    """Raised when controlled-process affinity cannot be verified."""


def validate_sha(value: str, expected: str, name: str) -> str:
    normalized = value.strip().lower()
    if not SHA_RE.fullmatch(normalized):
        raise BenchmarkConfigurationError(f"{name} must be a 40-character lowercase hexadecimal SHA")
    if normalized != expected:
        raise BenchmarkConfigurationError(f"{name} must equal the pre-registered SHA {expected}")
    return normalized


def treatment_spec(treatment: str) -> tuple[str, str]:
    if treatment not in TREATMENTS:
        raise BenchmarkConfigurationError(f"unknown treatment {treatment!r}; expected one of {TREATMENTS}")
    mode, revision = treatment.split("-", 1)
    return mode, revision


def block_schedule(block_index: int) -> tuple[str, ...]:
    if isinstance(block_index, bool) or not isinstance(block_index, int) or block_index < 1 or block_index > MAX_BLOCKS:
        raise BenchmarkConfigurationError(f"block index must be in 1..{MAX_BLOCKS}: {block_index!r}")
    return _BALANCED_SCHEDULES[(block_index - 1) % len(_BALANCED_SCHEDULES)]


def batch_schedule(start_block: int, batch_size: int = BLOCK_SIZE) -> dict[int, tuple[str, ...]]:
    if isinstance(start_block, bool) or not isinstance(start_block, int) or start_block not in LEGAL_START_BLOCKS:
        raise BenchmarkConfigurationError(
            f"start_block must be one of {LEGAL_START_BLOCKS}; got {start_block!r}"
        )
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size != BLOCK_SIZE:
        raise BenchmarkConfigurationError(f"batch_size is fixed at {BLOCK_SIZE}; got {batch_size!r}")
    end_block = start_block + batch_size - 1
    if end_block > MAX_BLOCKS:
        raise BenchmarkConfigurationError(f"batch ends at block {end_block}, beyond maximum {MAX_BLOCKS}")
    schedule = {index: block_schedule(index) for index in range(start_block, end_block + 1)}
    positions: dict[str, list[int]] = {treatment: [] for treatment in TREATMENTS}
    for row in schedule.values():
        if len(row) != BLOCK_SIZE or set(row) != set(TREATMENTS):
            raise BenchmarkConfigurationError("block schedule is not a permutation of the four treatments")
        for position, treatment in enumerate(row):
            positions[treatment].append(position)
    expected_positions = list(range(BLOCK_SIZE))
    if any(sorted(values) != expected_positions for values in positions.values()):
        raise BenchmarkConfigurationError(f"four-block schedule is not position-balanced: {positions}")
    return schedule


def case_id(block_index: int, position: int, treatment: str) -> str:
    if position < 0 or position >= BLOCK_SIZE:
        raise BenchmarkConfigurationError(f"case position must be in 0..{BLOCK_SIZE - 1}: {position}")
    treatment_spec(treatment)
    return f"block-{block_index:02d}-pos-{position + 1}-{treatment}"


INPUT_COUNTER_KEYS = (
    "packets_received",
    "chunks_received",
    "auth_inputs_sent",
    "movement_inputs_sent",
    "action_packets_sent",
)
PROGRESS_COUNTER_KEYS = INPUT_COUNTER_KEYS[1:]


def choose_controlled_cpu(available_cpus: list[int]) -> tuple[int, list[int]]:
    cpus = sorted({int(cpu) for cpu in available_cpus})
    if len(cpus) < 2:
        raise AffinityError(
            f"CPU affinity isolation requires at least two logical CPUs; runner exposes {cpus}"
        )
    controlled = cpus[-1]
    load_cpus = cpus[:-1]
    return controlled, load_cpus


def cpu_counter_resolution_seconds() -> float:
    try:
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, OSError, ValueError) as exc:
        raise RuntimeError(f"unable to determine Linux process CPU counter resolution: {exc}") from exc
    if clock_ticks <= 0:
        raise RuntimeError(f"Linux process CPU counter clock ticks are invalid: {clock_ticks}")
    return 1.0 / clock_ticks


def _linux_task_ids(pid: int) -> list[int]:
    if sys.platform != "linux":
        raise AffinityError("per-thread affinity verification requires Linux /proc task enumeration")
    task_dir = pathlib.Path(f"/proc/{pid}/task")
    try:
        tids = sorted(int(entry.name) for entry in task_dir.iterdir() if entry.name.isdigit())
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise AffinityError(f"unable to enumerate Linux tasks for pid {pid}: {exc}") from exc
    if not tids:
        raise AffinityError(f"Linux task enumeration returned no tasks for pid {pid}")
    return tids


def _sched_affinity(tid: int) -> list[int]:
    try:
        return sorted(int(cpu) for cpu in os.sched_getaffinity(tid))
    except (AttributeError, PermissionError, ProcessLookupError, OSError) as exc:
        raise AffinityError(f"unable to query Linux affinity for TID {tid}: {exc}") from exc


def _set_sched_affinity(tid: int, cpus: list[int]) -> None:
    try:
        os.sched_setaffinity(tid, set(cpus))
    except (AttributeError, PermissionError, ProcessLookupError, OSError) as exc:
        raise AffinityError(f"unable to set Linux affinity for TID {tid}: {exc}") from exc


def capture_task_affinity(pid: int, *, label: str) -> dict[str, list[int]]:
    """Capture every current task affinity while requiring a stable task set."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise AffinityError(f"{label} process ID is malformed: {pid!r}")
    captured: dict[str, list[int]] = {}
    for _attempt in range(8):
        tids = _linux_task_ids(pid)
        for tid in tids:
            key = str(tid)
            if key not in captured:
                captured[key] = _sched_affinity(tid)
        current_tids = set(_linux_task_ids(pid))
        if current_tids.issubset({int(tid) for tid in captured}):
            return {tid: captured[tid] for tid in sorted(captured, key=int) if int(tid) in current_tids}
    raise AffinityError(f"{label} task set did not stabilize while capturing affinity")


def capture_process_affinity(
    pid: int,
    *,
    label: str,
    process_affinity: list[int] | tuple[int, ...] | None = None,
    create_time: float | None = None,
) -> dict[str, Any]:
    """Capture process identity, process affinity, and all current task affinities."""

    try:
        process = psutil.Process(pid)
        observed_create_time = float(process.create_time()) if create_time is None else float(create_time)
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, TypeError, ValueError) as exc:
        raise AffinityError(f"unable to capture {label} process identity: {exc}") from exc
    if not math.isfinite(observed_create_time):
        raise AffinityError(f"{label} process create-time is not finite")
    if process_affinity is None:
        try:
            process_affinity = process.cpu_affinity()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError) as exc:
            raise AffinityError(f"unable to capture {label} process affinity: {exc}") from exc
    if not isinstance(process_affinity, (list, tuple)):
        raise AffinityError(f"{label} process affinity is malformed: {process_affinity!r}")
    try:
        normalized = sorted({int(cpu) for cpu in process_affinity})
    except (TypeError, ValueError) as exc:
        raise AffinityError(f"{label} process affinity is malformed: {process_affinity!r}") from exc
    if not normalized or any(cpu < 0 for cpu in normalized):
        raise AffinityError(f"{label} process affinity is empty or malformed: {process_affinity!r}")
    return {
        "pid": pid,
        "create_time": observed_create_time,
        "process_affinity": normalized,
        "tid_affinities": capture_task_affinity(pid, label=label),
    }


def pin_and_verify_task_affinity(
    pid: int,
    cpus: list[int] | tuple[int, ...],
    *,
    label: str,
    exact: bool = True,
    original_tids: dict[str, list[int]] | None = None,
    default_original: list[int] | tuple[int, ...] | None = None,
) -> dict[str, list[int]]:
    """Pin every current Linux task and repeat until no new task appears."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise AffinityError(f"{label} process ID is malformed: {pid!r}")
    if any(isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0 for cpu in cpus):
        raise AffinityError(f"{label} target CPU set is malformed: {cpus!r}")
    target = sorted(set(cpus))
    if not target:
        raise AffinityError(f"{label} has no target CPUs")
    observed: dict[str, list[int]] = {}
    stable = False
    for _attempt in range(8):
        tids = _linux_task_ids(pid)
        for tid in tids:
            current = _sched_affinity(tid)
            if original_tids is not None:
                key = str(tid)
                if key not in original_tids:
                    original_tids[key] = (
                        sorted({int(cpu) for cpu in default_original})
                        if default_original is not None
                        else current
                    )
            if exact and current != target:
                _set_sched_affinity(tid, target)
                current = _sched_affinity(tid)
            if exact and current != target:
                raise AffinityError(f"{label} TID {tid} remains outside target {target}: {current}")
            if not exact and not set(current).issubset(target):
                raise AffinityError(f"{label} TID {tid} includes CPU outside {target}: {current}")
            observed[str(tid)] = current
        current_tids = set(_linux_task_ids(pid))
        if not current_tids - set(tids):
            stable = True
            break
    if not stable:
        raise AffinityError(f"{label} task set did not stabilize while applying affinity")
    final_tids = _linux_task_ids(pid)
    missing = set(final_tids) - {int(tid) for tid in observed}
    if missing:
        raise AffinityError(f"{label} task enumeration changed after verification: {sorted(missing)}")
    return {tid: observed[tid] for tid in sorted(observed, key=int) if int(tid) in final_tids}


def restore_process_affinity(snapshot: dict[str, Any], *, label: str) -> dict[str, Any]:
    """Restore a captured process/task affinity and verify every current task."""

    if not isinstance(snapshot, dict):
        raise AffinityError(f"{label} affinity snapshot is missing")
    pid = snapshot.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise AffinityError(f"{label} affinity snapshot has an invalid process ID")
    try:
        expected_create_time = float(snapshot["create_time"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AffinityError(f"{label} affinity snapshot has an invalid create-time") from exc
    if not math.isfinite(expected_create_time):
        raise AffinityError(f"{label} affinity snapshot create-time is not finite")
    process_affinity = snapshot.get("process_affinity")
    if not isinstance(process_affinity, (list, tuple)):
        raise AffinityError(f"{label} affinity snapshot has no process affinity")
    try:
        expected_process_affinity = sorted({int(cpu) for cpu in process_affinity})
    except (TypeError, ValueError) as exc:
        raise AffinityError(f"{label} affinity snapshot process affinity is malformed") from exc
    if not expected_process_affinity or any(cpu < 0 for cpu in expected_process_affinity):
        raise AffinityError(f"{label} affinity snapshot process affinity is empty or malformed")
    original_tids = snapshot.get("tid_affinities")
    if not isinstance(original_tids, dict) or not original_tids:
        raise AffinityError(f"{label} affinity snapshot has no per-TID affinity")
    try:
        original_tids = {
            str(int(tid)): sorted({int(cpu) for cpu in cpus})
            for tid, cpus in original_tids.items()
        }
    except (TypeError, ValueError) as exc:
        raise AffinityError(f"{label} affinity snapshot per-TID affinity is malformed") from exc
    if any(not cpus or any(cpu < 0 for cpu in cpus) for cpus in original_tids.values()):
        raise AffinityError(f"{label} affinity snapshot contains an empty or malformed per-TID affinity")
    process = psutil.Process(pid)
    try:
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            raise AffinityError(f"{label} process is not alive during affinity restoration")
        actual_create_time = float(process.create_time())
    except AffinityError:
        raise
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError) as exc:
        raise AffinityError(f"unable to inspect {label} during affinity restoration: {exc}") from exc
    if not math.isfinite(actual_create_time) or abs(actual_create_time - expected_create_time) > 0.01:
        raise AffinityError(f"{label} process identity changed before affinity restoration")

    restored: dict[str, list[int]] = {}
    failures: dict[str, str] = {}
    stable = False

    def task_exited(tid: int) -> bool:
        try:
            return tid not in _linux_task_ids(pid)
        except (AffinityError, OSError):
            return False

    def record_tid_failure(tid: int, detail: str) -> None:
        key = str(tid)
        if task_exited(tid):
            restored.pop(key, None)
            failures.pop(key, None)
        else:
            restored.pop(key, None)
            failures.setdefault(key, detail)

    for _attempt in range(8):
        try:
            process.cpu_affinity(expected_process_affinity)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError) as exc:
            raise AffinityError(f"unable to restore {label} process affinity: {exc}") from exc
        tids = _linux_task_ids(pid)
        for tid in tids:
            key = str(tid)
            target = original_tids.get(key, expected_process_affinity)
            try:
                _set_sched_affinity(tid, target)
            except (AffinityError, OSError) as exc:
                record_tid_failure(tid, f"{label} TID {tid} could not be restored: {exc}")
                continue
            try:
                observed = _sched_affinity(tid)
            except (AffinityError, OSError) as exc:
                record_tid_failure(tid, f"{label} TID {tid} could not be verified: {exc}")
                continue
            if observed != target:
                record_tid_failure(tid, f"{label} TID {tid} was not restored: {observed} != {target}")
                continue
            restored[key] = observed
        current_tids = set(_linux_task_ids(pid))
        for key in list(failures):
            if int(key) not in current_tids:
                failures.pop(key, None)
        for key in list(restored):
            if int(key) not in current_tids:
                restored.pop(key, None)
        if not current_tids - set(tids):
            stable = True
            break
    if not stable:
        raise AffinityError(f"{label} task set did not stabilize during affinity restoration")
    try:
        observed_process_affinity = sorted(int(cpu) for cpu in process.cpu_affinity())
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError) as exc:
        raise AffinityError(f"unable to verify {label} process affinity restoration: {exc}") from exc
    if observed_process_affinity != expected_process_affinity:
        raise AffinityError(
            f"{label} process affinity was not restored: {observed_process_affinity} != {expected_process_affinity}"
        )
    final_tids = _linux_task_ids(pid)
    final_restored: dict[str, list[int]] = {}
    for tid in final_tids:
        key = str(tid)
        target = original_tids.get(key, expected_process_affinity)
        try:
            observed = _sched_affinity(tid)
        except (AffinityError, OSError) as exc:
            record_tid_failure(tid, f"{label} TID {tid} could not be verified: {exc}")
            continue
        if observed != target:
            record_tid_failure(tid, f"{label} TID {tid} was not restored: {observed} != {target}")
            continue
        final_restored[key] = observed
    surviving_tids = set(_linux_task_ids(pid))
    surviving_failures = [detail for key, detail in failures.items() if int(key) in surviving_tids]
    missing = surviving_tids - {int(tid) for tid in final_restored}
    if missing:
        surviving_failures.append(f"{label} surviving TIDs were not restored: {sorted(missing)}")
    if surviving_failures:
        raise AffinityError("; ".join(surviving_failures))
    return {
        "pid": pid,
        "create_time": actual_create_time,
        "process_affinity": observed_process_affinity,
        "tid_affinities": {
            tid: final_restored[tid] for tid in sorted(final_restored, key=int) if int(tid) in surviving_tids
        },
    }


def validate_affinity_snapshot(
    *,
    controlled_cpu: int,
    bds_affinity: list[int] | tuple[int, ...],
    load_generator_affinity: list[int] | tuple[int, ...],
    available_cpus: list[int] | tuple[int, ...] | None = None,
    bds_tid_affinities: dict[str, list[int]] | None = None,
    load_generator_tid_affinities: dict[str, list[int]] | None = None,
    controller_affinity: list[int] | tuple[int, ...] | None = None,
    controller_tid_affinities: dict[str, list[int]] | None = None,
) -> dict[str, Any]:
    def canonical_cpus(value: list[int] | tuple[int, ...], label: str) -> list[int]:
        if not isinstance(value, (list, tuple)) or any(
            isinstance(cpu, bool) or not isinstance(cpu, int) for cpu in value
        ):
            raise AffinityError(f"{label} CPU affinity is malformed: {value!r}")
        return sorted(set(value))

    def canonical_tid_map(value: dict[str, list[int]] | None, label: str) -> dict[str, list[int]]:
        if not isinstance(value, dict) or not value:
            raise AffinityError(f"{label} per-TID affinity evidence is missing")
        result: dict[str, list[int]] = {}
        for raw_tid, cpus in value.items():
            try:
                tid = int(raw_tid)
            except (TypeError, ValueError) as exc:
                raise AffinityError(f"{label} TID is malformed: {raw_tid!r}") from exc
            if tid <= 0:
                raise AffinityError(f"{label} TID is not positive: {raw_tid!r}")
            key = str(tid)
            if key in result:
                raise AffinityError(f"{label} contains duplicate TID {tid}")
            result[key] = canonical_cpus(cpus, f"{label} TID {tid}")
        return result

    if isinstance(controlled_cpu, bool) or not isinstance(controlled_cpu, int):
        raise AffinityError(f"controlled CPU is malformed: {controlled_cpu!r}")
    bds = canonical_cpus(bds_affinity, "BDS")
    load = canonical_cpus(load_generator_affinity, "load generator")
    available = canonical_cpus(
        available_cpus if available_cpus is not None else [*bds, *load],
        "runner",
    )
    if controller_affinity is None:
        raise AffinityError("controller process affinity evidence is missing")
    controller = canonical_cpus(controller_affinity, "controller")
    bds_tids = canonical_tid_map(bds_tid_affinities, "BDS")
    load_tids = canonical_tid_map(load_generator_tid_affinities, "load generator")
    controller_tids = canonical_tid_map(controller_tid_affinities, "controller")
    if len(available) < 2:
        raise AffinityError(f"runner topology cannot provide two logical CPUs: {available}")
    if bds != [controlled_cpu]:
        raise AffinityError(f"BDS affinity is not pinned to controlled CPU {controlled_cpu}: {bds}")
    if not load:
        raise AffinityError("load generator has no usable non-BDS CPU")
    if controlled_cpu in load:
        raise AffinityError(f"load generator affinity includes controlled BDS CPU {controlled_cpu}: {load}")
    if controller != load:
        raise AffinityError(f"controller and load generator affinity sets differ: {controller} != {load}")
    if controlled_cpu in controller:
        raise AffinityError(f"controller affinity includes controlled BDS CPU {controlled_cpu}: {controller}")
    if controlled_cpu not in available or any(cpu not in available for cpu in load):
        raise AffinityError(
            f"affinity contains a CPU outside runner topology: available={available} bds={bds} load={load}"
        )
    if any(values != [controlled_cpu] for values in bds_tids.values()):
        raise AffinityError(f"BDS per-TID affinity is outside controlled CPU {controlled_cpu}: {bds_tids}")
    if any(values != load for values in load_tids.values()):
        raise AffinityError(f"load-generator per-TID affinity does not match {load}: {load_tids}")
    if any(values != controller for values in controller_tids.values()):
        raise AffinityError(f"controller per-TID affinity does not match {controller}: {controller_tids}")
    return {
        "controlled_cpu": controlled_cpu,
        "bds_affinity": bds,
        "load_generator_affinity": load,
        "available_cpus": available,
        "bds_tid_affinities": bds_tids,
        "load_generator_tid_affinities": load_tids,
        "controller_affinity": controller,
        "controller_tid_affinities": controller_tids,
        "verified": True,
    }


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_TOP_LEVEL_EVIDENCE_FILES = frozenset(
    {
        "candidate-a-blocked-controller-error.json",
        "candidate-a-evidence-manifest.json",
        ".candidate-a-upload-ok",
        "preflight.txt",
    }
)
_BLOCK_LEVEL_EVIDENCE_FILES = frozenset(
    {
        "candidate-a-blocked-block.json",
        "case-status.tsv",
        EVIDENCE_MANIFEST_NAME,
    }
)
_CASE_EVIDENCE_FILES = frozenset(
    {
        B_STATS_EVIDENCE_PATH,
        "bds.log",
        "candidate-a-blocked-case.json",
        "candidate-a-blocked-result.json",
        "endstone.log",
        "failure-diagnostics.txt",
        "metadata.json",
        "python-attribution-performance.json",
        "python-attribution-performance.sparkprofile",
        "python-attribution-profile-summary.json",
        "python-attribution-result.json",
        "python-attribution.sparkprofile",
        "python-attribution-tick-metrics.json",
        "spark.log",
        "test-results.json",
    }
)


def _is_allowed_evidence_file(relative: pathlib.PurePath, *, block_relative: bool = False) -> bool:
    parts = relative.parts
    if len(parts) == 1:
        return parts[0] in _TOP_LEVEL_EVIDENCE_FILES or (block_relative and parts[0] in _BLOCK_LEVEL_EVIDENCE_FILES)
    if block_relative:
        if len(parts) != 2 or parts[0] not in TREATMENTS:
            return False
        name = parts[1]
    else:
        if len(parts) == 2 and BLOCK_DIR_RE.fullmatch(parts[0]):
            name = parts[1]
        elif len(parts) == 3 and BLOCK_DIR_RE.fullmatch(parts[0]) and parts[1] in TREATMENTS:
            name = parts[2]
            return name in _CASE_EVIDENCE_FILES or (
                name.startswith("python-attribution-bots-") and name.endswith(".log")
            )
        else:
            return False
    return name in _BLOCK_LEVEL_EVIDENCE_FILES or name in _CASE_EVIDENCE_FILES or (
        name.startswith("python-attribution-bots-") and name.endswith(".log")
    )


def _contained_direct_child(root: pathlib.Path, name: str) -> pathlib.Path:
    resolved_root = root.resolve()
    target = root / name
    if target.is_symlink():
        raise RuntimeError(f"refusing to remove symlink runtime payload: {target}")
    try:
        resolved_target = target.resolve()
        relative = resolved_target.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"runtime payload escapes case root: {target}") from exc
    if relative.parts != (name,):
        raise RuntimeError(f"runtime payload is not a direct case-root child: {target}")
    return target


def prune_case_runtime_payloads(case_root: pathlib.Path) -> list[str]:
    """Remove only generated runtime payload directories from one case root."""

    case_path = pathlib.Path(case_root)
    if case_path.is_symlink():
        raise RuntimeError(f"refusing to remove runtime payloads through symlink case root: {case_path}")
    root = case_path.resolve()
    if not root.is_dir():
        raise RuntimeError(f"case evidence root is not a directory: {root}")
    removed: list[str] = []
    for name in RUNTIME_PAYLOAD_DIRS:
        target = root / name
        if not target.exists() and not target.is_symlink():
            continue
        target = _contained_direct_child(root, name)
        if not target.is_dir():
            raise RuntimeError(f"runtime payload is not a directory: {target}")
        shutil.rmtree(target)
        removed.append(name)
    return removed


def _evidence_files(root: pathlib.Path, *, block_relative: bool = False) -> tuple[list[pathlib.Path], list[str]]:
    files: list[pathlib.Path] = []
    errors: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            errors.append(f"unexpected symlink in evidence: {relative}")
        elif path.is_file():
            if _is_allowed_evidence_file(relative, block_relative=block_relative):
                files.append(path)
            else:
                errors.append(f"unexpected evidence file: {relative}")
        elif path.is_dir():
            parts = relative.parts
            if block_relative and len(parts) == 1 and parts[0] in TREATMENTS:
                continue
            if not block_relative and len(parts) == 1 and BLOCK_DIR_RE.fullmatch(parts[0]):
                continue
            if not block_relative and len(parts) == 2 and parts[0].startswith("block-") and parts[1] in TREATMENTS:
                continue
            errors.append(f"unexpected evidence directory: {relative}")
    return sorted(files, key=lambda path: str(path.relative_to(root))), errors


def _write_block_evidence_manifest(block_dir: pathlib.Path) -> dict[str, Any]:
    files, errors = _evidence_files(block_dir, block_relative=True)
    if errors:
        raise RuntimeError("evidence allowlist rejected the block: " + "; ".join(errors))
    manifest_path = block_dir / EVIDENCE_MANIFEST_NAME
    files = [path for path in files if path != manifest_path]
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for path in files:
        size = path.stat().st_size
        if size > MAX_EVIDENCE_FILE_BYTES:
            raise RuntimeError(
                f"evidence file exceeds {MAX_EVIDENCE_FILE_BYTES} bytes: {path.relative_to(block_dir)} ({size})"
            )
        total_bytes += size
        entries.append(
            {
                "path": str(path.relative_to(block_dir)).replace("\\", "/"),
                "bytes": size,
                "sha256": _sha256_file(path),
            }
        )
    if total_bytes > MAX_EVIDENCE_BYTES:
        raise RuntimeError(f"evidence exceeds {MAX_EVIDENCE_BYTES} bytes: {total_bytes}")
    manifest = {
        "protocol_version": PROTOCOL_VERSION,
        "allowed_file_count": len(entries),
        "total_bytes": total_bytes,
        "max_file_bytes": MAX_EVIDENCE_FILE_BYTES,
        "max_total_bytes": MAX_EVIDENCE_BYTES,
        "runtime_payload_dirs_pruned": list(RUNTIME_PAYLOAD_DIRS),
        "files": entries,
    }
    write_json(manifest_path, manifest)
    final_files, final_errors = _evidence_files(block_dir, block_relative=True)
    if final_errors:
        raise RuntimeError("evidence allowlist rejected the manifest: " + "; ".join(final_errors))
    final_total = sum(path.stat().st_size for path in final_files)
    if final_total > MAX_EVIDENCE_BYTES:
        raise RuntimeError(f"evidence including manifest exceeds {MAX_EVIDENCE_BYTES} bytes: {final_total}")
    return manifest


def prepare_evidence_for_upload(evidence_root: pathlib.Path) -> list[dict[str, Any]]:
    """Prune case payloads and write a bounded allowlist manifest for Actions upload."""

    root = pathlib.Path(evidence_root).resolve()
    if not root.is_dir():
        raise RuntimeError(f"evidence root is not a directory: {root}")
    manifests: list[dict[str, Any]] = []
    block_dirs = sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("block-"))
    for block_dir in block_dirs:
        if block_dir.is_symlink() or not BLOCK_DIR_RE.fullmatch(block_dir.name):
            raise RuntimeError(f"unexpected block evidence directory: {block_dir.name}")
        for treatment in TREATMENTS:
            case_root = block_dir / treatment
            if case_root.exists() or case_root.is_symlink():
                prune_case_runtime_payloads(case_root)
        manifests.append(_write_block_evidence_manifest(block_dir))
    files, errors = _evidence_files(root)
    if errors:
        raise RuntimeError("evidence allowlist rejected the root: " + "; ".join(errors))
    if any(path.stat().st_size > MAX_EVIDENCE_FILE_BYTES for path in files):
        raise RuntimeError(f"an evidence file exceeds {MAX_EVIDENCE_FILE_BYTES} bytes")
    if sum(path.stat().st_size for path in files) > MAX_EVIDENCE_BYTES:
        raise RuntimeError(f"evidence exceeds {MAX_EVIDENCE_BYTES} bytes")
    return manifests


def _event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(event.get("event", "unknown")) for event in events).items()))


def _counter_snapshot(event: dict[str, Any], keys: tuple[str, ...]) -> dict[str, int] | None:
    values: dict[str, int] = {}
    for key in keys:
        value = event.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            numeric = int(value)
            exact = float(value) == numeric
        except (OverflowError, TypeError, ValueError):
            return None
        if numeric < 0 or not exact:
            return None
        values[key] = numeric
    return values


def latest_progress_counters(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest cumulative counters emitted by the fixed bot."""

    for index in range(len(events) - 1, -1, -1):
        if events[index].get("event") != "fleet_progress":
            continue
        snapshot = _counter_snapshot(events[index], PROGRESS_COUNTER_KEYS)
        if snapshot is not None:
            return {
                "counters": snapshot,
                "source_event": str(events[index].get("event", "unknown")),
                "source_index": index,
            }
    return None


def progress_window_deltas(boundaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    required = ("warmup_start", "warmup_end", "measurement_start", "measurement_end")
    snapshots: dict[str, dict[str, int]] = {}
    for name in required:
        boundary = boundaries.get(name)
        if not isinstance(boundary, dict):
            raise TypeError(f"missing workload boundary {name}")
        snapshot = boundary.get("progress_counters")
        if not isinstance(snapshot, dict):
            raise TypeError(f"missing progress counters at workload boundary {name}")
        counters = snapshot.get("counters")
        if not isinstance(counters, dict):
            raise TypeError(f"malformed progress counters at workload boundary {name}")
        parsed = _counter_snapshot(counters, PROGRESS_COUNTER_KEYS)
        if parsed is None:
            raise RuntimeError(f"progress counters at workload boundary {name} are not nonnegative integers")
        snapshots[name] = parsed
    for left, right in pairwise(required):
        for key in PROGRESS_COUNTER_KEYS:
            if snapshots[right][key] < snapshots[left][key]:
                raise RuntimeError(
                    f"progress counter {key} moved backwards from {left} to {right}: "
                    f"{snapshots[left][key]} -> {snapshots[right][key]}"
                )
    return {
        "counter_keys": list(PROGRESS_COUNTER_KEYS),
        "warmup": {
            key: snapshots["warmup_end"][key] - snapshots["warmup_start"][key]
            for key in PROGRESS_COUNTER_KEYS
        },
        "measurement": {
            key: snapshots["measurement_end"][key] - snapshots["measurement_start"][key]
            for key in PROGRESS_COUNTER_KEYS
        },
        "scope": BOT_PROGRESS_COUNTER_SCOPE,
        "monotonic": True,
    }


def nonnegative_input_counters(counters: dict[str, Any]) -> dict[str, int]:
    parsed = _counter_snapshot(counters, INPUT_COUNTER_KEYS)
    if parsed is None:
        raise RuntimeError("bot input counters are missing or not nonnegative integers")
    return parsed


def _int_diagnostic(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip().strip('"'))
    except (TypeError, ValueError):
        return None


def extract_pep_events(summary: dict[str, Any] | None) -> dict[str, int | None]:
    raw_value = (summary or {}).get("python_diagnostics") or {}
    raw = raw_value if isinstance(raw_value, dict) else {}
    keys = {
        "py_start": "Python PY_START events",
        "py_resume": "Python PY_RESUME events",
        "py_throw": "Python PY_THROW events",
        "py_return": "Python PY_RETURN events",
        "py_yield": "Python PY_YIELD events",
        "py_unwind": "Python PY_UNWIND events",
        "registered_threads": "Python registered threads",
        "overflows": "Python shadow overflows",
        "snapshot_attempts": "Python shadow snapshot attempts",
        "snapshot_failures": "Python shadow snapshot failures",
        "attributed_samples": "Python attributed samples",
        "native_only_samples": "Python native-only samples",
        "native_boundary_misses": "Python native boundary misses",
        "thread_mismatches": "Python thread mismatches",
        "unknown_code_ids": "Python unknown code IDs",
        "callback_failures": "Python monitoring callback failures",
    }
    return {name: _int_diagnostic(raw.get(key)) for name, key in keys.items()}


def validate_pep_events(events: dict[str, int | None], *, require_events: bool) -> dict[str, Any]:
    required_zero = ("callback_failures", "overflows", "unknown_code_ids")
    report: dict[str, Any] = {
        "required_zero": {name: events.get(name) for name in required_zero},
        "reported": {
            name: events.get(name)
            for name in ("native_boundary_misses", "snapshot_failures", "thread_mismatches")
        },
    }
    if require_events:
        missing = [name for name, value in events.items() if value is None]
        if missing:
            raise RuntimeError(f"profile is missing PEP diagnostics: {missing}")
        for name in required_zero:
            if events.get(name) != 0:
                raise RuntimeError(f"Python diagnostic {name} must be zero, got {events.get(name)!r}")
        if int(events.get("py_start") or 0) <= 0:
            raise RuntimeError("full case has no PY_START events")
    return report


def _scenario_contract(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        raise BenchmarkConfigurationError(f"configured bot scenario does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkConfigurationError(f"unable to read bot scenario {path}: {exc}") from exc
    scenario_name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or scenario_name != BOT_SCENARIO:
        raise BenchmarkConfigurationError(
            f"bot scenario must be the exact {BOT_SCENARIO!r} scenario; got {scenario_name!r}"
        )
    steps = payload.get("steps")
    if steps != [{"action": "idle"}]:
        raise BenchmarkConfigurationError(
            "bot scenario must contain exactly one indefinite idle step; movement and packet actions are forbidden"
        )
    scenario_sha256 = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    if scenario_sha256 != BOT_SCENARIO_SHA256:
        raise BenchmarkConfigurationError(
            f"bot scenario SHA mismatch: {scenario_sha256} != {BOT_SCENARIO_SHA256}"
        )
    return {
        "name": BOT_SCENARIO,
        "sha256": scenario_sha256,
        "steps": len(steps),
        "actions": ["idle"],
        "bounded_area_policy": STATIONARY_BOUNDED_AREA_POLICY,
        "indefinite_idle": True,
    }


def _world_contract(server_dir: pathlib.Path) -> dict[str, Any]:
    properties = server_dir / "server.properties"
    values: dict[str, str] = {}
    if properties.is_file():
        for line in properties.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    expected = {
        "level-type": "FLAT",
        "level-seed": DETERMINISTIC_LEVEL_SEED,
        "view-distance": str(CHUNK_RADIUS),
        "tick-distance": "4",
    }
    mismatch = {key: (values.get(key), value) for key, value in expected.items() if values.get(key) != value}
    if mismatch:
        raise RuntimeError(f"deterministic world configuration mismatch: {mismatch}")
    return {
        "snapshot_id": WORLD_SNAPSHOT_ID,
        "level_type": values["level-type"],
        "level_seed": values["level-seed"],
        "view_distance": int(values["view-distance"]),
        "tick_distance": int(values["tick-distance"]),
        "world_recreated": True,
    }


@contextmanager
def _working_directory(path: pathlib.Path) -> Iterator[None]:
    previous = pathlib.Path.cwd()
    path.mkdir(parents=True, exist_ok=True)
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class CandidateABlockedCase(PythonAttributionPerformance):
    """One fresh BDS measurement process for a single treatment."""

    def __init__(
        self,
        *,
        platform_name: str,
        bot_binary: pathlib.Path,
        block_index: int,
        position: int,
        treatment: str,
        baseline_sha: str,
        candidate_sha: str,
        bot_ref: str,
        scenario_contract: dict[str, Any],
    ) -> None:
        mode, revision = treatment_spec(treatment)
        self.case_result_path: pathlib.Path | None = None
        self.block_index = block_index
        self.position = position
        self.treatment = treatment
        self.revision = revision
        self.expected_spark_sha = baseline_sha if revision == "B" else candidate_sha
        self.baseline_sha = baseline_sha
        self.candidate_sha = candidate_sha
        self.bot_ref = bot_ref
        self.scenario_contract = scenario_contract
        self.protocol = {
            "protocol_version": PROTOCOL_VERSION,
            "case_id": case_id(block_index, position, treatment),
            "block_index": block_index,
            "position": position,
            "treatment": treatment,
            "mode": mode,
            "revision": revision,
            "baseline_sha": baseline_sha,
            "candidate_sha": candidate_sha,
            "endstone_sha": ENDSTONE_SHA,
            "bot_ref": bot_ref,
            "expected_spark_sha": self.expected_spark_sha,
            "platform": platform_name,
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
            "scenario": scenario_contract,
        }
        super().__init__(platform_name, bot_binary, mode, MEASUREMENT_SECONDS, BOT_COUNT)
        # Use the checked-in stationary scenario instead of the parent default.
        self.scenario = BOT_SCENARIO
        self.bot_log = self.root / f"python-attribution-bots-{platform_name}-{BOT_COUNT}-{BOT_SCENARIO}.log"
        self.result["bot_scenario"] = BOT_SCENARIO
        self.case_result_path = self.root / "candidate-a-blocked-result.json"
        self.result["protocol"] = self.protocol
        self.result["case_id"] = self.protocol["case_id"]
        self.result["treatment"] = self.treatment
        self.result["block_index"] = self.block_index
        self.result["position"] = self.position
        self.measurement_pid: int | None = None
        self.measurement_create_time: float | None = None
        self.initial_measurement_pid: int | None = None
        self.initial_measurement_create_time: float | None = None
        self.managed_root_identity: dict[str, Any] | None = None
        self.affinity: dict[str, Any] | None = None
        self._affinity_baselines: dict[str, dict[str, Any]] = {}
        self._affinity_restored: bool = False
        self._affinity_mutated: bool = False
        self._available_cpus: list[int] = []
        self._controlled_cpu: int | None = None
        self._load_cpus: list[int] = []
        self._bot_events: list[dict[str, Any]] = []
        self._bot_boundaries: dict[str, dict[str, Any]] = {}
        self._progress_deltas: dict[str, Any] | None = None
        self._affinity_samples: list[dict[str, Any]] = []
        self._affinity_phase = "bootstrap"
        self._world: dict[str, Any] | None = None
        self.bstats_config: dict[str, Any] | None = None
        self._warmup_start_ns = 0
        self._warmup_end_ns = 0
        self._write_results()
        write_json(self.root / "candidate-a-blocked-case.json", self.protocol)

    def _write_results(self) -> None:
        super()._write_results()
        if self.case_result_path is not None:
            write_json(self.case_result_path, self.result)

    def _managed_root_process(self) -> psutil.Process:
        if self.server is None or self.server.process is None:
            raise AffinityError("managed Endstone/BDS root process is not started")
        server_process = self.server.process
        server_pid = getattr(self.server, "pid", None)
        server_create_time = getattr(self.server, "create_time", None)
        server_command = getattr(self.server, "started_command", None)
        if not isinstance(server_command, list):
            server_command = getattr(self.server, "cmd", None)
        if server_pid != server_process.pid or server_create_time is None:
            raise AffinityError("ServerProcess PID/create-time identity is unavailable")
        if not isinstance(server_command, list) or not server_command or not all(
            isinstance(argument, str) for argument in server_command
        ):
            raise AffinityError("ServerProcess command identity is unavailable")
        if server_command[0] != sys.executable:
            raise AffinityError("ServerProcess does not use the configured Python interpreter")
        try:
            process = psutil.Process(server_process.pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                raise AffinityError("managed Endstone/BDS root process is not alive")
            create_time = float(process.create_time())
            name = process.name()
            exe = process.exe()
            cmdline = process.cmdline()
        except AffinityError:
            raise
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError) as exc:
            raise AffinityError(f"unable to inspect managed Endstone/BDS root process: {exc}") from exc
        if abs(create_time - float(server_create_time)) > 0.01:
            raise AffinityError("managed Endstone/BDS root PID was reused")
        try:
            same_interpreter = pathlib.Path(exe).resolve() == pathlib.Path(sys.executable).resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            same_interpreter = False
        if not same_interpreter:
            raise AffinityError("managed Endstone/BDS root executable is not sys.executable")
        expected_folder = str(self.server_dir.resolve())
        if cmdline != server_command:
            raise AffinityError("managed process command line does not match ServerProcess command")
        module_matches = [
            index
            for index, argument in enumerate(server_command)
            if argument == "-m" and index + 1 < len(server_command) and server_command[index + 1] == "endstone"
        ]
        folder_matches = [
            index
            for index, argument in enumerate(server_command)
            if (
                argument == "--server-folder"
                and index + 1 < len(server_command)
                and server_command[index + 1] == expected_folder
            )
        ]
        if len(module_matches) != 1 or len(folder_matches) != 1:
            raise AffinityError("managed process command is missing exact -m endstone/--server-folder identity")
        identity = {
            "role": "managed_endstone_bds_root",
            "pid": process.pid,
            "create_time": create_time,
            "server_process_pid": server_pid,
            "server_process_create_time": float(server_create_time),
            "interpreter": sys.executable,
            "name": name,
            "exe": exe,
            "server_process_command": list(server_command),
            "cmdline": list(cmdline),
            "server_folder": expected_folder,
            "alive": True,
        }
        self.managed_root_identity = identity
        self.result["managed_root_identity"] = identity
        return process

    def _measurement_process(self) -> psutil.Process:
        return self._managed_root_process()

    def install_artifacts(self) -> None:
        super().install_artifacts()
        self.result["artifact_metadata"] = self.metadata
        components = self.metadata.get("components")
        endstone = components.get("endstone") if isinstance(components, dict) else None
        if not isinstance(endstone, dict):
            raise BenchmarkConfigurationError("artifact metadata has no Endstone component")
        observed_sha = str(endstone.get("sha") or "").lower()
        validate_sha(observed_sha, ENDSTONE_SHA, "Endstone SHA")
        artifact = endstone.get("artifact")
        if not isinstance(artifact, dict):
            raise BenchmarkConfigurationError("Endstone artifact metadata is missing")
        artifact_id = artifact.get("id")
        run_id = endstone.get("run_id")
        artifact_name = artifact.get("name")
        if (
            isinstance(artifact_id, bool)
            or not isinstance(artifact_id, int)
            or artifact_id <= 0
            or isinstance(run_id, bool)
            or not isinstance(run_id, int)
            or run_id <= 0
            or not isinstance(artifact_name, str)
            or not artifact_name.strip()
        ):
            raise BenchmarkConfigurationError("Endstone version/artifact metadata is incomplete")
        if endstone.get("repository") != "EndstoneMC/endstone":
            raise BenchmarkConfigurationError(
                f"Endstone repository metadata mismatch: {endstone.get('repository')!r}"
            )
        self.protocol["endstone_artifact"] = {
            "repository": endstone["repository"],
            "sha": observed_sha,
            "run_id": run_id,
            "artifact": artifact,
        }
        self.result["protocol"] = self.protocol
        self._write_results()

    def _capture_bot_boundary(self, name: str, *, timestamp_ns: int | None = None) -> None:
        if self.bot is None:
            return
        events = self.bot.event_snapshot()
        progress = latest_progress_counters(events)
        self._bot_boundaries[name] = {
            "monotonic_ns": timestamp_ns if timestamp_ns is not None else time.monotonic_ns(),
            "event_count": len(events),
            "event_counts": _event_counts(events),
            "online_events": sum(1 for event in events if event.get("event") in {"online", "fleet_online"}),
            "fleet_progress_events": sum(1 for event in events if event.get("event") == "fleet_progress"),
            "progress_events": sum(1 for event in events if event.get("event") == "bot_progress"),
            "progress_counters": progress,
        }

    def _record_affinity_sample(
        self,
        *,
        bds_tids: dict[str, list[int]],
        load_tids: dict[str, list[int]],
        controller_tids: dict[str, list[int]],
    ) -> None:
        assert self.measurement_pid is not None and self._controlled_cpu is not None
        bds_process = self._managed_root_process()
        load_process = None
        if self.bot is not None and self.bot.process is not None:
            try:
                load_process = psutil.Process(self.bot.process.pid)
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError) as exc:
                raise AffinityError(f"unable to inspect load generator process: {exc}") from exc
        if load_process is None:
            raise AffinityError("load generator process was not started")
        try:
            bds_affinity = sorted(int(cpu) for cpu in bds_process.cpu_affinity())
            load_affinity = sorted(int(cpu) for cpu in load_process.cpu_affinity())
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError) as exc:
            raise AffinityError(f"unable to query process affinity: {exc}") from exc
        controller_affinity = _sched_affinity(os.getpid())
        validated = validate_affinity_snapshot(
            controlled_cpu=self._controlled_cpu,
            bds_affinity=bds_affinity,
            load_generator_affinity=load_affinity,
            available_cpus=self._available_cpus,
            bds_tid_affinities=bds_tids,
            load_generator_tid_affinities=load_tids,
            controller_affinity=controller_affinity,
            controller_tid_affinities=controller_tids,
        )
        self.affinity = (self.affinity or {}) | validated | {
            "bds_pid": self.measurement_pid,
            "bds_tid_scope": MANAGED_ROOT_TID_SCOPE,
            "managed_root_identity": self.managed_root_identity,
            "load_generator_pid": load_process.pid,
            "controller_pid": os.getpid(),
            "bds_affinity_after": bds_affinity,
            "load_generator_affinity": load_affinity,
            "controller_affinity": controller_affinity,
            "bds_tids": sorted(int(tid) for tid in bds_tids),
            "load_generator_tids": sorted(int(tid) for tid in load_tids),
            "controller_tids": sorted(int(tid) for tid in controller_tids),
            "bds_tid_affinities": bds_tids,
            "load_generator_tid_affinities": load_tids,
            "controller_tid_affinities": controller_tids,
        }
        self._affinity_samples.append(
            {
                "monotonic_ns": time.monotonic_ns(),
                "phase": self._affinity_phase,
                "bds_tids": bds_tids,
                "load_generator_tids": load_tids,
                "controller_tids": controller_tids,
            }
        )
        self.affinity["verification_count"] = len(self._affinity_samples)
        self.affinity["verification_samples"] = self._affinity_samples
        self.result["affinity"] = self.affinity

    def _verify_all_affinity(self) -> None:
        if self.measurement_pid is None or self._controlled_cpu is None:
            raise AffinityError("BDS measurement affinity was not configured")
        if self.bot is None or self.bot.process is None:
            raise AffinityError("load generator process was not started")
        bds_tids = pin_and_verify_task_affinity(
            self.measurement_pid,
            [self._controlled_cpu],
            label="BDS",
            exact=True,
            original_tids=self._affinity_baselines.get("bds", {}).get("tid_affinities"),
            default_original=self._affinity_baselines.get("bds", {}).get("process_affinity"),
        )
        if self.bot.process is None:
            raise AffinityError("load generator process was not started")
        load_tids = pin_and_verify_task_affinity(
            self.bot.process.pid,
            self._load_cpus,
            label="load generator",
            exact=True,
            original_tids=self._affinity_baselines.get("load_generator", {}).get("tid_affinities"),
            default_original=self._affinity_baselines.get("load_generator", {}).get("process_affinity"),
        )
        controller_tids = pin_and_verify_task_affinity(
            os.getpid(),
            self._load_cpus,
            label="controller",
            exact=True,
            original_tids=self._affinity_baselines.get("controller", {}).get("tid_affinities"),
            default_original=self._affinity_baselines.get("controller", {}).get("process_affinity"),
        )
        self._record_affinity_sample(
            bds_tids=bds_tids,
            load_tids=load_tids,
            controller_tids=controller_tids,
        )

    def _set_bot_affinity(self) -> None:
        if self.bot is None or self.bot.process is None:
            raise AffinityError("load generator process was not started")
        if not self._load_cpus:
            raise AffinityError("no non-BDS CPUs are available for the load generator")
        try:
            load_process = psutil.Process(self.bot.process.pid)
            load_baseline = capture_process_affinity(
                load_process.pid,
                label="load generator",
                process_affinity=load_process.cpu_affinity(),
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError, AffinityError) as exc:
            if isinstance(exc, AffinityError):
                raise
            raise AffinityError(f"unable to capture load generator affinity: {exc}") from exc
        self._affinity_baselines["load_generator"] = load_baseline
        self._affinity_mutated = True
        if self.affinity is None:
            self.affinity = {}
        self.affinity["original_affinity"] = self._affinity_baselines
        try:
            load_process.cpu_affinity(self._load_cpus)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError) as exc:
            raise AffinityError(f"unable to set load generator affinity: {exc}") from exc
        self._verify_all_affinity()
        self.check(
            "load-generator-affinity",
            "PASS",
            "controller and load generator pinned to CPUs excluding the controlled BDS CPU",
            **(self.affinity or {}),
        )
        self._write_results()

    def apply_measurement_affinity(self) -> None:
        process = self._measurement_process()
        try:
            available = sorted(int(cpu) for cpu in os.sched_getaffinity(0))
            controlled, load_cpus = choose_controlled_cpu(available)
            controller_baseline = capture_process_affinity(
                os.getpid(),
                label="controller",
                process_affinity=_sched_affinity(os.getpid()),
            )
            bds_baseline = capture_process_affinity(
                process.pid,
                label="BDS",
                process_affinity=process.cpu_affinity(),
                create_time=process.create_time(),
            )
            self._affinity_baselines = {"controller": controller_baseline, "bds": bds_baseline}
            self._affinity_mutated = True
            self._affinity_restored = False
            before = bds_baseline["process_affinity"]
            process.cpu_affinity([controlled])
            bds_tids = pin_and_verify_task_affinity(
                process.pid,
                [controlled],
                label="BDS",
                exact=True,
                original_tids=bds_baseline["tid_affinities"],
                default_original=bds_baseline["process_affinity"],
            )
            controller_tids = pin_and_verify_task_affinity(
                os.getpid(),
                load_cpus,
                label="controller",
                exact=True,
                original_tids=controller_baseline["tid_affinities"],
                default_original=controller_baseline["process_affinity"],
            )
            observed = sorted(int(cpu) for cpu in process.cpu_affinity())
            controller_affinity = _sched_affinity(os.getpid())
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError, AttributeError, AffinityError) as exc:
            if isinstance(exc, AffinityError):
                raise
            raise AffinityError(f"unable to set BDS measurement affinity: {exc}") from exc
        self._available_cpus = sorted(available)
        self._controlled_cpu = controlled
        self._load_cpus = load_cpus
        self.measurement_pid = process.pid
        self.measurement_create_time = process.create_time()
        self.affinity = {
            "bds_pid": process.pid,
            "initial_bds_pid": self.initial_measurement_pid,
            "initial_bds_create_time": self.initial_measurement_create_time,
            "bds_create_time": self.measurement_create_time,
            "bds_tid_scope": MANAGED_ROOT_TID_SCOPE,
            "managed_root_identity": self.managed_root_identity,
            "bds_affinity_before": before,
            "bds_affinity_after": sorted(observed),
            "controlled_cpu": controlled,
            "available_cpus": self._available_cpus,
            "load_generator_affinity": None,
            "load_generator_pid": None,
            "controller_pid": os.getpid(),
            "controller_affinity": controller_affinity,
            "bds_tids": sorted(int(tid) for tid in bds_tids),
            "controller_tids": sorted(int(tid) for tid in controller_tids),
            "bds_tid_affinities": bds_tids,
            "controller_tid_affinities": controller_tids,
            "load_generator_tid_affinities": None,
            "runner_cpu_topology": {
                "allowed_cpus": self._available_cpus,
                "controlled_cpu": controlled,
                "load_cpus": self._load_cpus,
                "cpu_count": os.cpu_count(),
                "controlled_process_isolation": True,
                "host_work_excluded": False,
                "kernel_and_unrelated_host_work_excluded": False,
            },
            "verification_count": 0,
            "verification_samples": [],
            "verified": False,
            "original_affinity": self._affinity_baselines,
            "restoration": {"status": "PENDING", "verified": False},
        }
        self.result["affinity"] = self.affinity
        self.check(
            "bds-affinity",
            "PASS",
            f"all existing BDS tasks pinned to controlled CPU {controlled}; new tasks are checked each interval",
            **self.affinity,
        )
        self._write_results()

    def _restore_affinity(self) -> None:
        if not self._affinity_mutated or self._affinity_restored:
            return
        restored: dict[str, Any] = {}
        failures: list[str] = []
        for label in ("load_generator", "bds", "controller"):
            snapshot = self._affinity_baselines.get(label)
            if snapshot is None:
                continue
            try:
                restored[label] = restore_process_affinity(snapshot, label=label)
            except AffinityError as exc:
                failures.append(f"{label}: {exc}")
        if self.affinity is None:
            self.affinity = {}
        if failures:
            self.affinity["restoration"] = {
                "status": "FAIL",
                "verified": False,
                "errors": failures,
                "restored": restored,
            }
            self.result["affinity"] = self.affinity
            self._write_results()
            raise AffinityError("affinity restoration could not be proven: " + "; ".join(failures))
        self._affinity_restored = True
        self.affinity["original_affinity"] = self._affinity_baselines
        self.affinity["restoration"] = {
            "status": "PASS",
            "verified": True,
            "restored": restored,
        }
        self.result["affinity"] = self.affinity
        self._write_results()

    def _verify_measurement_process(self) -> None:
        if self.measurement_pid is None or self.measurement_create_time is None or self._controlled_cpu is None:
            raise AffinityError("BDS measurement affinity was not configured")
        process = self._managed_root_process()
        if process.pid != self.measurement_pid:
            raise AffinityError("managed Endstone/BDS root PID changed during the measurement")
        try:
            create_time = process.create_time()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError) as exc:
            raise AffinityError(f"managed Endstone/BDS root disappeared or is inaccessible: {exc}") from exc
        if abs(create_time - self.measurement_create_time) > 0.01:
            raise AffinityError("managed Endstone/BDS root PID was reused during the measurement")
        self._verify_all_affinity()
        self.affinity["verified"] = True
        self.result["affinity"] = self.affinity

    def process_cpu_seconds(self) -> float:
        if self.measurement_pid is None:
            raise AffinityError("BDS measurement process is not configured")
        self._verify_measurement_process()
        return self._read_process_cpu_seconds()

    def _read_process_cpu_seconds(self) -> float:
        if self.measurement_pid is None or self.measurement_create_time is None:
            raise AffinityError("BDS measurement process is not configured")
        process = self._managed_root_process()
        if process.pid != self.measurement_pid:
            raise AffinityError("managed Endstone/BDS root PID changed while reading CPU time")
        try:
            times = process.cpu_times()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError) as exc:
            raise AffinityError(f"unable to read BDS process CPU counter: {exc}") from exc
        if abs(process.create_time() - self.measurement_create_time) > 0.01:
            raise AffinityError("managed Endstone/BDS root PID was reused while reading CPU time")
        return float(times.user) + float(times.system)

    def _cpu_snapshot(self) -> tuple[int, float]:
        """Read a BDS CPU counter after affinity verification and timestamp it."""

        self._verify_measurement_process()
        cpu_seconds = self._read_process_cpu_seconds()
        return time.monotonic_ns(), cpu_seconds

    def _query_gametime_snapshot(self) -> tuple[int, int]:
        value = self.query_gametime()
        return time.monotonic_ns(), value

    def process_rss_bytes(self) -> int:
        if self.measurement_pid is None:
            return super().process_rss_bytes()
        if self.measurement_create_time is None:
            raise AffinityError("BDS measurement process create-time is not configured")
        try:
            process = self._managed_root_process()
            if process.pid != self.measurement_pid:
                raise AffinityError("managed Endstone/BDS root PID changed while reading RSS")
            if abs(process.create_time() - self.measurement_create_time) > 0.01:
                raise AffinityError("managed Endstone/BDS root PID was reused while reading RSS")
            return int(process.memory_info().rss)
        except AffinityError:
            raise
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
            return 0

    def bootstrap_server(self) -> None:
        self._disable_bstats()
        self.start_server()
        self.wait_plugin()
        first = self._measurement_process()
        self.initial_measurement_pid = first.pid
        if self.managed_root_identity is not None:
            self.initial_measurement_create_time = self.managed_root_identity["create_time"]
        if not self.server or not self.server.graceful_stop(60):
            if self.server:
                self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop after initial world bootstrap")
        self.server.close()
        self.server = None

        properties = configure_deterministic_world(self.server_dir)
        set_server_property(properties, "max-players", "30")
        self.check(
            "world-configuration",
            "PASS",
            "fresh deterministic flat world configured before measurement process",
            level_seed=DETERMINISTIC_LEVEL_SEED,
            level_type="FLAT",
            snapshot_id=WORLD_SNAPSHOT_ID,
        )
        self._disable_bstats()
        self.start_server()
        self.wait_plugin()
        self.command_check("world-difficulty", "difficulty normal")
        self.command_check("world-mob-spawning", "gamerule doMobSpawning true")
        self.command_check("world-random-tick", "gamerule randomTickSpeed 1")
        second = self._measurement_process()
        if second.pid == self.initial_measurement_pid or (
            self.initial_measurement_create_time is not None
            and self.managed_root_identity is not None
            and abs(self.managed_root_identity["create_time"] - self.initial_measurement_create_time) <= 0.01
        ):
            raise RuntimeError("managed Endstone/BDS root process was not fresh after bootstrap restart")
        self._world = _world_contract(self.server_dir)
        self.protocol["world"] = self._world
        self.result["world"] = self._world
        self._write_results()

    def _disable_bstats(self) -> None:
        """Write, verify, and preserve bStats disablement before server startup."""

        try:
            evidence = write_disabled_bstats_config(self.server_dir)
            source = self.server_dir / pathlib.PurePosixPath(B_STATS_CONFIG_RELATIVE_PATH)
            copied = copy_bstats_evidence(self.root, source)
        except (BStatsConfigError, OSError) as exc:
            raise RuntimeError(f"unable to establish disabled bStats evidence: {exc}") from exc
        if copied != {**evidence, "relative_path": B_STATS_CONFIG_RELATIVE_PATH}:
            raise RuntimeError("copied bStats evidence does not match the server config")
        self.bstats_config = evidence
        self.protocol["bstats_config"] = evidence
        self.result["bstats_config"] = evidence
        self.check(
            "bstats-disabled",
            "PASS",
            "Endstone bStats disabled before BDS startup",
            relative_path=B_STATS_CONFIG_RELATIVE_PATH,
            canonical_enabled=False,
            bytes=evidence["bytes"],
            sha256=evidence["sha256"],
        )
        self._write_results()

    def start_bots(self) -> None:
        super().start_bots()
        self._set_bot_affinity()
        self._capture_bot_boundary("online")

    def _wait_alive(self, seconds: int, phase: str) -> None:
        assert self.server is not None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.server.is_alive():
                raise RuntimeError(f"BDS exited during {phase}")
            if self.bot is None or not self.bot.is_alive():
                raise RuntimeError(f"load generator exited during {phase}")
            self._verify_measurement_process()
            time.sleep(min(AFFINITY_POLL_INTERVAL_SECONDS, max(0.05, deadline - time.monotonic())))

    def measure_with_windows(self) -> tuple[dict[str, Any], int | None]:
        assert self.server is not None
        self._affinity_phase = "warmup"
        self._warmup_start_ns = time.monotonic_ns()
        self._capture_bot_boundary("warmup_start", timestamp_ns=self._warmup_start_ns)
        self._wait_alive(WARMUP_SECONDS, "60-second warmup")
        self._warmup_end_ns = time.monotonic_ns()
        self._capture_bot_boundary("warmup_end", timestamp_ns=self._warmup_end_ns)

        tick_start_ns, tick_start = self._query_gametime_snapshot()
        rss_start = self.process_rss_bytes()
        self._affinity_phase = "measurement"
        cpu_start_ns, cpu_start = self._cpu_snapshot()
        self.measure_start_ns = cpu_start_ns
        self._capture_bot_boundary("measurement_start", timestamp_ns=cpu_start_ns)
        profiler_start = self.start_profiler_if_needed()
        deadline_ns = cpu_start_ns + MEASUREMENT_SECONDS * 1_000_000_000
        rss: list[int] = []
        while time.monotonic_ns() < deadline_ns:
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during 600-second measurement window")
            if self.bot is None or not self.bot.is_alive():
                raise RuntimeError("load generator exited during 600-second measurement window")
            self._verify_measurement_process()
            rss_value = self.process_rss_bytes()
            if rss_value:
                rss.append(rss_value)
            remaining = max(0.05, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)
            time.sleep(min(AFFINITY_POLL_INTERVAL_SECONDS, remaining))
        tick_end_ns, tick_end = self._query_gametime_snapshot()
        cpu_end_ns, cpu_end = self._cpu_snapshot()
        self.measure_end_ns = cpu_end_ns
        self._capture_bot_boundary("measurement_end", timestamp_ns=cpu_end_ns)
        self._progress_deltas = progress_window_deltas(self._bot_boundaries)
        rss_end = self.process_rss_bytes()
        ticks = tick_end - tick_start
        if ticks <= 0:
            raise RuntimeError(f"non-positive Bedrock gametime delta: {tick_start} -> {tick_end}")
        cpu_snapshot_interval_ns = cpu_end_ns - cpu_start_ns
        wall_seconds = cpu_snapshot_interval_ns / 1_000_000_000
        cpu_seconds = cpu_end - cpu_start
        if cpu_seconds < 0.0:
            raise RuntimeError(f"BDS process CPU counter moved backwards: {cpu_start} -> {cpu_end}")
        counter_resolution_seconds = cpu_counter_resolution_seconds()
        metric_resolution = counter_resolution_seconds / wall_seconds * 100.0
        if metric_resolution >= CPU_METRIC_RESOLUTION_LIMIT_PERCENTAGE_POINTS:
            raise RuntimeError(
                "process CPU metric resolution is too coarse for the 0.5 percentage-point target: "
                f"{metric_resolution:.6f} percentage points"
            )
        return {
            "ticks": ticks,
            "wall_seconds": wall_seconds,
            "cpu_seconds": cpu_seconds,
            "process_cpu_percent_of_one_core": cpu_seconds / wall_seconds * 100.0,
            "cpu_ms_per_tick": cpu_seconds * 1000.0 / ticks,
            "observed_tps": ticks / wall_seconds,
            "rss_start_bytes": rss_start,
            "rss_end_bytes": rss_end,
            "rss_mean_bytes": int(sum(rss) / len(rss)) if rss else rss_end,
            "rss_peak_bytes": max(rss) if rss else rss_end,
            "counter_windows": {
                "warmup": {
                    "start_monotonic_ns": self._warmup_start_ns,
                    "end_monotonic_ns": self._warmup_end_ns,
                    "configured_seconds": WARMUP_SECONDS,
                    "observed_seconds": (self._warmup_end_ns - self._warmup_start_ns) / 1_000_000_000,
                },
                "measurement": {
                    "start_monotonic_ns": self.measure_start_ns,
                    "end_monotonic_ns": self.measure_end_ns,
                    "configured_seconds": MEASUREMENT_SECONDS,
                    "observed_seconds": wall_seconds,
                    "tick_start": tick_start,
                    "tick_end": tick_end,
                    "ticks": ticks,
                    "tick_start_monotonic_ns": tick_start_ns,
                    "tick_end_monotonic_ns": tick_end_ns,
                },
            },
            "cpu_snapshots": {
                "start": {"monotonic_ns": cpu_start_ns, "cpu_seconds": cpu_start},
                "end": {"monotonic_ns": cpu_end_ns, "cpu_seconds": cpu_end},
                "interval_ns": cpu_snapshot_interval_ns,
                "interval_seconds": wall_seconds,
                "denominator": "end CPU snapshot monotonic_ns - start CPU snapshot monotonic_ns",
                "counter_resolution_seconds": counter_resolution_seconds,
                "metric_resolution_percentage_points": metric_resolution,
            },
        }, profiler_start

    def tick_statistics(self) -> dict[str, Any]:
        data = json.loads(self.tick_metrics_path.read_text(encoding="utf-8"))
        samples = [
            item
            for item in data.get("samples", [])
            if self.measure_start_ns <= int(item["monotonic_ns"]) <= self.measure_end_ns
        ]
        if len(samples) < 100:
            raise RuntimeError(f"too few per-tick metrics inside exact measurement window: {len(samples)}")
        mspt = [float(item["mspt"]) for item in samples]
        tps = [float(item["tps"]) for item in samples]
        return {
            "samples": len(samples),
            "mspt_mean": statistics.fmean(mspt),
            "mspt_p50": _percentile(mspt, 0.50),
            "mspt_p95": _percentile(mspt, 0.95),
            "mspt_p99": _percentile(mspt, 0.99),
            "mspt_max": max(mspt),
            "tps_mean": statistics.fmean(tps),
            "tps_p50": _percentile(tps, 0.50),
            "tps_p05": _percentile(tps, 0.05),
            "tps_min": min(tps),
            "window": {
                "start_monotonic_ns": self.measure_start_ns,
                "end_monotonic_ns": self.measure_end_ns,
                "inclusive": True,
            },
        }

    def _workload_evidence(self) -> dict[str, Any]:
        events = self._bot_events
        shutdown = next((event for event in reversed(events) if event.get("event") == "fleet_shutdown"), None)
        online = next((event for event in events if event.get("event") == "fleet_online"), None)
        return {
            "bot_count": BOT_COUNT,
            "scenario": BOT_SCENARIO,
            "chunk_radius": CHUNK_RADIUS,
            "boundaries": self._bot_boundaries,
            "progress_window_deltas": self._progress_deltas,
            "progress_counter_scope": BOT_PROGRESS_COUNTER_SCOPE,
            "event_counts": _event_counts(events),
            "fleet_online": online,
            "fleet_shutdown": shutdown,
            "input_counters": {
                key: shutdown.get(key)
                for key in INPUT_COUNTER_KEYS
            }
            if shutdown
            else {},
            "balance_role": "check-only; never a CPU regression covariate",
        }

    def _record_profile(self, metrics: dict[str, Any], viewer_url: str | None) -> None:
        metrics["viewer_url"] = viewer_url
        if viewer_url:
            profile = parse_sampler_data(self.profile_path.read_bytes())
            summary = profile_summary(profile)
            metrics["profile_summary"] = summary
            pep_events = extract_pep_events(summary)
            metrics["pep_events"] = pep_events
            metrics["pep_diagnostics"] = validate_pep_events(pep_events, require_events=True)
            metrics["profile_file"] = self.profile_path.name
            metrics["profile_file_bytes"] = self.profile_path.stat().st_size
            metrics["profile_file_sha256"] = _sha256_file(self.profile_path)
        else:
            metrics["profile_summary"] = None
            metrics["pep_events"] = None
            metrics["pep_diagnostics"] = validate_pep_events({}, require_events=False)

    def execute(self) -> int:
        stage = "initialization"
        metrics: dict[str, Any] | None = None
        try:
            mode, _revision = treatment_spec(self.treatment)
            os.environ["EXPECTED_SPARK_SHA"] = self.expected_spark_sha
            os.environ["SPARK_PYTHON_ATTRIBUTION_MODE"] = "off" if mode == "off" else "auto"
            os.environ["SPARK_PYTHON_HOTSPOT_MODE"] = HOTSPOT_MODE
            os.environ["SPARK_PYTHON_HOTSPOT_ITERATIONS"] = str(HOTSPOT_ITERATIONS)
            os.environ["SPARK_PYTHON_TICK_METRICS"] = str(self.tick_metrics_path)
            scenario_path = pathlib.Path(os.environ.get(SCENARIO_FILE_ENV, "").strip()).resolve()
            os.environ[SCENARIO_FILE_ENV] = str(scenario_path)
            stage = "scenario-contract"
            verified_scenario = _scenario_contract(scenario_path)
            verified_scenario["path"] = str(scenario_path)
            if verified_scenario["sha256"] != self.scenario_contract.get("sha256"):
                raise BenchmarkConfigurationError("bot scenario changed after block manifest validation")
            self.scenario_contract = verified_scenario
            self.protocol["scenario"] = verified_scenario
            self.result["protocol"] = self.protocol
            self._write_results()
            stage = "artifact-install"
            self.install_artifacts()
            self.result["artifact_metadata"] = self.metadata
            spark_metadata = self.metadata.get("components", {}).get("spark", {})
            observed_spark_sha = str(spark_metadata.get("sha", "")).lower()
            if observed_spark_sha != self.expected_spark_sha:
                raise BenchmarkConfigurationError(
                    f"installed Spark SHA mismatch: {observed_spark_sha!r} != {self.expected_spark_sha}"
                )
            stage = "server-bootstrap"
            self.bootstrap_server()
            stage = "spark-sanity"
            self.run_basic_commands()
            stage = "bds-affinity"
            self.apply_measurement_affinity()
            stage = "bots-connect"
            self.start_bots()
            self.result["workload"] = self._workload_evidence()
            self._write_results()
            stage = "measurement-window"
            metrics, profiler_start = self.measure_with_windows()
            stage = "profile-stop"
            viewer_url, profile = self.stop_profiler(profiler_start)
            if profile is not None:
                metrics["profile_summary"] = profile
                metrics["pep_events"] = extract_pep_events(profile)
                metrics["pep_diagnostics"] = validate_pep_events(metrics["pep_events"], require_events=True)
                metrics["pep_event_window"] = {
                    "start_monotonic_ns": self.measure_start_ns,
                    "end_monotonic_ns": self.measure_end_ns,
                    "scope": "full-profile-cumulative; not window-aligned",
                }
                metrics["profile_file"] = self.profile_path.name
                metrics["profile_file_bytes"] = self.profile_path.stat().st_size
                metrics["profile_file_sha256"] = _sha256_file(self.profile_path)
                self.validate_profile(viewer_url or "")
            else:
                metrics["profile_summary"] = None
                metrics["pep_events"] = None
                metrics["pep_diagnostics"] = validate_pep_events({}, require_events=False)
                metrics["pep_event_window"] = {
                    "start_monotonic_ns": self.measure_start_ns,
                    "end_monotonic_ns": self.measure_end_ns,
                    "scope": "not applicable to attribution-off treatment",
                }
            metrics["viewer_url"] = viewer_url
            stage = "affinity-restore"
            self._restore_affinity()
            stage = "bots-disconnect"
            bot_before_disconnect = self.bot
            if self.bot is not None:
                self._capture_bot_boundary("before_disconnect")
                bot_before_disconnect = self.bot
            self.stop_bots()
            if bot_before_disconnect is not None:
                self._bot_events = bot_before_disconnect.event_snapshot()
            workload = self._workload_evidence()
            if not isinstance(workload.get("fleet_shutdown"), dict):
                raise RuntimeError("bot fleet did not emit a shutdown event")  # noqa: TRY004
            nonnegative_input_counters(workload.get("input_counters") or {})
            if self._progress_deltas is None:
                self._progress_deltas = progress_window_deltas(self._bot_boundaries)
            workload["progress_window_deltas"] = self._progress_deltas
            self.result["workload"] = workload
            stage = "shutdown"
            self.shutdown()
            stage = "tick-statistics"
            metrics["tick_statistics"] = self.tick_statistics()
            self.result["performance"] = metrics
            self.result["counter_windows"] = metrics["counter_windows"]
            self.result["affinity"] = self.affinity
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self.check("benchmark-window", "PASS", "60-second warmup followed by exact 600-second measurement")
            self._write_results()
            return 0
        except Exception as exc:  # noqa: BLE001 - preserve all case evidence on runtime failures
            self.result["status"] = "FAIL"
            self.result["state"] = "failed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"
            if metrics is not None:
                self.result["performance"] = metrics
            self._write_results()
            try:
                self._restore_affinity()
            except AffinityError as restoration_error:
                self.result["error_summary"] += f"; affinity restoration failed: {restoration_error}"
                self._write_results()
            try:
                self._bot_events = self.bot.event_snapshot() if self.bot is not None else self._bot_events
                self.result["workload"] = self._workload_evidence()
            except Exception:  # noqa: BLE001,S110 - cleanup must not hide the original failure
                pass
            try:
                if self.bot is not None:
                    self.stop_bots()
            except Exception:  # noqa: BLE001,S110 - cleanup must not hide the original failure
                pass
            try:
                self.shutdown()
            except Exception:  # noqa: BLE001,S110 - cleanup must not hide the original failure
                pass
            diagnostics = traceback.format_exc()
            try:
                last_lines = self.server.snapshot()[-300:] if self.server is not None else []
                (self.root / "failure-diagnostics.txt").write_text(
                    diagnostics + "\n\nLast log lines:\n" + "\n".join(last_lines), encoding="utf-8"
                )
            except Exception:  # noqa: BLE001,S110 - evidence splitting is best effort
                pass
            return 1
        finally:
            try:
                self._restore_affinity()
            except AffinityError as restoration_error:
                self.result["status"] = "FAIL"
                self.result["state"] = "failed"
                self.result["error_summary"] = (
                    f"{self.result.get('error_summary') or 'benchmark failed'}; "
                    f"affinity restoration failed: {restoration_error}"
                )
            self.result["completed_at"] = now_iso()
            self.protocol["world"] = self._world
            self.protocol["affinity"] = self.affinity
            self.protocol["managed_root_identity"] = self.managed_root_identity
            self.protocol["managed_root_tid_scope"] = MANAGED_ROOT_TID_SCOPE
            self.protocol["counter_windows"] = self.result.get("counter_windows") or (
                self.result.get("performance") or {}
            ).get("counter_windows")
            self.protocol["workload"] = self.result.get("workload")
            write_json(self.root / "candidate-a-blocked-case.json", self.protocol)
            try:
                self.split_logs()
            except Exception:  # noqa: BLE001,S110 - evidence splitting is best effort
                pass
            self._write_results()


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def run_case(
    *,
    case_dir: pathlib.Path,
    platform_name: str,
    bot_binary: pathlib.Path,
    block_index: int,
    position: int,
    treatment: str,
    baseline_sha: str,
    candidate_sha: str,
    bot_ref: str,
    scenario_contract: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    case_dir.mkdir(parents=True, exist_ok=True)
    with _working_directory(case_dir):
        validator = CandidateABlockedCase(
            platform_name=platform_name,
            bot_binary=bot_binary,
            block_index=block_index,
            position=position,
            treatment=treatment,
            baseline_sha=baseline_sha,
            candidate_sha=candidate_sha,
            bot_ref=bot_ref,
            scenario_contract=scenario_contract,
        )
        code = validator.execute()
        return code, validator.result


def run_block(
    *,
    evidence_root: pathlib.Path,
    block_index: int,
    platform_name: str,
    bot_binary: pathlib.Path,
    baseline_sha: str,
    candidate_sha: str,
    bot_ref: str,
) -> int:
    baseline_sha = validate_sha(baseline_sha, BASELINE_SHA, "baseline_sha")
    candidate_sha = validate_sha(candidate_sha, CANDIDATE_SHA, "candidate_sha")
    bot_ref = validate_sha(bot_ref, BOT_REF, "bot_ref")
    if platform_name != "linux":
        raise BenchmarkConfigurationError("Candidate A blocked benchmark is pre-registered for Linux only")
    if isinstance(block_index, bool) or not isinstance(block_index, int) or not 1 <= block_index <= MAX_BLOCKS:
        raise BenchmarkConfigurationError(f"block_index must be in 1..{MAX_BLOCKS}: {block_index!r}")
    schedule = batch_schedule(((block_index - 1) // BLOCK_SIZE) * BLOCK_SIZE + 1)[block_index]
    source_scenario = pathlib.Path(os.environ.get(SCENARIO_FILE_ENV, "").strip()).resolve()
    scenario_contract = _scenario_contract(source_scenario)
    scenario_contract["path"] = str(source_scenario)
    os.environ[SCENARIO_FILE_ENV] = str(source_scenario)
    block_dir = evidence_root / f"block-{block_index:02d}"
    block_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "protocol_version": PROTOCOL_VERSION,
        "block_index": block_index,
        "batch_start_block": ((block_index - 1) // BLOCK_SIZE) * BLOCK_SIZE + 1,
        "batch_size": BLOCK_SIZE,
        "schedule": list(schedule),
        "baseline_sha": baseline_sha,
        "candidate_sha": candidate_sha,
        "endstone_sha": ENDSTONE_SHA,
        "bot_ref": bot_ref,
        "platform": platform_name,
        "bot_count": BOT_COUNT,
        "bot_scenario": BOT_SCENARIO,
        "warmup_seconds": WARMUP_SECONDS,
        "measurement_seconds": MEASUREMENT_SECONDS,
        "sample_interval_ms": SAMPLE_INTERVAL_MS,
        "cpu_metric_resolution_limit_percentage_points": CPU_METRIC_RESOLUTION_LIMIT_PERCENTAGE_POINTS,
        "hotspot_iterations": HOTSPOT_ITERATIONS,
        "hotspot_iterations_rationale": HOTSPOT_ITERATIONS_RATIONALE,
        "affinity_poll_interval_seconds": AFFINITY_POLL_INTERVAL_SECONDS,
        "bot_progress_counter_keys": list(PROGRESS_COUNTER_KEYS),
        "bot_progress_counter_scope": BOT_PROGRESS_COUNTER_SCOPE,
        "pep_event_scope": "full-profile-cumulative; not window-aligned",
        "affinity_model": "controlled-process CPU isolation; host and kernel work are not excluded",
        "measurement_process_scope": "managed Endstone/BDS root process (python -m endstone); descendants excluded",
        "managed_root_tid_scope": MANAGED_ROOT_TID_SCOPE,
        "scenario": scenario_contract,
        "world_snapshot_id": WORLD_SNAPSHOT_ID,
    }
    write_json(block_dir / "candidate-a-blocked-block.json", manifest)
    status_path = block_dir / "case-status.tsv"
    overall = 0
    rows: list[dict[str, Any]] = []
    with status_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("block_index", "position", "treatment", "case_id", "exit_code", "status"),
            delimiter="\t",
        )
        writer.writeheader()
        for position, treatment in enumerate(schedule):
            identifier = case_id(block_index, position, treatment)
            case_dir = block_dir / treatment
            print(f"===== {identifier} =====", flush=True)
            try:
                code, result = run_case(
                    case_dir=case_dir,
                    platform_name=platform_name,
                    bot_binary=bot_binary,
                    block_index=block_index,
                    position=position,
                    treatment=treatment,
                    baseline_sha=baseline_sha,
                    candidate_sha=candidate_sha,
                    bot_ref=bot_ref,
                    scenario_contract=scenario_contract,
                )
                status = str(result.get("status", "FAIL"))
            except Exception as exc:  # noqa: BLE001 - continue the remaining balanced cases
                code = 1
                status = "FAIL"
                result = {
                    "protocol": manifest,
                    "status": status,
                    "state": "failed",
                    "failed_stage": "case-launch",
                    "error_summary": f"{type(exc).__name__}: {exc}",
                    "completed_at": now_iso(),
                }
                case_dir.mkdir(parents=True, exist_ok=True)
                write_json(case_dir / "candidate-a-blocked-result.json", result)
                write_json(
                    case_dir / "candidate-a-blocked-case.json",
                    {**manifest, "case_id": identifier, "treatment": treatment},
                )
                (case_dir / "failure-diagnostics.txt").write_text(traceback.format_exc(), encoding="utf-8")
            if code != 0 or status != "PASS":
                overall = 1
            row = {
                "block_index": block_index,
                "position": position,
                "treatment": treatment,
                "case_id": identifier,
                "exit_code": code,
                "status": status,
            }
            rows.append(row)
            writer.writerow(row)
            stream.flush()
    manifest["case_status"] = rows
    manifest["status"] = "PASS" if overall == 0 else "FAIL"
    manifest["evidence_gate"] = {
        "status": "PASS",
        "manifest_count": 1,
        "max_total_bytes": MAX_EVIDENCE_BYTES,
        "max_file_bytes": MAX_EVIDENCE_FILE_BYTES,
    }
    write_json(block_dir / "candidate-a-blocked-block.json", manifest)
    try:
        prepare_evidence_for_upload(evidence_root)
    except Exception as exc:  # noqa: BLE001 - fail closed while retaining case evidence
        overall = 1
        manifest["status"] = "FAIL"
        manifest["evidence_gate"] = {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
        }
        write_json(block_dir / "candidate-a-blocked-block.json", manifest)
        print(f"Candidate A evidence gate failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return overall


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="linux", choices=["linux"])
    parser.add_argument("--bot")
    parser.add_argument("--block-index", type=int)
    parser.add_argument("--evidence-root", default="evidence")
    parser.add_argument(
        "--prepare-evidence",
        action="store_true",
        help="prune generated case payloads and verify the upload evidence allowlist",
    )
    parser.add_argument("--baseline-sha", default=BASELINE_SHA)
    parser.add_argument("--candidate-sha", default=CANDIDATE_SHA)
    parser.add_argument("--bot-ref", default=BOT_REF)
    args = parser.parse_args()
    if args.prepare_evidence:
        try:
            prepare_evidence_for_upload(pathlib.Path(args.evidence_root).resolve())
            return 0
        except Exception as exc:  # noqa: BLE001 - emit configuration evidence for Actions upload
            print(f"Candidate A evidence preparation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if not args.bot or args.block_index is None:
        parser.error("--bot and --block-index are required unless --prepare-evidence is supplied")
    try:
        return run_block(
            evidence_root=pathlib.Path(args.evidence_root).resolve(),
            block_index=args.block_index,
            platform_name=args.platform,
            bot_binary=pathlib.Path(args.bot).resolve(),
            baseline_sha=args.baseline_sha,
            candidate_sha=args.candidate_sha,
            bot_ref=args.bot_ref,
        )
    except Exception as exc:  # noqa: BLE001 - emit configuration evidence for Actions upload
        error_root = pathlib.Path(args.evidence_root).resolve()
        error_root.mkdir(parents=True, exist_ok=True)
        write_json(
            error_root / "candidate-a-blocked-controller-error.json",
            {
                "protocol_version": PROTOCOL_VERSION,
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
                "completed_at": now_iso(),
            },
        )
        print(f"Candidate A blocked benchmark configuration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
