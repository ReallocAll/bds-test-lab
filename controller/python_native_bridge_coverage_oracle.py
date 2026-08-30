#!/usr/bin/env python3
"""Run and analyze the paired fixed-window Python attribution correctness oracle."""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import shutil
import statistics
import sys
import time
from itertools import pairwise
from typing import Any

from controller.python_profile_payload import (
    Node,
    ProfilePayload,
    ThreadTree,
    fetch_viewer_payload,
    iter_leaf_paths,
    parse_sampler_data,
    python_nodes,
)
from controller.run_test import IntegrationTest, now_iso, run_checked, write_json

PARENT_SHA = "78314038b67d506ec48da9a61181c0048fb3658e"
CANDIDATE_SHA = "ea0af5f3abf1817bba126b3cc9bfe78d837cb329"
EXPECTED_MODULE = "endstone_spark_python_coverage_oracle"
EXPECTED_SOURCE = "spark-python-coverage-oracle"
EXPECTED_FIXED = "CoveragePlugin.fixed_window_tick"
EXPECTED_NESTED = "CoveragePlugin.fixed_window_tick.<locals>.nested_call"
EXPECTED_PROFILE_SECONDS = 60
EXPECTED_WARMUP_SECONDS = 30
WINDOW_NS = 20_000_000
COUNTER_CLOCK = "monotonic_ns"
COUNTER_CLOCK_SOURCE = "time.perf_counter_ns"
COUNTER_ALIGNMENT_METHOD = "timestamp-slice"
COUNTER_SCOPE = "profile-window-only; full windows selected by monotonic timestamps"
START_ACK_TOKEN = "profiler is now running"
STOP_REQUEST_ACK_TOKEN = "stopping the profiler and finalizing results"
STOP_COMPLETE_ACK_TOKEN = "profiler stopped"
OBSERVER_TOKENS = (
    "pyStartThunk",
    "pyResumeThunk",
    "pyThrowThunk",
    "pyReturnThunk",
    "pyYieldThunk",
    "pyUnwindThunk",
    "pyStartNativeCallback",
    "pyResumeNativeCallback",
    "pyThrowNativeCallback",
    "pyReturnNativeCallback",
    "pyYieldNativeCallback",
    "pyUnwindNativeCallback",
    "nativeEventCallback",
)
REQUIRED_DIAGNOSTICS = {
    "Python attribution backend": None,
    "Python function attribution enabled": None,
    "Python version": None,
    "Python PY_START events": 1,
    "Python shadow snapshot attempts": 1,
    "Python shadow snapshot failures": 0,
    "Python shadow overflows": 0,
    "Python unknown code IDs": 0,
    "Python native boundary misses": 0,
    "Python thread mismatches": 0,
    "Python monitoring callback failures": 0,
    "Python attributed samples": 1,
    "Python native-only samples": 1,
}

# Student-t critical values for a two-sided 95% interval and a one-sided 95%
# lower bound. The oracle normally runs five pairs (df=4).
TWO_SIDED_95 = {
    1: 12.7062047364,
    2: 4.3026527299,
    3: 3.1824463053,
    4: 2.7764451052,
    5: 2.5705818356,
    6: 2.4469118511,
    7: 2.3646242510,
    8: 2.3060041350,
    9: 2.2621571630,
    10: 2.22813885196,
    11: 2.2009851601,
    12: 2.1788128297,
    13: 2.1603686565,
    14: 2.1447866879,
    15: 2.1314495456,
    16: 2.1199052992,
    17: 2.1098155778,
    18: 2.1009220402,
    19: 2.0930240544,
    20: 2.0859634473,
    21: 2.0796138448,
    22: 2.0738730679,
    23: 2.0686576104,
    24: 2.0638985616,
    25: 2.0595385528,
    26: 2.0555294386,
    27: 2.0518305165,
    28: 2.0484071418,
    29: 2.0452296421,
    30: 2.0422724563,
}
ONE_SIDED_95 = {
    1: 6.3137515148,
    2: 2.9199855804,
    3: 2.3533634348,
    4: 2.1318467863,
    5: 2.0150483733,
    6: 1.9431802804,
    7: 1.8945786051,
    8: 1.8595480375,
    9: 1.8331129327,
    10: 1.8124611228,
    11: 1.7958848187,
    12: 1.7822875556,
    13: 1.7709333959,
    14: 1.7613101367,
    15: 1.7530503557,
    16: 1.7458836763,
    17: 1.7396067261,
    18: 1.7340636066,
    19: 1.7291328115,
    20: 1.7247182429,
    21: 1.7207429028,
    22: 1.7171443744,
    23: 1.7138715277,
    24: 1.7108820799,
    25: 1.7081407613,
    26: 1.7056179198,
    27: 1.7032884457,
    28: 1.7011309343,
    29: 1.6991270265,
    30: 1.6972608860,
}
CASE_STATUS_HEADER = "rep\ttarget\tsha\tlabel\tcontroller_exit_code\tstatus\tnote"


class AlignmentError(RuntimeError):
    """Raised when cumulative records cannot prove an aligned interval."""


def _line_contains(token: str):
    token = token.lower()
    return lambda line: token in line.lower()


def _wait_command_ack(server: Any, start_index: int, predicate, timeout: float) -> tuple[list[str], int]:
    """Observe a command acknowledgement using the shared server snapshot API."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = server.snapshot()
        output = lines[start_index:]
        if any(predicate(line) for line in output):
            return output, time.perf_counter_ns()
        if not server.is_alive():
            break
        time.sleep(0.05)
    raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for command acknowledgement")


def validate_profile_boundaries(boundaries: dict[str, Any]) -> list[str]:
    """Validate command acknowledgements and the effective sampled interval."""

    failures: list[str] = []
    if not isinstance(boundaries, dict):
        return ["profile boundaries are not an object"]
    if boundaries.get("clock") != COUNTER_CLOCK or boundaries.get("clock_source") != COUNTER_CLOCK_SOURCE:
        failures.append("profile boundary clock is missing or mismatched")
    if boundaries.get("strict") is not True:
        failures.append("profile boundaries are not marked strict")
    ordered_names = (
        "start_command_sent_ns",
        "start_ack_observed_ns",
        "profile_start_ns",
        "profile_end_ns",
        "stop_command_sent_ns",
        "stop_request_ack_observed_ns",
        "stop_complete_ack_observed_ns",
        "stop_command_flushed_ns",
    )
    values: dict[str, int] = {}
    for name in ordered_names:
        value = boundaries.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            failures.append(f"profile boundary {name} is missing or invalid")
        else:
            values[name] = value
    if len(values) == len(ordered_names):
        for previous, current in pairwise(ordered_names):
            if values[previous] > values[current]:
                failures.append(f"profile boundary ordering is invalid: {previous} > {current}")
        if values["profile_start_ns"] >= values["profile_end_ns"]:
            failures.append("profile effective interval is empty")
    if boundaries.get("start_acknowledgement") != START_ACK_TOKEN:
        failures.append("Spark start acknowledgement evidence is missing")
    if boundaries.get("stop_request_acknowledgement") != STOP_REQUEST_ACK_TOKEN:
        failures.append("Spark stop acknowledgement evidence is missing")
    if boundaries.get("stop_complete_acknowledgement") != STOP_COMPLETE_ACK_TOKEN:
        failures.append("Spark stop completion acknowledgement evidence is missing")
    for key, token, label in (
        ("start_ack_line", START_ACK_TOKEN, "start"),
        ("stop_request_ack_line", STOP_REQUEST_ACK_TOKEN, "stop"),
        ("stop_complete_ack_line", STOP_COMPLETE_ACK_TOKEN, "stop completion"),
    ):
        line = boundaries.get(key)
        if not isinstance(line, str) or token not in line.lower():
            failures.append(f"Spark {label} acknowledgement line is missing or mismatched")
    command_window_start = boundaries.get("command_window_start_ns")
    command_window_end = boundaries.get("command_window_end_ns")
    if command_window_start is not None or command_window_end is not None:
        if (
            isinstance(command_window_start, bool)
            or not isinstance(command_window_start, int)
            or isinstance(command_window_end, bool)
            or not isinstance(command_window_end, int)
            or command_window_start <= 0
            or command_window_end <= command_window_start
        ):
            failures.append("profile acknowledged command-window boundaries are invalid")
        elif len(values) == len(ordered_names) and (
            command_window_start != values["start_ack_observed_ns"]
            or command_window_end != values["stop_command_sent_ns"]
        ):
            failures.append("profile acknowledged command-window boundaries do not match acknowledgements")
    return failures


def _metadata_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    return decoded if isinstance(decoded, str) else value


def _canonical_plugin_key(value: str) -> str:
    return value.replace("_", "-").replace(".", "-").lower()


def _method_matches(actual: str, expected: str) -> bool:
    return actual == expected or actual.startswith((expected + "(", expected + "@"))


def _path_contains(path: tuple[Node, ...], expected: list[str]) -> bool:
    cursor = 0
    for node in path:
        if cursor < len(expected) and _method_matches(node.method_name, expected[cursor]):
            cursor += 1
    return cursor == len(expected)


def _main_threads(profile: ProfilePayload) -> list[ThreadTree]:
    exact = [thread for thread in profile.threads if thread.name.strip().lower() in {"server thread", "main thread"}]
    if exact:
        return exact
    return [
        thread
        for thread in profile.threads
        if "server thread" in thread.name.lower() or "main thread" in thread.name.lower()
    ]


def _diagnostic_int(diagnostics: dict[str, str], key: str, failures: list[str]) -> int | None:
    value = diagnostics.get(key)
    if value is None:
        failures.append(f"missing diagnostic: {key}")
        return None
    try:
        return int(_metadata_text(value))
    except ValueError:
        failures.append(f"invalid integer diagnostic {key}: {value!r}")
        return None


def _reachable_indices(thread: ThreadTree, roots: list[int]) -> set[int]:
    reachable: set[int] = set()
    pending = list(roots)
    while pending:
        index = pending.pop()
        if index in reachable or index < 0 or index >= len(thread.nodes):
            continue
        reachable.add(index)
        pending.extend(thread.nodes[index].children_refs)
    return reachable


def validate_profile(profile: ProfilePayload, expected_seconds: int = EXPECTED_PROFILE_SECONDS) -> dict[str, Any]:
    """Validate one decoded profile without trusting controller-produced summaries."""

    failures: list[str] = []
    diagnostics = profile.extra_metadata
    duration = profile.duration_seconds
    if profile.sampler_mode != 0:
        failures.append(f"expected execution sampler mode 0, got {profile.sampler_mode}")
    if profile.interval != 4000:
        failures.append(f"expected 4ms interval (4000us), got {profile.interval}")
    if not math.isfinite(duration) or duration < expected_seconds - 5 or duration > expected_seconds + 5:
        failures.append(f"profile duration {duration:.3f}s is not within 5s of {expected_seconds}s")

    main_threads = _main_threads(profile)
    if len(main_threads) != 1:
        failures.append(f"expected exactly one main thread, found {[thread.name for thread in main_threads]}")
    main_thread = main_threads[0] if len(main_threads) == 1 else None
    root_weight = main_thread.weight if main_thread else 0.0
    if root_weight <= 0:
        failures.append("main-thread root has no positive weight")

    nodes = [node for thread in profile.threads for node in thread.nodes]
    plugin_nodes = [
        node
        for _thread, node in python_nodes(profile)
        if node.class_name == f"[Python] {EXPECTED_MODULE}"
    ]
    if not plugin_nodes:
        failures.append("profile contains no coverage-oracle Python nodes")
    elif not any(node.line_number > 0 for node in plugin_nodes):
        failures.append("coverage-oracle Python nodes have no source line")
    source = profile.class_sources.get(f"[Python] {EXPECTED_MODULE}")
    if not source or _canonical_plugin_key(source) != _canonical_plugin_key(EXPECTED_SOURCE):
        failures.append(f"coverage-oracle class source mismatch: {source!r}")

    observer_nodes = [node.method_name for node in nodes if any(token in node.method_name for token in OBSERVER_TOKENS)]
    if observer_nodes:
        failures.append(f"Spark observer/native callback names leaked into profile: {sorted(set(observer_nodes))}")

    fixed_nodes = [
        node
        for node in main_thread.nodes
        if node.class_name == f"[Python] {EXPECTED_MODULE}" and _method_matches(node.method_name, EXPECTED_FIXED)
    ] if main_thread else []
    nested_nodes = [
        node
        for node in main_thread.nodes
        if node.class_name == f"[Python] {EXPECTED_MODULE}" and _method_matches(node.method_name, EXPECTED_NESTED)
    ] if main_thread else []
    fixed_weight = sum(node.weight for node in fixed_nodes)
    nested_indices: set[int] = set()
    if main_thread:
        fixed_indices = [
            index
            for index, node in enumerate(main_thread.nodes)
            if node.class_name == f"[Python] {EXPECTED_MODULE}" and _method_matches(node.method_name, EXPECTED_FIXED)
        ]
        reachable = _reachable_indices(main_thread, fixed_indices)
        nested_indices = {
            index
            for index in reachable
            if main_thread.nodes[index].class_name == f"[Python] {EXPECTED_MODULE}"
            and _method_matches(main_thread.nodes[index].method_name, EXPECTED_NESTED)
        }
    nested_weight = sum(main_thread.nodes[index].weight for index in nested_indices) if main_thread else 0.0
    if not fixed_nodes or fixed_weight <= 0:
        failures.append("fixed_window_tick branch is missing or has zero weight")
    if not nested_nodes or nested_weight <= 0:
        failures.append("nested_call branch is missing or has zero weight")
    chain_present = False
    if main_thread:
        chain_present = any(
            thread_name == main_thread.name and _path_contains(path, [EXPECTED_FIXED, EXPECTED_NESTED])
            for thread_name, path in iter_leaf_paths(profile)
        )
    if not chain_present:
        failures.append("missing known fixed_window_tick -> nested_call chain")

    backend = _metadata_text(diagnostics.get("Python attribution backend"))
    enabled = _metadata_text(diagnostics.get("Python function attribution enabled"))
    version = _metadata_text(diagnostics.get("Python version"))
    if backend != "PEP669":
        failures.append(f"expected PEP669 backend, got {backend!r}")
    if enabled != "true":
        failures.append(f"Python function attribution is not enabled: {enabled!r}")
    if not version.startswith("3.13"):
        failures.append(f"expected CPython 3.13 profile, got {version!r}")

    values: dict[str, int] = {}
    for key, requirement in REQUIRED_DIAGNOSTICS.items():
        if requirement is None:
            continue
        value = _diagnostic_int(diagnostics, key, failures)
        if value is None:
            continue
        values[key] = value
        if requirement == 1 and value <= 0:
            failures.append(f"required positive diagnostic is zero: {key}")
        elif requirement == 0 and value != 0:
            failures.append(f"required zero diagnostic is nonzero: {key}={value}")

    attributed = values.get("Python attributed samples", 0)
    native_only = values.get("Python native-only samples", 0)
    sample_total = attributed + native_only
    if sample_total <= 0:
        failures.append("attributed/native-only sample denominator is zero")
    attributed_fraction = attributed / sample_total if sample_total else 0.0
    py_start = values.get("Python PY_START events", 0)
    snapshot_attempts = values.get("Python shadow snapshot attempts", 0)
    if py_start <= 0:
        failures.append("PY_START event count is zero")
    if snapshot_attempts <= 0:
        failures.append("shadow snapshot attempt count is zero")

    metrics = {
        "attributed_samples": attributed,
        "native_only_samples": native_only,
        "attributed_fraction": attributed_fraction,
        "fixed_window_weight": fixed_weight,
        "nested_call_weight": nested_weight,
        "fixed_window_inclusive_weight": fixed_weight,
        "nested_inclusive_weight": nested_weight,
        "nested_inclusive_over_outer_inclusive": nested_weight / fixed_weight if fixed_weight else 0.0,
        "main_thread_root_weight": root_weight,
        "fixed_window_fraction": fixed_weight / root_weight if root_weight else 0.0,
        "nested_call_present": bool(nested_nodes and nested_weight > 0),
        "chain_present": chain_present,
        "duration_seconds": duration,
        "py_start_events": py_start,
        "py_start_rate_hz": py_start / duration if duration > 0 else 0.0,
        "snapshot_attempts": snapshot_attempts,
        "plugin_node_count": len(plugin_nodes),
        "observer_node_count": len(observer_nodes),
    }
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "metrics": metrics,
        "diagnostics": {key: values.get(key, _metadata_text(diagnostics.get(key))) for key in REQUIRED_DIAGNOSTICS},
        "backend": backend,
        "python_version": version,
        "class_source": source,
    }


def _number(stats: dict[str, Any], key: str, failures: list[str]) -> float | None:
    value = stats.get(key)
    if value is None:
        failures.append(f"missing workload field: {key}")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        failures.append(f"invalid workload field {key}: {value!r}")
        return None
    if not math.isfinite(number):
        failures.append(f"non-finite workload field {key}: {value!r}")
        return None
    return number


def validate_workload(stats: dict[str, Any], expected_seconds: int = EXPECTED_PROFILE_SECONDS) -> dict[str, Any]:
    failures: list[str] = []
    alignment = stats.get("counter_alignment")
    if not isinstance(alignment, dict):
        failures.append("workload counters have no strict profile-window alignment")
        alignment = {}
    if alignment.get("method") != COUNTER_ALIGNMENT_METHOD:
        failures.append("workload counter alignment method is missing or mismatched")
    if alignment.get("clock") != COUNTER_CLOCK:
        failures.append("workload counter alignment clock is missing or mismatched")
    if alignment.get("strict") is not True:
        failures.append("workload counter alignment is not strict")
    profile_start = alignment.get("profile_start_ns")
    profile_end = alignment.get("profile_end_ns")
    if (
        isinstance(profile_start, bool)
        or not isinstance(profile_start, int)
        or isinstance(profile_end, bool)
        or not isinstance(profile_end, int)
        or profile_start <= 0
        or profile_end <= profile_start
    ):
        failures.append("workload profile boundaries are missing or invalid")
        profile_start = 0
        profile_end = 0
    source_count = alignment.get("source_window_count")
    included_count = alignment.get("included_window_count")
    excluded_count = alignment.get("excluded_window_count")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (source_count, included_count, excluded_count)):
        failures.append("workload alignment window counts are missing or non-integral")
    elif source_count < 1 or included_count < 1 or excluded_count != source_count - included_count:
        failures.append("workload alignment window counts are inconsistent")
    if stats.get("counter_scope") != COUNTER_SCOPE:
        failures.append("workload counter scope is missing or mismatched")
    counter_clock = stats.get("clock")
    if (
        not isinstance(counter_clock, dict)
        or counter_clock.get("name") != COUNTER_CLOCK
        or counter_clock.get("source") != COUNTER_CLOCK_SOURCE
        or counter_clock.get("unit") != "ns"
    ):
        failures.append("workload counter clock is missing or mismatched")
    records = stats.get("window_records")
    if not isinstance(records, list) or not records:
        failures.append("aligned workload window records are missing")
        records = []
    if stats.get("module") != EXPECTED_MODULE:
        failures.append(f"workload module mismatch: {stats.get('module')!r}")
    if stats.get("tick_method") != EXPECTED_FIXED:
        failures.append(f"workload tick method mismatch: {stats.get('tick_method')!r}")
    if stats.get("nested_method") != EXPECTED_NESTED:
        failures.append(f"workload nested method mismatch: {stats.get('nested_method')!r}")
    window_ns = _number(stats, "window_ns", failures)
    invocation_count = _number(stats, "invocation_count", failures)
    nested_count = _number(stats, "nested_call_count", failures)
    total_ns = _number(stats, "elapsed_ns_total", failures)
    active_seconds = _number(stats, "active_seconds", failures)
    rate = _number(stats, "invocation_rate_hz", failures)
    if invocation_count is not None and (
        not float(invocation_count).is_integer() or int(invocation_count) != len(records)
    ):
        failures.append("aligned window-record count does not match invocation count")
    if records and isinstance(records[0], dict) and profile_start != records[0].get("start_ns"):
        failures.append("profile start boundary does not equal first aligned window start")
    if records and isinstance(records[-1], dict) and profile_end != records[-1].get("end_ns"):
        failures.append("profile end boundary does not equal last aligned window end")
    previous_start = -1
    previous_end = -1
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            failures.append(f"aligned workload window {index} is not an object")
            continue
        try:
            start_ns = _record_int(record, "start_ns")
            end_ns = _record_int(record, "end_ns")
            elapsed_record = _record_int(record, "elapsed_ns")
            nested_record = _record_int(record, "nested_call_count")
        except RuntimeError as exc:
            failures.append(str(exc))
            continue
        if start_ns < profile_start or end_ns > profile_end or end_ns <= start_ns:
            failures.append(f"aligned workload window {index} falls outside profile boundaries")
        if elapsed_record != end_ns - start_ns or nested_record < 0:
            failures.append(f"aligned workload window {index} has inconsistent counters")
        if start_ns < previous_start or start_ns < previous_end:
            failures.append(f"aligned workload windows overlap or are unordered at {index}")
        previous_start = start_ns
        previous_end = end_ns
    if records and isinstance(records[0], dict) and alignment.get("first_included_start_ns") != records[0].get("start_ns"):
        failures.append("alignment first included timestamp does not match window records")
    if records and isinstance(records[-1], dict) and alignment.get("last_included_end_ns") != records[-1].get("end_ns"):
        failures.append("alignment last included timestamp does not match window records")
    elapsed = stats.get("elapsed_ns")
    if not isinstance(elapsed, dict):
        failures.append("missing elapsed_ns distribution")
        elapsed = {}
    elapsed_count = _number(elapsed, "count", failures)
    elapsed_min = _number(elapsed, "min", failures)
    elapsed_max = _number(elapsed, "max", failures)
    elapsed_mean = _number(elapsed, "mean", failures)
    elapsed_p50 = _number(elapsed, "p50", failures)
    elapsed_p95 = _number(elapsed, "p95", failures)
    if window_ns != WINDOW_NS:
        failures.append(f"window length mismatch: {window_ns}ns")
    if invocation_count is not None and invocation_count < expected_seconds * 10:
        failures.append(f"too few fixed-window invocations: {invocation_count:.0f}")
    if invocation_count is not None and nested_count is not None and nested_count < invocation_count * 100:
        failures.append(f"nested-call volume is too low: {nested_count:.0f} for {invocation_count:.0f} invocations")
    if invocation_count is not None and total_ns is not None and total_ns <= 0:
        failures.append("elapsed-time total is zero")
    if invocation_count is not None and elapsed_count is not None and elapsed_count != invocation_count - 0.0:
        failures.append(f"elapsed distribution count {elapsed_count} does not match invocation count {invocation_count}")
    if elapsed_min is not None and not 15_000_000 <= elapsed_min <= 100_000_000:
        failures.append(f"elapsed minimum is implausible: {elapsed_min:.0f}ns")
    if elapsed_max is not None and not 15_000_000 <= elapsed_max <= 100_000_000:
        failures.append(f"elapsed maximum is implausible: {elapsed_max:.0f}ns")
    if total_ns is not None and elapsed_count and elapsed_mean is not None:
        expected_total = elapsed_mean * elapsed_count
        if abs(total_ns - expected_total) > max(1_000_000.0, expected_total * 0.01):
            failures.append(f"elapsed total {total_ns:.0f} disagrees with distribution mean/count {expected_total:.0f}")
    if elapsed_mean is not None and not 18_000_000 <= elapsed_mean <= 22_000_000:
        failures.append(f"elapsed mean is not approximately 20ms: {elapsed_mean:.0f}ns")
    if elapsed_p50 is not None and not 18_000_000 <= elapsed_p50 <= 22_000_000:
        failures.append(f"elapsed p50 is not approximately 20ms: {elapsed_p50:.0f}ns")
    if elapsed_p95 is not None and elapsed_p95 > 100_000_000:
        failures.append(f"elapsed p95 has an unstable long tail: {elapsed_p95:.0f}ns")
    if active_seconds is not None and active_seconds < expected_seconds * 0.85:
        failures.append(f"active workload duration is too short: {active_seconds:.3f}s")
    if rate is not None and not 10.0 <= rate <= 30.0:
        failures.append(f"fixed-window invocation rate is unstable/out of range: {rate:.3f}Hz")

    intervals = stats.get("window_intervals_ns")
    interval_summary: dict[str, float] = {}
    if not isinstance(intervals, list) or len(intervals) < 2:
        failures.append("too few invocation intervals to assess scheduler stability")
    else:
        if invocation_count is not None and len(intervals) != int(invocation_count) - 1:
            failures.append(f"invocation interval count {len(intervals)} != invocation count - 1 ({invocation_count - 1:.0f})")
        try:
            numeric_intervals = [float(value) for value in intervals]
            interval_mean = statistics.fmean(numeric_intervals)
            interval_sd = statistics.stdev(numeric_intervals)
            interval_cv = interval_sd / interval_mean if interval_mean else math.inf
            interval_summary = {
                "count": float(len(numeric_intervals)),
                "mean_ns": interval_mean,
                "p50_ns": statistics.median(numeric_intervals),
                "p95_ns": sorted(numeric_intervals)[int(0.95 * (len(numeric_intervals) - 1))],
                "cv_percent": interval_cv * 100.0,
            }
            if not 30_000_000 <= interval_summary["p50_ns"] <= 100_000_000:
                failures.append(f"invocation interval p50 is unstable: {interval_summary['p50_ns']:.0f}ns")
            if interval_cv > 0.75:
                failures.append(f"invocation interval CV is unstable: {interval_cv * 100.0:.2f}%")
        except (TypeError, ValueError, statistics.StatisticsError):
            failures.append("invalid invocation interval distribution")
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "metrics": {
            "window_ns": window_ns,
            "invocation_count": invocation_count,
            "nested_call_count": nested_count,
            "elapsed_ns_total": total_ns,
            "active_seconds": active_seconds,
            "invocation_rate_hz": rate,
            "elapsed_ns": elapsed,
            "intervals": interval_summary,
            "counter_alignment": alignment,
        },
    }


def _describe(values: list[float]) -> dict[str, float]:
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "mean": mean,
        "stddev": sd,
        "min": min(values),
        "max": max(values),
    }


def _record_int(record: dict[str, Any], key: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AlignmentError(f"counter window field {key} is not an integer: {value!r}")
    return value


def _integer_percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    part = index - lower
    return round(ordered[lower] * (1.0 - part) + ordered[upper] * part)


def align_workload_counters(
    cumulative: dict[str, Any], profile_start_ns: int, profile_end_ns: int
) -> dict[str, Any]:
    """Reconstruct complete fixture windows inside an acknowledged interval."""

    if not isinstance(cumulative, dict):
        raise AlignmentError("cumulative workload counters are not an object")
    if isinstance(profile_start_ns, bool) or not isinstance(profile_start_ns, int):
        raise AlignmentError("profile start boundary is not an integer monotonic timestamp")
    if isinstance(profile_end_ns, bool) or not isinstance(profile_end_ns, int):
        raise AlignmentError("profile end boundary is not an integer monotonic timestamp")
    if profile_start_ns <= 0 or profile_end_ns <= profile_start_ns:
        raise AlignmentError("profile monotonic boundaries are invalid")
    clock = cumulative.get("clock")
    if (
        not isinstance(clock, dict)
        or clock.get("name") != COUNTER_CLOCK
        or clock.get("source") != COUNTER_CLOCK_SOURCE
        or clock.get("unit") != "ns"
    ):
        raise AlignmentError("counter clock is missing or not monotonic nanoseconds")
    records = cumulative.get("window_records")
    if not isinstance(records, list) or not records:
        raise AlignmentError("cumulative counters contain no timestamped window records")

    normalized: list[dict[str, int]] = []
    previous_start = -1
    previous_end = -1
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise AlignmentError(f"counter window {index} is not an object")
        start_ns = _record_int(raw, "start_ns")
        end_ns = _record_int(raw, "end_ns")
        elapsed_ns = _record_int(raw, "elapsed_ns")
        nested_calls = _record_int(raw, "nested_call_count")
        if start_ns <= 0 or end_ns <= start_ns or elapsed_ns != end_ns - start_ns:
            raise AlignmentError(f"counter window {index} has invalid timestamps or elapsed duration")
        if nested_calls < 0:
            raise AlignmentError(f"counter window {index} has a negative nested-call count")
        if start_ns < previous_start or start_ns < previous_end:
            raise AlignmentError(f"counter window {index} is not ordered and non-overlapping")
        normalized.append(
            {
                "start_ns": start_ns,
                "end_ns": end_ns,
                "elapsed_ns": elapsed_ns,
                "nested_call_count": nested_calls,
            }
        )
        previous_start = start_ns
        previous_end = end_ns

    selected = [
        record
        for record in normalized
        if record["start_ns"] >= profile_start_ns and record["end_ns"] <= profile_end_ns
    ]
    if not selected:
        raise AlignmentError("no complete fixture windows fall inside the profiler boundaries")
    durations = [record["elapsed_ns"] for record in selected]
    starts = [record["start_ns"] for record in selected]
    first_start = selected[0]["start_ns"]
    last_end = selected[-1]["end_ns"]
    active_seconds = (last_end - first_start) / 1_000_000_000.0
    if active_seconds <= 0:
        raise AlignmentError("aligned fixture window duration is zero")
    alignment = {
        "method": COUNTER_ALIGNMENT_METHOD,
        "clock": COUNTER_CLOCK,
        "profile_start_ns": profile_start_ns,
        "profile_end_ns": profile_end_ns,
        "source_window_count": len(normalized),
        "included_window_count": len(selected),
        "excluded_window_count": len(normalized) - len(selected),
        "first_included_start_ns": first_start,
        "last_included_end_ns": last_end,
        "strict": True,
    }
    return {
        "module": cumulative.get("module"),
        "tick_method": cumulative.get("tick_method"),
        "nested_method": cumulative.get("nested_method"),
        "window_ns": cumulative.get("window_ns"),
        "invocation_count": len(selected),
        "nested_call_count": sum(record["nested_call_count"] for record in selected),
        "elapsed_ns_total": sum(durations),
        "elapsed_ns": {
            "count": len(durations),
            "min": min(durations),
            "max": max(durations),
            "mean": sum(durations) / len(durations),
            "p50": _integer_percentile(durations, 0.50),
            "p95": _integer_percentile(durations, 0.95),
        },
        "active_seconds": active_seconds,
        "invocation_rate_hz": len(selected) / active_seconds,
        "window_intervals_ns": [current - previous for previous, current in pairwise(starts)],
        "clock": clock,
        "counter_scope": COUNTER_SCOPE,
        "counter_alignment": alignment,
        "window_records": selected,
    }


def paired_t_interval(values: list[float], confidence: float = 0.95) -> dict[str, Any]:
    del confidence  # The registered oracle uses the explicit 95% tables below.
    n = len(values)
    if n < 2:
        return {"status": "INCONCLUSIVE", "reason": "fewer than two paired observations", "n": n}
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    if not math.isfinite(sd):
        return {"status": "INCONCLUSIVE", "reason": "paired variance is non-finite", "n": n, "mean": mean}
    df = n - 1
    two_sided_t = TWO_SIDED_95.get(df, 1.96)
    one_sided_t = ONE_SIDED_95.get(df, 1.645)
    if sd == 0:
        return {
            "status": "RESOLVED",
            "n": n,
            "df": df,
            "mean": mean,
            "stddev": 0.0,
            "standard_error": 0.0,
            "two_sided_95": {"lower": mean, "upper": mean, "t": two_sided_t},
            "one_sided_95_lower": mean,
            "bound_used": "two_sided_95_lower",
            "degenerate": True,
        }
    standard_error = sd / math.sqrt(n)
    two_sided_half_width = two_sided_t * standard_error
    return {
        "status": "RESOLVED",
        "n": n,
        "df": df,
        "mean": mean,
        "stddev": sd,
        "standard_error": standard_error,
        "two_sided_95": {"lower": mean - two_sided_half_width, "upper": mean + two_sided_half_width, "t": two_sided_t},
        "one_sided_95_lower": mean - one_sided_t * standard_error,
        "bound_used": "two_sided_95_lower",
    }


def assess_noninferiority(values: list[float], margin: float, expected_n: int = 5) -> dict[str, Any]:
    interval = paired_t_interval(values)
    result: dict[str, Any] = {"margin": margin, "deltas": values, "interval": interval}
    if len(values) != expected_n or interval.get("status") != "RESOLVED":
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "sample size or paired variance cannot resolve the estimand"
        return result
    lower = float(interval["two_sided_95"]["lower"])
    upper = float(interval["two_sided_95"]["upper"])
    if lower > margin:
        result["status"] = "PASS"
    elif upper <= margin:
        result["status"] = "FAIL"
    else:
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "95% interval crosses the non-inferiority margin"
    result["lower_95_bound"] = lower
    result["upper_95_bound"] = upper
    return result


def _expected_rows(repetitions: int) -> set[tuple[int, str]]:
    return {(rep, target) for rep in range(1, repetitions + 1) for target in ("parent", "candidate")}


def parse_case_status(path: pathlib.Path, repetitions: int) -> tuple[list[dict[str, Any]], list[str]]:
    problems: list[str] = []
    if not path.is_file():
        return [], ["missing case-status.tsv"]
    rows: list[dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != CASE_STATUS_HEADER:
        problems.append("case-status.tsv header mismatch")
    for line_number, line in enumerate(lines, 1):
        if line_number == 1:
            continue
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            problems.append(f"malformed case-status row {line_number}: {line!r}")
            continue
        try:
            row = {
                "rep": int(fields[0]),
                "target": fields[1],
                "sha": fields[2],
                "label": fields[3],
                "controller_exit_code": int(fields[4]),
                "status": fields[5],
                "extra": fields[6],
            }
        except ValueError:
            problems.append(f"invalid case-status row {line_number}: {line!r}")
            continue
        rows.append(row)
    actual = {(row["rep"], row["target"]) for row in rows}
    expected = _expected_rows(repetitions)
    if actual != expected:
        problems.append(f"case set mismatch missing={sorted(expected - actual)} extra={sorted(actual - expected)}")
    labels = {row["label"] for row in rows}
    if len(labels) != len(rows):
        problems.append("case-status contains duplicate labels")
    for row in rows:
        expected_label = f"rep{row['rep']}-{row['target']}"
        if row["label"] != expected_label:
            problems.append(f"unexpected case label {row['label']!r}, expected {expected_label!r}")
    return rows, problems


def _boundary_timestamp(boundaries: dict[str, Any], key: str) -> int:
    value = boundaries.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AlignmentError(f"profile boundary {key} is not an integer monotonic timestamp")
    return value


def _read_case(root: pathlib.Path, row: dict[str, Any], expected_sha: dict[str, str], expected_seconds: int) -> tuple[dict[str, Any] | None, list[str], str | None]:
    label = row["label"]
    case = root / label
    problems: list[str] = []
    target = row["target"]
    if target not in expected_sha:
        problems.append(f"{label}: unexpected target {target!r}")
        return None, problems, None
    if row["sha"] != expected_sha.get(target):
        problems.append(f"{label}: status SHA {row['sha']} != expected {expected_sha.get(target)}")
    if row["controller_exit_code"] != 0 or row["status"] != "PASS":
        problems.append(f"{label}: controller exit={row['controller_exit_code']} status={row['status']}")
    result_path = case / "python-native-bridge-coverage-result.json"
    metadata_path = case / "metadata.json"
    profile_path = case / "python-native-bridge-coverage.sparkprofile"
    counters_path = case / "coverage-counters.json"
    cumulative_counters_path = case / "coverage-counters-cumulative.json"
    boundaries_path = case / "profile-window-boundaries.json"
    for path in (result_path, metadata_path, profile_path, counters_path, cumulative_counters_path, boundaries_path):
        if not path.is_file() or path.stat().st_size == 0:
            problems.append(f"{label}: missing or empty {path.name}")
    required_paths = (result_path, metadata_path, profile_path, counters_path, cumulative_counters_path, boundaries_path)
    if problems and not all(path.is_file() and path.stat().st_size > 0 for path in required_paths):
        return None, problems, None
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        counters = json.loads(counters_path.read_text(encoding="utf-8"))
        cumulative_counters = json.loads(cumulative_counters_path.read_text(encoding="utf-8"))
        boundaries = json.loads(boundaries_path.read_text(encoding="utf-8"))
        profile = parse_sampler_data(profile_path.read_bytes())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        problems.append(f"{label}: evidence decode failed: {type(exc).__name__}: {exc}")
        return None, problems, None
    if not isinstance(result, dict):
        problems.append(f"{label}: controller result is not a JSON object")
        return None, problems, None
    if not isinstance(metadata, dict):
        problems.append(f"{label}: metadata is not a JSON object")
        return None, problems, None
    if not isinstance(counters, dict):
        problems.append(f"{label}: workload counters are not a JSON object")
        return None, problems, None
    if not isinstance(cumulative_counters, dict):
        problems.append(f"{label}: cumulative workload counters are not a JSON object")
    if not isinstance(boundaries, dict):
        problems.append(f"{label}: profile boundaries are not a JSON object")
    components = metadata.get("components") or {}
    if not isinstance(components, dict):
        problems.append(f"{label}: metadata components are not a JSON object")
        return None, problems, None
    spark_component = components.get("spark") or {}
    endstone = components.get("endstone") or {}
    if not isinstance(spark_component, dict) or not isinstance(endstone, dict):
        problems.append(f"{label}: metadata component entries are not JSON objects")
        return None, problems, None
    spark_sha = str(spark_component.get("sha") or "")
    endstone_sha = str(endstone.get("sha") or "")
    artifact = endstone.get("artifact") or {}
    artifact_name = str(artifact.get("name") or "") if isinstance(artifact, dict) else ""
    if spark_sha != expected_sha[target]:
        problems.append(f"{label}: metadata Spark SHA {spark_sha} != expected {expected_sha[target]}")
    if not endstone_sha:
        problems.append(f"{label}: missing Endstone SHA")
    if "cp313" not in artifact_name.lower():
        problems.append(f"{label}: Endstone artifact is not CPython 3.13: {artifact_name!r}")
    if result.get("status") != "PASS":
        problems.append(f"{label}: controller result status is {result.get('status')!r}")
    alignment = counters.get("counter_alignment")
    if isinstance(boundaries, dict) and isinstance(alignment, dict):
        boundary_failures = validate_profile_boundaries(boundaries)
        problems.extend(f"{label}: boundary: {failure}" for failure in boundary_failures)
        if any(boundaries.get(key) != alignment.get(key) for key in ("clock", "profile_start_ns", "profile_end_ns")):
            problems.append(f"{label}: aligned counter boundaries do not match boundary evidence")
        if boundaries.get("clock_source") != COUNTER_CLOCK_SOURCE:
            problems.append(f"{label}: profile boundary clock source is missing or mismatched")
        if result.get("profile_boundaries") != boundaries:
            problems.append(f"{label}: result profile boundaries do not match boundary evidence")
    if isinstance(boundaries, dict):
        try:
            expected_counters = align_workload_counters(
                cumulative_counters,
                _boundary_timestamp(boundaries, "profile_start_ns"),
                _boundary_timestamp(boundaries, "profile_end_ns"),
            )
        except RuntimeError as exc:
            problems.append(f"{label}: cannot reconstruct aligned workload counters: {exc}")
        else:
            if counters != expected_counters:
                problems.append(f"{label}: aligned workload counters do not exactly match cumulative records and boundaries")
    profile_report = validate_profile(profile, expected_seconds)
    workload_report = validate_workload(counters, expected_seconds)
    if profile_report["status"] != "PASS":
        problems.extend(f"{label}: profile: {failure}" for failure in profile_report["failures"])
    if workload_report["status"] != "PASS":
        problems.extend(f"{label}: workload: {failure}" for failure in workload_report["failures"])
    claimed = result.get("oracle")
    if not isinstance(claimed, dict) or claimed.get("status") != "PASS":
        problems.append(f"{label}: controller did not claim a PASS oracle result")
    if problems:
        return None, problems, endstone_sha
    return {
        "label": label,
        "rep": row["rep"],
        "target": target,
        "spark_sha": spark_sha,
        "endstone_sha": endstone_sha,
        "profile": profile_report,
        "workload": workload_report,
    }, problems, endstone_sha


def analyze_paired_evidence(root: pathlib.Path, parent_sha: str, candidate_sha: str, repetitions: int = 5) -> dict[str, Any]:
    expected_sha = {"parent": parent_sha, "candidate": candidate_sha}
    rows, problems = parse_case_status(root / "case-status.tsv", repetitions)
    reports: list[dict[str, Any]] = []
    endstone_shas: set[str] = set()
    for row in rows:
        report, case_problems, endstone_sha = _read_case(root, row, expected_sha, EXPECTED_PROFILE_SECONDS)
        problems.extend(case_problems)
        if endstone_sha:
            endstone_shas.add(endstone_sha)
        if report is not None:
            reports.append(report)
    if len(endstone_shas) != 1:
        problems.append(f"Endstone SHA drift or missing across paired run: {sorted(endstone_shas)}")

    by_key = {(report["rep"], report["target"]): report for report in reports}
    estimands: dict[str, dict[str, Any]] = {}
    for name, field, margin in (
        ("attributed_fraction", "attributed_fraction", -0.01),
        ("fixed_window_fraction", "fixed_window_fraction", -0.02),
        ("nested_inclusive_over_outer_inclusive", "nested_inclusive_over_outer_inclusive", -0.02),
    ):
        deltas: list[float] = []
        for rep in range(1, repetitions + 1):
            parent = by_key.get((rep, "parent"))
            candidate = by_key.get((rep, "candidate"))
            if parent is None or candidate is None:
                continue
            deltas.append(float(candidate["profile"]["metrics"][field]) - float(parent["profile"]["metrics"][field]))
        estimand = assess_noninferiority(deltas, margin, repetitions)
        estimand["margin_percentage_points"] = margin * 100.0
        estimand["deltas_percentage_points"] = [value * 100.0 for value in deltas]
        estimand["summary"] = _describe(deltas) if deltas else None
        estimand["summary_percentage_points"] = (
            {key: value * 100.0 for key, value in estimand["summary"].items()} if estimand["summary"] else None
        )
        estimands[name] = estimand

    if problems or any(item["status"] == "FAIL" for item in estimands.values()):
        status = "FAIL"
    elif any(item["status"] == "INCONCLUSIVE" for item in estimands.values()):
        status = "INCONCLUSIVE"
    else:
        status = "PASS"
    return {
        "status": status,
        "correctness_only": True,
        "performance_claim": False,
        "parent_sha": parent_sha,
        "candidate_sha": candidate_sha,
        "repetitions": repetitions,
        "expected_case_count": repetitions * 2,
        "observed_case_count": len(rows),
        "endstone_shas": sorted(endstone_shas),
        "preregistered_estimands": {
            "attributed_fraction": {"field": "candidate_minus_parent", "margin": -0.01, "margin_percentage_points": -1.0},
            "fixed_window_fraction": {"field": "candidate_minus_parent", "margin": -0.02, "margin_percentage_points": -2.0},
            "nested_inclusive_over_outer_inclusive": {
                "field": "candidate_minus_parent",
                "margin": -0.02,
                "margin_percentage_points": -2.0,
                "source": "nested_call_inclusive_weight / fixed_window_tick_inclusive_weight",
            },
        },
        "estimands": estimands,
        "case_reports": reports,
        "problems": problems,
    }


PLUGIN_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "spark-python-coverage-oracle"


class CoverageOracleRun(IntegrationTest):
    def __init__(self, platform_name: str, profile_seconds: int, warmup_seconds: int) -> None:
        super().__init__(platform_name)
        self.profile_seconds = profile_seconds
        self.warmup_seconds = warmup_seconds
        self.coverage_result_path = self.root / "python-native-bridge-coverage-result.json"
        self.profile_path = self.root / "python-native-bridge-coverage.sparkprofile"
        self.summary_path = self.root / "python-native-bridge-coverage-summary.json"
        self.counters_path = self.root / "coverage-counters.json"
        self.cumulative_counters_path = self.root / "coverage-counters-cumulative.json"
        self.profile_boundaries_path = self.root / "profile-window-boundaries.json"
        self.profile_boundaries: dict[str, Any] | None = None
        self.result.update(
            {
                "test_kind": "spark-python-native-bridge-coverage-oracle",
                "python_version": sys.version.split()[0],
                "profile_seconds": profile_seconds,
                "warmup_seconds": warmup_seconds,
                "spark_profile_viewer_url": None,
                "profile_boundaries": None,
                "oracle": None,
            }
        )
        self._write_outputs()

    def _write_outputs(self) -> None:
        write_json(self.result_path, self.result)
        write_json(self.coverage_result_path, self.result)

    def check(self, name: str, status: str, detail: str | None = None, **extra: Any) -> None:
        super().check(name, status, detail, **extra)
        self._write_outputs()

    def install_artifacts(self) -> None:
        IntegrationTest.install_artifacts(self)
        expected = os.environ.get("EXPECTED_SPARK_SHA", "").strip()
        observed = str((self.metadata.get("components", {}).get("spark") or {}).get("sha") or "")
        if not expected or observed != expected:
            raise RuntimeError(f"resolved Spark SHA {observed!r} does not match EXPECTED_SPARK_SHA {expected!r}")
        self.check("exact-spark-sha", "PASS", observed)
        wheel_dir = self.root / "coverage-wheel"
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
        wheels = sorted(wheel_dir.glob("endstone_spark_python_coverage_oracle-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected one coverage-oracle plugin wheel, got: {wheels}")
        plugin_dir = self.server_dir / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        target = plugin_dir / wheels[0].name
        shutil.copy2(wheels[0], target)
        self.check("coverage-oracle-plugin-installed", "PASS", str(target.relative_to(self.root)))

    def wait_plugin(self) -> None:
        assert self.server is not None
        self.server.wait_for(
            lambda lines: any("spark python coverage oracle enabled" in line.lower() for line in lines),
            30,
            "Python coverage oracle plugin enable",
        )
        self.check("coverage-oracle-plugin-enabled", "PASS")

    def capture_profile(self) -> dict[str, Any]:
        assert self.server is not None
        command = "spark profiler start --interval 4"
        start_command_sent_ns = time.perf_counter_ns()
        start = self.server.command(command)
        start_ack_output, start_ack_observed_ns = _wait_command_ack(
            self.server, start, _line_contains(START_ACK_TOKEN), timeout=8.0
        )
        start_ack_line = next(line for line in start_ack_output if START_ACK_TOKEN in line.lower())
        start_output = self.server.wait_command_output(start, timeout=8.0)
        if any("unknown command" in line.lower() or "command not found" in line.lower() for line in start_output):
            raise RuntimeError("Spark profiler start command was rejected")
        if not any(START_ACK_TOKEN in line.lower() for line in start_output):
            raise RuntimeError("Spark profiler start acknowledgement was not observable")
        profile_start_ns = start_ack_observed_ns
        deadline_ns = profile_start_ns + self.profile_seconds * 1_000_000_000
        while time.perf_counter_ns() < deadline_ns:
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during coverage oracle profile")
            remaining = max(0.05, (deadline_ns - time.perf_counter_ns()) / 1_000_000_000.0)
            time.sleep(min(1.0, remaining))
        stop_command_sent_ns = time.perf_counter_ns()
        stop_at = self.server.command("spark profiler stop")
        stop_request_output, stop_request_ack_observed_ns = _wait_command_ack(
            self.server, stop_at, _line_contains(STOP_REQUEST_ACK_TOKEN), timeout=8.0
        )
        stop_request_ack_line = next(line for line in stop_request_output if STOP_REQUEST_ACK_TOKEN in line.lower())
        stop_output = self.server.wait_command_output(stop_at, timeout=8.0)
        if any("unknown command" in line.lower() or "command not found" in line.lower() for line in stop_output):
            raise RuntimeError("Spark profiler stop command was rejected")
        if not any(STOP_REQUEST_ACK_TOKEN in line.lower() for line in stop_output):
            raise RuntimeError("Spark profiler stop acknowledgement was not observable")
        stop_complete_output, stop_complete_ack_observed_ns = _wait_command_ack(
            self.server, stop_at, _line_contains(STOP_COMPLETE_ACK_TOKEN), timeout=90.0
        )
        stop_complete_ack_line = next(line for line in stop_complete_output if STOP_COMPLETE_ACK_TOKEN in line.lower())
        stop_output = self.server.wait_command_output(stop_at, timeout=8.0)
        if not any(STOP_COMPLETE_ACK_TOKEN in line.lower() for line in stop_output):
            raise RuntimeError("Spark profiler stop completion acknowledgement was not observable")
        stop_command_flushed_ns = time.perf_counter_ns()
        self.profile_boundaries = {
            "clock": COUNTER_CLOCK,
            "clock_source": COUNTER_CLOCK_SOURCE,
            "profile_start_ns": profile_start_ns,
            "profile_end_ns": stop_command_sent_ns,
            "start_command_sent_ns": start_command_sent_ns,
            "start_ack_observed_ns": start_ack_observed_ns,
            "stop_command_sent_ns": stop_command_sent_ns,
            "stop_request_ack_observed_ns": stop_request_ack_observed_ns,
            "stop_complete_ack_observed_ns": stop_complete_ack_observed_ns,
            "stop_command_flushed_ns": stop_command_flushed_ns,
            "start_command_output_lines": len(start_output),
            "stop_command_output_lines": len(stop_output),
            "start_acknowledgement": START_ACK_TOKEN,
            "start_ack_line": start_ack_line,
            "stop_request_acknowledgement": STOP_REQUEST_ACK_TOKEN,
            "stop_request_ack_line": stop_request_ack_line,
            "stop_complete_acknowledgement": STOP_COMPLETE_ACK_TOKEN,
            "stop_complete_ack_line": stop_complete_ack_line,
            "method": "observable Spark acknowledgements plus complete fixture record boundaries",
            "strict": True,
        }
        write_json(self.profile_boundaries_path, self.profile_boundaries)
        deadline = time.monotonic() + 90
        url: str | None = None
        while time.monotonic() < deadline:
            url = self._viewer_url(self.server.snapshot(), start)
            if url:
                break
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during coverage oracle profile")
            time.sleep(1)
        if not url:
            raise RuntimeError("coverage oracle profile produced no viewer URL")
        raw = fetch_viewer_payload(url)
        if len(raw) < 64:
            raise RuntimeError(f"coverage oracle profile payload is unexpectedly small: {len(raw)} bytes")
        self.profile_path.write_bytes(raw)
        profile = parse_sampler_data(raw)
        report = validate_profile(profile, self.profile_seconds)
        assert self.profile_boundaries is not None
        self.profile_boundaries["profile_start_time_ms"] = profile.start_time_ms
        self.profile_boundaries["profile_end_time_ms"] = profile.end_time_ms
        self.profile_boundaries["profile_metric_duration_seconds"] = profile.duration_seconds
        write_json(self.profile_boundaries_path, self.profile_boundaries)
        self.result["profile_boundaries"] = self.profile_boundaries
        self.result["spark_profile_viewer_url"] = url
        self.result["profile_validation"] = report
        self.summary_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        self._write_outputs()
        if report["status"] != "PASS":
            raise RuntimeError("profile correctness oracle failed: " + "; ".join(report["failures"]))
        return report

    def execute(self) -> int:
        stage = "initialization"
        os.environ["SPARK_PYTHON_ATTRIBUTION_MODE"] = "auto"
        os.environ["SPARK_PYTHON_COVERAGE_STATS"] = str(self.cumulative_counters_path)
        try:
            stage = "artifact-install"
            self.install_artifacts()
            stage = "server-start"
            self.start_server()
            stage = "plugin-enable"
            self.wait_plugin()
            stage = "warmup"
            time.sleep(self.warmup_seconds)
            stage = "profile"
            profile_report = self.capture_profile()
            stage = "shutdown"
            self.shutdown()
            stage = "workload-validation"
            if not self.cumulative_counters_path.is_file():
                raise RuntimeError("coverage fixture did not export cumulative coverage counters")
            if self.profile_boundaries is None:
                raise RuntimeError("profile boundaries were not recorded; workload alignment is unproven")
            cumulative = json.loads(self.cumulative_counters_path.read_text(encoding="utf-8"))
            command_start_ns = _boundary_timestamp(self.profile_boundaries, "profile_start_ns")
            command_end_ns = _boundary_timestamp(self.profile_boundaries, "profile_end_ns")
            counters = align_workload_counters(
                cumulative,
                command_start_ns,
                command_end_ns,
            )
            alignment = counters["counter_alignment"]
            effective_start_ns = _boundary_timestamp(alignment, "first_included_start_ns")
            effective_end_ns = _boundary_timestamp(alignment, "last_included_end_ns")
            self.profile_boundaries.update(
                {
                    "command_window_start_ns": command_start_ns,
                    "command_window_end_ns": command_end_ns,
                    "profile_start_ns": effective_start_ns,
                    "profile_end_ns": effective_end_ns,
                    "effective_interval_source": "first and last complete fixture window inside acknowledged Spark session",
                }
            )
            if self.profile_boundaries["profile_start_ns"] != alignment["first_included_start_ns"] or self.profile_boundaries[
                "profile_end_ns"
            ] != alignment["last_included_end_ns"]:
                raise RuntimeError("effective profile boundaries do not match aligned fixture records")
            boundary_failures = validate_profile_boundaries(self.profile_boundaries)
            if boundary_failures:
                raise RuntimeError("profile boundary validation failed: " + "; ".join(boundary_failures))
            write_json(self.profile_boundaries_path, self.profile_boundaries)
            self.result["profile_boundaries"] = self.profile_boundaries
            self._write_outputs()
            counters = align_workload_counters(
                cumulative,
                effective_start_ns,
                effective_end_ns,
            )
            write_json(self.counters_path, counters)
            self.result["workload_counter_alignment"] = counters["counter_alignment"]
            workload_report = validate_workload(counters, self.profile_seconds)
            self.result["workload_validation"] = workload_report
            self.result["oracle"] = {
                "status": "PASS" if workload_report["status"] == "PASS" and profile_report["status"] == "PASS" else "FAIL",
                "profile": profile_report,
                "workload": workload_report,
            }
            if workload_report["status"] != "PASS":
                raise RuntimeError("workload correctness oracle failed: " + "; ".join(workload_report["failures"]))
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            return 0
        except (KeyError, OSError, RuntimeError, TypeError, ValueError, TimeoutError) as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "failed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"
            try:
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.server.close()
            except (OSError, RuntimeError):
                pass
            return 1
        finally:
            self.result["completed_at"] = now_iso()
            self.split_logs()
            self._write_outputs()


def run_live(args: argparse.Namespace) -> int:
    validator = CoverageOracleRun(args.platform, args.profile_seconds, args.warmup_seconds)
    code = validator.execute()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


def run_analysis(args: argparse.Namespace) -> int:
    report = analyze_paired_evidence(args.root, args.parent_sha, args.candidate_sha, args.repetitions)
    output = args.output or args.root / "coverage-oracle-summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    return 0 if report["status"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("evidence"))
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--parent-sha", default=PARENT_SHA)
    parser.add_argument("--candidate-sha", default=CANDIDATE_SHA)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--platform", choices=["linux", "windows"])
    parser.add_argument("--profile-seconds", type=int, default=EXPECTED_PROFILE_SECONDS)
    parser.add_argument("--warmup-seconds", type=int, default=EXPECTED_WARMUP_SECONDS)
    args = parser.parse_args()
    if args.analyze:
        return run_analysis(args)
    if args.platform is None:
        parser.error("--platform is required unless --analyze is selected")
    if args.platform != "linux":
        parser.error("the fixed correctness oracle is registered for Linux CPython 3.13 only")
    if args.profile_seconds != EXPECTED_PROFILE_SECONDS or args.warmup_seconds != EXPECTED_WARMUP_SECONDS:
        parser.error("the correctness oracle requires exactly 30s warmup and 60s profiling")
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
