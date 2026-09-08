from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import json
import math
import mmap
import multiprocessing
import os
import pathlib
import signal
import struct
import subprocess
import sys
import time
import traceback
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

import psutil

from controller.bot_validation import list_players
from controller.ci_diagnostics import read_ci_diagnostics
from controller.combined_pack_gamerule_fleet_exact_runner import (
    _FrameworkShutdownServerProcess,
)
from controller.fleet_spark_validation import PLAYER_COUNT_RE, FleetBotProcess
from controller.python_evidence_provenance import validate_bds_version
from controller.run_test import (
    READY_HINTS,
    SPARK_LOAD_HINTS,
    IntegrationTest,
    now_iso,
    write_json,
)
from controller.spark51_windows_bds_runner import (
    ALLOCATION_INTERVAL_BYTES,
    ALLOCATION_KIND,
    BOT_COUNT,
    CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT,
    CPU_BASELINE_KIND,
    CPU_LOAD_KIND,
    SPARK_CANDIDATE_SHA,
    Spark51WindowsBdsValidation,
    _ordered_reload_evidence,
    _select_live_bds_identity,
)

EXPERIMENT_TIMEOUT_SECONDS = 25 * 60
HARD_DEADLINE_SECONDS = EXPERIMENT_TIMEOUT_SECONDS
WORK_DEADLINE_SECONDS = 23 * 60
SHUTDOWN_RESERVE_SECONDS = 120.0
WORK_TIMEOUT_SECONDS = WORK_DEADLINE_SECONDS
EMERGENCY_TERMINATION_SECONDS = 1480.0
OBSERVER_ZERO_DEADLINE_SECONDS = 1485.0
CONTROLLER_ZERO_DEADLINE_SECONDS = 1495.0
SUPERVISOR_FAILURE_DEADLINE_SECONDS = 1500.0
RELOAD_REPETITIONS = 10
RELOADS_PER_REPETITION = 3
TOTAL_RELOADS = RELOAD_REPETITIONS * RELOADS_PER_REPETITION
POST_RELOAD_PROFILES = RELOAD_REPETITIONS
POST_RELOAD_PROFILE_COMMAND = "spark profiler start --timeout 30 --interval 4"
POST_RELOAD_PROFILE_KIND_PREFIX = "post-reload-"
OBSERVER_SAMPLE_SECONDS = 1.0
OBSERVER_STALL_SAMPLES = 5
OBSERVER_RING_SIZE = 60
OBSERVER_CAPTURE_DELAY_SECONDS = 2.0
OBSERVER_CAPTURE_MAX_BYTES = 512 * 1024
RELOAD_MAPPING_GRACE_SECONDS = 15.0
RELOAD_WAIT_SECONDS = 60.0
PROFILE_COMPLETION_GRACE_SECONDS = 90.0
PROFILE_STOP_WAIT_SECONDS = 60.0
REPETITION_DEADLINE_SECONDS = 90.0
REPETITION_WORST_CASE_SECONDS = REPETITION_DEADLINE_SECONDS
CONTROL_BLOCK_SIZE = 4096
CONTROL_MAGIC = b"SPK51CTL"
CONTROL_SCHEMA_VERSION = 2
CONTROL_SUPERVISOR_STARTING = 1
CONTROL_SUPERVISOR_RUNNING = 2
CONTROL_SUPERVISOR_FAILURE = 3
CONTROL_SUPERVISOR_OBSERVER_DEATH_CONFIRMED = 4
CONTROL_SUPERVISOR_COMPLETE = 5
CONTROL_SUPERVISOR_FORCED_OBSERVER_FAILURE = 6
CONTROL_SUPERVISOR_EMERGENCY = 7
CONTROL_CONTROLLER_STARTING = 1
CONTROL_CONTROLLER_RUNNING = 2
CONTROL_CONTROLLER_NORMAL_OBSERVER_STOP_REQUEST = 3
CONTROL_CONTROLLER_FAILURE = 4
CONTROL_CONTROLLER_COMPLETE = 5
CONTROL_OBSERVER_WAITING = 1
CONTROL_OBSERVER_READY = 2
CONTROL_OBSERVER_FAILURE = 3
CONTROL_OBSERVER_TRIGGERED = 4
CONTROL_OBSERVER_STOPPED = 5
CONTROL_OFFSET_SUPERVISOR_STATUS = 12
CONTROL_OFFSET_CONTROLLER_STATUS = 16
CONTROL_OFFSET_OBSERVER_STATUS = 20
CONTROL_OFFSET_ORIGIN_NS = 24
CONTROL_OFFSET_WORK_DEADLINE_NS = 32
CONTROL_OFFSET_HARD_DEADLINE_NS = 40
CONTROL_OFFSET_EMERGENCY_NS = 48
CONTROL_OFFSET_OBSERVER_ZERO_NS = 56
CONTROL_OFFSET_CONTROLLER_ZERO_NS = 64
CONTROL_OFFSET_REPETITION_DEADLINE_NS = 72
CONTROL_OFFSET_CONTROLLER_PID = 80
CONTROL_OFFSET_OBSERVER_PID = 84
CONTROL_OFFSET_SUPERVISOR_UPDATE_NS = 88
CONTROL_OFFSET_CONTROLLER_UPDATE_NS = 96
CONTROL_OFFSET_OBSERVER_UPDATE_NS = 104
CONTROL_OFFSET_REPETITION = 112
CONTROL_OFFSET_PHASE = 116
EXPECTED_SPARK_ARTIFACT_ID = 10056904202
EXPECTED_SPARK_ARTIFACT_DIGEST = "sha256:2d6e7c2bd82b110ffc2f5d347624e42d0e96301507be2ee42aab9f75cb9e41f6"
EXPECTED_SPARK_ARTIFACT_RUN_ID = 34228410211
EXPECTED_SPARK_WORKFLOW_PATH = ".github/workflows/build.yml"
EXPECTED_SPARK_REPOSITORY = "ReallocAll/spark"
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
_OBSERVER_STATE_FILE = "observer-state.json"
_OBSERVER_COMMAND_FILE = "observer-command.json"
_OBSERVER_RESULT_FILE = "observer-result.json"
_OBSERVER_EVIDENCE_FILE = "post-reload-diagnostic.json"
_HEARTBEAT_MARKER = "kind=ci-lifecycle-heartbeat"
_REQUIRED_CONTEXTS = ("ApplicationTick", "PluginTick")
_HEARTBEAT_FIELDS = (
    "generation",
    "phase",
    "task_id",
    "callback_seq",
    "is_sync",
    "is_cancelled",
    "command_pending",
    "dispatch_result",
)


class DiagnosticAvailabilityError(RuntimeError):
    pass


class DeadlineExceeded(RuntimeError):
    pass


def clipped_timeout(requested: float, deadline: float, *, now: float | None = None) -> float:
    """Return a positive timeout that cannot cross deadline."""

    try:
        requested_value = float(requested)
        deadline_value = float(deadline)
        now_value = time.monotonic() if now is None else float(now)
    except (TypeError, ValueError) as exc:
        raise DeadlineExceeded("invalid deadline timeout") from exc
    remaining = deadline_value - now_value
    if not math.isfinite(requested_value) or requested_value <= 0:
        raise DeadlineExceeded(f"invalid timeout {requested!r}")
    if not math.isfinite(remaining) or remaining <= 0:
        raise DeadlineExceeded("controller deadline expired")
    return min(requested_value, remaining)


def budget_allows_next_repetition(
    now: float,
    deadline: float,
    reserve: float | None = None,
    worst_case: float | None = None,
) -> bool:
    if reserve is None and worst_case is None:
        reserve = 0.0
        worst_case = REPETITION_DEADLINE_SECONDS
    elif reserve is None or worst_case is None:
        return False
    try:
        now_value = float(now)
        deadline_value = float(deadline)
        reserve_value = float(reserve)
        worst_case_value = float(worst_case)
    except (TypeError, ValueError):
        return False
    return all(
        math.isfinite(value) and value >= 0
        for value in (now_value, deadline_value, reserve_value, worst_case_value)
    ) and deadline_value - now_value > reserve_value + worst_case_value


def admit_repetition(now: float, work_deadline: float) -> tuple[float, float]:
    """Return the aggregate repetition deadline and effective deadline."""

    try:
        now_value = float(now)
        work_value = float(work_deadline)
    except (TypeError, ValueError) as exc:
        raise DeadlineExceeded("invalid repetition deadline") from exc
    if not math.isfinite(now_value) or not math.isfinite(work_value):
        raise DeadlineExceeded("invalid repetition deadline")
    repetition_deadline = now_value + REPETITION_DEADLINE_SECONDS
    if not repetition_deadline < work_value:
        raise DeadlineExceeded("repetition cannot fit before the work deadline")
    return repetition_deadline, min(repetition_deadline, work_value)


def next_observer_tick(scheduled: float, observed: float, interval: float = OBSERVER_SAMPLE_SECONDS) -> float:
    """Advance one interval and fail instead of emitting catch-up samples."""

    try:
        scheduled_value = float(scheduled)
        observed_value = float(observed)
        interval_value = float(interval)
    except (TypeError, ValueError) as exc:
        raise DeadlineExceeded("invalid observer cadence") from exc
    if not all(math.isfinite(value) and value > 0 for value in (interval_value,)):
        raise DeadlineExceeded("invalid observer cadence")
    if observed_value - scheduled_value > interval_value:
        raise DeadlineExceeded("observer missed a complete sampling interval")
    return observed_value + interval_value


def _latest_heartbeat(lines: list[str]) -> dict[str, Any] | None:
    for line in reversed(lines):
        if _HEARTBEAT_MARKER not in line:
            continue
        fields: dict[str, str] = {}
        for part in line.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator:
                fields[key] = value.strip()
        if any(field not in fields for field in _HEARTBEAT_FIELDS) or not fields.get("generation"):
            return None
        return {field: fields[field] for field in _HEARTBEAT_FIELDS}
    return None


def _heartbeat_signature(heartbeat: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if heartbeat is None:
        return None
    return tuple(
        heartbeat.get(key)
        for key in (
            "generation",
            "phase",
            "task_id",
            "callback_seq",
            "is_sync",
            "is_cancelled",
            "command_pending",
            "dispatch_result",
        )
    )


def _mapping_contexts(mapping: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    contexts = mapping.get("contexts")
    if not isinstance(contexts, list):
        raise DiagnosticAvailabilityError("diagnostic mapping contexts are missing")
    by_name: dict[str, dict[str, Any]] = {}
    for context in contexts:
        if isinstance(context, dict) and isinstance(context.get("context"), str):
            by_name[context["context"]] = context
    selected: dict[str, dict[str, Any]] = {}
    for name in _REQUIRED_CONTEXTS:
        context = by_name.get(name)
        if not isinstance(context, dict) or context.get("status") != "available":
            raise DiagnosticAvailabilityError(f"diagnostic mapping context {name} is unavailable")
        sequence = context.get("transition_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise DiagnosticAvailabilityError(f"diagnostic mapping context {name} has no transition sequence")
        if not isinstance(context.get("session_generation"), str):
            raise DiagnosticAvailabilityError(f"diagnostic mapping context {name} has no session generation")
        selected[name] = context
    return selected


def _validated_mapping(mapping: object) -> dict[str, Any]:
    if not isinstance(mapping, dict) or mapping.get("status") != "available":
        reason = mapping.get("reason") if isinstance(mapping, dict) else "malformed"
        raise DiagnosticAvailabilityError(f"diagnostic mapping is unavailable: {reason}")
    if mapping.get("schema_version") != 2:
        raise DiagnosticAvailabilityError("diagnostic mapping schema version is invalid")
    lifetime = mapping.get("mapping_lifetime")
    if isinstance(lifetime, bool) or not isinstance(lifetime, int) or lifetime <= 0:
        raise DiagnosticAvailabilityError("diagnostic mapping lifetime is invalid")
    if not isinstance(mapping.get("mapping_name"), str) or not mapping["mapping_name"].strip():
        raise DiagnosticAvailabilityError("diagnostic mapping name is missing")
    _mapping_contexts(mapping)
    return copy.deepcopy(mapping)


def _mapping_identity(mapping: Mapping[str, Any]) -> tuple[str, int, int]:
    return (
        str(mapping["mapping_name"]),
        int(mapping["mapping_lifetime"]),
        int(mapping["schema_version"]),
    )


class SharedControlBlock:
    """Fixed-layout shared control state exchanged with the Windows supervisor."""

    def __init__(self, path: pathlib.Path, *, create: bool = False) -> None:
        self.path = pathlib.Path(path).resolve()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("wb") as output:
                output.write(b"\x00" * CONTROL_BLOCK_SIZE)
        if not self.path.is_file() or self.path.stat().st_size != CONTROL_BLOCK_SIZE:
            raise RuntimeError(f"shared control block must be exactly {CONTROL_BLOCK_SIZE} bytes")
        self._file = self.path.open("r+b")
        self._map = mmap.mmap(self._file.fileno(), CONTROL_BLOCK_SIZE, access=mmap.ACCESS_WRITE)

    def close(self) -> None:
        try:
            self._map.flush()
            self._map.close()
        finally:
            self._file.close()

    def _read_int(self, offset: int) -> int:
        return struct.unpack_from("<i", self._map, offset)[0]

    def _read_long(self, offset: int) -> int:
        return struct.unpack_from("<q", self._map, offset)[0]

    def _write_int(self, offset: int, value: int) -> None:
        struct.pack_into("<i", self._map, offset, int(value))
        self._map.flush()

    def _write_long(self, offset: int, value: int) -> None:
        struct.pack_into("<q", self._map, offset, int(value))
        self._map.flush()

    def initialize(self, *, origin_ns: int, work_deadline_ns: int, hard_deadline_ns: int, emergency_ns: int, observer_zero_ns: int, controller_zero_ns: int) -> None:
        self._map[:] = b"\x00" * CONTROL_BLOCK_SIZE
        self._map[0:8] = CONTROL_MAGIC
        self._write_int(8, CONTROL_SCHEMA_VERSION)
        self._write_int(CONTROL_OFFSET_SUPERVISOR_STATUS, CONTROL_SUPERVISOR_STARTING)
        self._write_int(CONTROL_OFFSET_CONTROLLER_STATUS, CONTROL_CONTROLLER_STARTING)
        self._write_int(CONTROL_OFFSET_OBSERVER_STATUS, CONTROL_OBSERVER_WAITING)
        self._write_long(CONTROL_OFFSET_ORIGIN_NS, origin_ns)
        self._write_long(CONTROL_OFFSET_WORK_DEADLINE_NS, work_deadline_ns)
        self._write_long(CONTROL_OFFSET_HARD_DEADLINE_NS, hard_deadline_ns)
        self._write_long(CONTROL_OFFSET_EMERGENCY_NS, emergency_ns)
        self._write_long(CONTROL_OFFSET_OBSERVER_ZERO_NS, observer_zero_ns)
        self._write_long(CONTROL_OFFSET_CONTROLLER_ZERO_NS, controller_zero_ns)
        timestamp = time.monotonic_ns()
        self._write_long(CONTROL_OFFSET_SUPERVISOR_UPDATE_NS, timestamp)
        self._write_long(CONTROL_OFFSET_CONTROLLER_UPDATE_NS, timestamp)
        self._write_long(CONTROL_OFFSET_OBSERVER_UPDATE_NS, timestamp)

    def validate(self) -> None:
        if bytes(self._map[0:8]) != CONTROL_MAGIC or self._read_int(8) != CONTROL_SCHEMA_VERSION:
            raise RuntimeError("shared control block identity is invalid")

    def statuses(self) -> dict[str, int]:
        return {
            "supervisor": self._read_int(CONTROL_OFFSET_SUPERVISOR_STATUS),
            "controller": self._read_int(CONTROL_OFFSET_CONTROLLER_STATUS),
            "observer": self._read_int(CONTROL_OFFSET_OBSERVER_STATUS),
        }

    def supervisor_status(self) -> int:
        return self._read_int(CONTROL_OFFSET_SUPERVISOR_STATUS)

    def controller_status(self) -> int:
        return self._read_int(CONTROL_OFFSET_CONTROLLER_STATUS)

    def observer_status(self) -> int:
        return self._read_int(CONTROL_OFFSET_OBSERVER_STATUS)

    def set_controller_status(self, status: int) -> None:
        self._write_int(CONTROL_OFFSET_CONTROLLER_STATUS, status)
        self._write_long(CONTROL_OFFSET_CONTROLLER_UPDATE_NS, time.monotonic_ns())

    def set_supervisor_status(self, status: int) -> None:
        self._write_int(CONTROL_OFFSET_SUPERVISOR_STATUS, status)
        self._write_long(CONTROL_OFFSET_SUPERVISOR_UPDATE_NS, time.monotonic_ns())

    def set_observer_status(self, status: int) -> None:
        self._write_int(CONTROL_OFFSET_OBSERVER_STATUS, status)
        self._write_long(CONTROL_OFFSET_OBSERVER_UPDATE_NS, time.monotonic_ns())

    def _supervisor_status_is_failure(self) -> bool:
        return self.supervisor_status() in {
            CONTROL_SUPERVISOR_FAILURE,
            CONTROL_SUPERVISOR_FORCED_OBSERVER_FAILURE,
            CONTROL_SUPERVISOR_EMERGENCY,
        }

    def update_controller(self) -> None:
        self._write_long(CONTROL_OFFSET_CONTROLLER_UPDATE_NS, time.monotonic_ns())

    def update_observer(self) -> None:
        self._write_long(CONTROL_OFFSET_OBSERVER_UPDATE_NS, time.monotonic_ns())

    def set_repetition(self, repetition: int, deadline_ns: int) -> None:
        self._write_int(CONTROL_OFFSET_REPETITION, repetition)
        self._write_long(CONTROL_OFFSET_REPETITION_DEADLINE_NS, deadline_ns)
        self.update_controller()

    def clear_repetition(self) -> None:
        self._write_long(CONTROL_OFFSET_REPETITION_DEADLINE_NS, 0)
        self._write_int(CONTROL_OFFSET_REPETITION, 0)
        self.update_controller()

    def repetition_deadline_ns(self) -> int:
        return self._read_long(CONTROL_OFFSET_REPETITION_DEADLINE_NS)

    def deadlines(self) -> dict[str, int]:
        return {
            "origin_ns": self._read_long(CONTROL_OFFSET_ORIGIN_NS),
            "work_deadline_ns": self._read_long(CONTROL_OFFSET_WORK_DEADLINE_NS),
            "hard_deadline_ns": self._read_long(CONTROL_OFFSET_HARD_DEADLINE_NS),
            "emergency_ns": self._read_long(CONTROL_OFFSET_EMERGENCY_NS),
            "observer_zero_ns": self._read_long(CONTROL_OFFSET_OBSERVER_ZERO_NS),
            "controller_zero_ns": self._read_long(CONTROL_OFFSET_CONTROLLER_ZERO_NS),
            "repetition_deadline_ns": self.repetition_deadline_ns(),
        }


def _signal_named_event(name: str | None) -> None:
    if not name or os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_event = kernel32.OpenEventW
    open_event.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    open_event.restype = ctypes.c_void_p
    set_event = kernel32.SetEvent
    set_event.argtypes = [ctypes.c_void_p]
    set_event.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = open_event(0x0002, 0, name)
    if handle:
        try:
            set_event(handle)
        finally:
            close_handle(handle)


def _atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _observer_process_snapshot(pid: int, identity: tuple[int, float], sample: int, mapping: Mapping[str, Any]) -> dict[str, Any]:
    try:
        process = psutil.Process(pid)
        observed_create_time = float(process.create_time())
        if observed_create_time != identity[1]:
            raise DiagnosticAvailabilityError("BDS identity changed during observer snapshot")
        threads = []
        for thread in process.threads()[:256]:
            threads.append({"id": int(thread.id), "user_time": float(thread.user_time), "system_time": float(thread.system_time)})
        return {
            "snapshot": sample,
            "monotonic_ns": time.monotonic_ns(),
            "bds_pid": pid,
            "bds_create_time": observed_create_time,
            "name": process.name(),
            "status": process.status(),
            "num_threads": len(threads),
            "threads": threads,
            "mapping_lifetime": mapping.get("mapping_lifetime"),
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
        raise DiagnosticAvailabilityError(f"BDS process snapshot failed: {type(exc).__name__}") from exc


def _bounded_observer_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    bounded = copy.deepcopy(evidence)

    def size() -> int:
        return len(json.dumps(bounded, separators=(",", ":"), sort_keys=True).encode("utf-8"))

    if size() > OBSERVER_CAPTURE_MAX_BYTES:
        bounded["ring"] = bounded.get("ring", [])[-20:]
        bounded["log_lines"] = bounded.get("log_lines", [])[-20:]
        bounded["snapshots"] = bounded.get("snapshots", [])[-2:]
    if size() > OBSERVER_CAPTURE_MAX_BYTES:
        bounded["ring"] = []
        bounded["log_lines"] = []
        bounded["snapshots"] = [
            {key: snapshot.get(key) for key in ("snapshot", "monotonic_ns", "bds_pid", "bds_create_time")}
            for snapshot in bounded.get("snapshots", [])
            if isinstance(snapshot, dict)
        ]
    if size() > OBSERVER_CAPTURE_MAX_BYTES:
        bounded = {
            "kind": "spark51-post-reload-diagnostic",
            "trigger_reason": "lifecycle-and-tick-transitions-unchanged",
            "repetition": evidence.get("repetition"),
            "reload_index": evidence.get("reload_index"),
            "captured_at": evidence.get("captured_at"),
            "validated_mapping": {"status": "available"},
            "trigger_sample": {"sample": None},
            "ring": [],
            "snapshots": [],
            "log_lines": [],
        }
    if size() > OBSERVER_CAPTURE_MAX_BYTES:
        raise RuntimeError("observer evidence exceeds bounded capture size")
    return bounded


def _observer_read_log(log_path: pathlib.Path) -> tuple[list[str], str | None]:
    try:
        raw = log_path.read_bytes()
    except OSError as exc:
        raise DiagnosticAvailabilityError(f"lifecycle log unavailable: {type(exc).__name__}") from exc
    raw = raw[-1024 * 1024 :]
    text = raw.decode("utf-8", "replace")
    complete = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    if not complete and lines:
        lines.pop()
    heartbeat = _latest_heartbeat(lines)
    if heartbeat is None:
        raise DiagnosticAvailabilityError("complete lifecycle-heartbeat line is unavailable")
    return lines[-40:], json.dumps(heartbeat, separators=(",", ":"), sort_keys=True)


def _observer_verify_identity(identity: tuple[int, float]) -> None:
    pid, create_time = identity
    try:
        process = psutil.Process(pid)
        observed = float(process.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError) as exc:
        raise DiagnosticAvailabilityError(f"pinned BDS process is unavailable: {type(exc).__name__}") from exc
    if observed != create_time:
        raise DiagnosticAvailabilityError(f"pinned BDS process identity changed: expected={identity!r} observed={(pid, observed)!r}")


def _observer_state_base() -> dict[str, Any]:
    return {
        "status": "waiting",
        "triggered": False,
        "failure": None,
        "trigger": None,
        "mapping": None,
        "ring": [],
        "sample_count": 0,
        "unchanged_samples": 0,
        "files": [],
        "process": {"pid": os.getpid(), "alive": True, "exitcode": None, "stopped": False, "forced_termination": False},
    }


def run_autonomous_observer(root: pathlib.Path, control_path: pathlib.Path) -> int:
    control = SharedControlBlock(control_path)
    try:
        control.validate()
    except Exception:
        control.close()
        raise
    root = pathlib.Path(root).resolve()
    state_path = root / _OBSERVER_STATE_FILE
    command_path = root / _OBSERVER_COMMAND_FILE
    output_directory = root / "combined-health-capture-windows"
    log_path = root / "bds.log"
    event_ready = os.environ.get("SPARK51_OBSERVER_READY_EVENT")
    event_failure = os.environ.get("SPARK51_OBSERVER_FAILURE_EVENT")
    state = _observer_state_base()
    _atomic_json(state_path, state)
    _atomic_json(root / _OBSERVER_RESULT_FILE, state)
    control.set_observer_status(CONTROL_OBSERVER_WAITING)
    sequence = 0
    armed = False
    stop_requested = False
    pending_reacquire: dict[str, Any] | None = None
    expected_identity: tuple[int, float] | None = None
    expected_mapping_identity: tuple[str, int, int] | None = None
    expected_lifetime: int | None = None
    repetition: int | None = None
    reload_index: int | None = None
    mapping: dict[str, Any] | None = None
    ring: deque[dict[str, Any]] = deque(maxlen=OBSERVER_RING_SIZE)
    last_signature: tuple[Any, ...] | None = None
    unchanged_samples = 0
    sample_number = 0
    next_sample = 0.0

    def publish() -> None:
        state["ring"] = list(ring)
        state["sample_count"] = sample_number
        state["unchanged_samples"] = unchanged_samples
        _atomic_json(state_path, state)
        _atomic_json(root / _OBSERVER_RESULT_FILE, state)
        control.update_observer()

    def fail(reason: str, detail: str) -> None:
        nonlocal armed, pending_reacquire
        armed = False
        pending_reacquire = None
        state["status"] = "failed"
        state["failure"] = {"reason": reason, "detail": detail[:1200], "at": now_iso()}
        state["triggered"] = False
        control.set_observer_status(CONTROL_OBSERVER_FAILURE)
        _signal_named_event(event_failure)
        publish()

    def mapping_for(identity: tuple[int, float]) -> dict[str, Any]:
        _observer_verify_identity(identity)
        return _validated_mapping(read_ci_diagnostics(identity[0]))

    try:
        while True:
            command = _read_json(command_path)
            if command is not None:
                observed_sequence = command.get("sequence")
                if isinstance(observed_sequence, int) and observed_sequence > sequence:
                    sequence = observed_sequence
                    operation = command.get("operation")
                    if operation == "stop":
                        stop_requested = True
                    elif operation == "disarm":
                        armed = False
                        pending_reacquire = None
                        last_signature = None
                        unchanged_samples = 0
                        state["status"] = "disarmed"
                        control.set_observer_status(CONTROL_OBSERVER_WAITING)
                        publish()
                    elif operation in ("arm", "reacquire"):
                        raw_identity = command.get("bds_identity")
                        if not isinstance(raw_identity, list) or len(raw_identity) != 2:
                            fail("malformed-command", "observer command has no pinned BDS identity")
                        else:
                            expected_identity = (int(raw_identity[0]), float(raw_identity[1]))
                            repetition = int(command.get("repetition"))
                            reload_index = int(command.get("reload_index"))
                            expected_lifetime = None
                            expected_mapping_identity = None
                            pending_reacquire = dict(command)
                            armed = operation == "arm"
                            if not armed:
                                state["status"] = "reacquiring"
                                publish()
                            else:
                                state["status"] = "arming"
                            next_sample = time.monotonic() + OBSERVER_SAMPLE_SECONDS
                    elif operation is not None:
                        fail("malformed-command", f"unknown observer operation: {operation!r}")

            if stop_requested:
                if state["status"] in {"failed", "triggered"}:
                    state["process"]["stopped"] = True
                    state["process"]["alive"] = False
                    publish()
                    return 1
                state["status"] = "stopped"
                state["process"]["stopped"] = True
                state["process"]["alive"] = False
                control.set_observer_status(CONTROL_OBSERVER_STOPPED)
                publish()
                return 0

            if pending_reacquire is not None and expected_identity is not None:
                try:
                    observed_mapping = mapping_for(expected_identity)
                    observed_identity = expected_identity
                    identity = _mapping_identity(observed_mapping)
                    expected_lifetime = observed_mapping["mapping_lifetime"]
                    expected_mapping_identity = identity
                    mapping = observed_mapping
                    state["mapping"] = {
                        "mapping_name": identity[0],
                        "mapping_lifetime": identity[1],
                        "schema_version": identity[2],
                        "bds_pid": observed_identity[0],
                        "bds_create_time": observed_identity[1],
                    }
                    state["repetition"] = repetition
                    state["reload_index"] = reload_index
                    if pending_reacquire.get("operation") == "arm":
                        state["status"] = "armed"
                        control.set_observer_status(CONTROL_OBSERVER_READY)
                        _signal_named_event(event_ready)
                    else:
                        state["status"] = "reacquired"
                    pending_reacquire = None
                    publish()
                except (DiagnosticAvailabilityError, OSError, RuntimeError, ValueError) as exc:
                    command_deadline_ns = int(pending_reacquire.get("deadline_ns") or 0)
                    if command_deadline_ns and time.monotonic_ns() >= command_deadline_ns:
                        fail("mapping-unavailable", str(exc))
                        pending_reacquire = None

            if armed and expected_identity is not None and expected_mapping_identity is not None and mapping is not None:
                now = time.monotonic()
                if now >= next_sample:
                    try:
                        next_sample_after = next_observer_tick(next_sample, now)
                    except DeadlineExceeded as exc:
                        fail("sampling-interval-missed", str(exc))
                    else:
                        sample_number += 1
                        _observer_verify_identity(expected_identity)
                        observed_mapping = mapping_for(expected_identity)
                        if _mapping_identity(observed_mapping) != expected_mapping_identity or observed_mapping["mapping_lifetime"] != expected_lifetime:
                            raise DiagnosticAvailabilityError("diagnostic mapping identity changed while observer was armed")
                        contexts = _mapping_contexts(observed_mapping)
                        lines, heartbeat_json = _observer_read_log(log_path)
                        heartbeat = json.loads(heartbeat_json)
                        record = {
                            "sample": sample_number,
                            "captured_at": now_iso(),
                            "monotonic_ns": time.monotonic_ns(),
                            "repetition": repetition,
                            "reload_index": reload_index,
                            "bds_pid": expected_identity[0],
                            "bds_create_time": expected_identity[1],
                            "heartbeat": heartbeat,
                            "heartbeat_line": heartbeat_json,
                            "log_lines": lines,
                            "mapping_lifetime": expected_lifetime,
                            "session_generation": {name: context["session_generation"] for name, context in contexts.items()},
                            "contexts": {
                                name: {"transition_sequence": context["transition_sequence"], "phase": context.get("phase")}
                                for name, context in contexts.items()
                            },
                        }
                        ring.append(record)
                        signature = (
                            _heartbeat_signature(heartbeat),
                            contexts["ApplicationTick"]["transition_sequence"],
                            contexts["PluginTick"]["transition_sequence"],
                        )
                        if signature == last_signature:
                            unchanged_samples += 1
                        else:
                            last_signature = signature
                            unchanged_samples = 1
                        state["last_sample"] = record
                        state["status"] = "armed"
                        next_sample = next_sample_after
                        publish()
                        if unchanged_samples >= OBSERVER_STALL_SAMPLES:
                            first = _observer_process_snapshot(expected_identity[0], expected_identity, 1, observed_mapping)
                            delay = OBSERVER_CAPTURE_DELAY_SECONDS
                            repetition_deadline_ns = control.repetition_deadline_ns()
                            hard_deadline_ns = control.deadlines()["hard_deadline_ns"]
                            capture_deadline_ns = min(
                                value for value in (hard_deadline_ns, repetition_deadline_ns or hard_deadline_ns) if value > 0
                            )
                            remaining_ns = capture_deadline_ns - time.monotonic_ns()
                            if remaining_ns < int(delay * 1_000_000_000):
                                raise DeadlineExceeded("observer capture deadline expired before +2s snapshot")
                            time.sleep(delay)
                            if time.monotonic_ns() >= capture_deadline_ns:
                                raise DeadlineExceeded("observer capture deadline expired during +2s snapshot")
                            _observer_verify_identity(expected_identity)
                            second_mapping = mapping_for(expected_identity)
                            if _mapping_identity(second_mapping) != expected_mapping_identity:
                                raise DiagnosticAvailabilityError("diagnostic mapping changed during trigger capture")
                            second = _observer_process_snapshot(expected_identity[0], expected_identity, 2, second_mapping)
                            evidence = _bounded_observer_evidence(
                                {
                                    "kind": "spark51-post-reload-diagnostic",
                                    "trigger_reason": "lifecycle-and-tick-transitions-unchanged",
                                    "repetition": repetition,
                                    "reload_index": reload_index,
                                    "captured_at": now_iso(),
                                    "validated_mapping": copy.deepcopy(observed_mapping),
                                    "trigger_sample": copy.deepcopy(record),
                                    "ring": list(ring),
                                    "snapshots": [first, second],
                                    "log_lines": lines,
                                }
                            )
                            evidence_path = output_directory / _OBSERVER_EVIDENCE_FILE
                            _atomic_json(evidence_path, evidence)
                            state["status"] = "triggered"
                            state["triggered"] = True
                            state["trigger"] = evidence
                            state["files"] = [evidence_path.name]
                            control.set_observer_status(CONTROL_OBSERVER_TRIGGERED)
                            publish()
                            armed = False
                            control.update_observer()
            time.sleep(0.05)
    except (DiagnosticAvailabilityError, DeadlineExceeded, OSError, RuntimeError, ValueError, psutil.Error) as exc:
        fail("observer-failed", str(exc))
        return 1
    finally:
        control.close()


class SupervisorObserverClient:
    """Controller-side file/event client; sampling remains in the supervisor child."""

    def __init__(self, root: pathlib.Path, control_path: pathlib.Path, output_directory: pathlib.Path, log_path: pathlib.Path, *, clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep) -> None:
        self.root = pathlib.Path(root).resolve()
        self.control_path = pathlib.Path(control_path).resolve()
        self.output_directory = pathlib.Path(output_directory)
        self.log_path = pathlib.Path(log_path)
        self.clock = clock
        self.sleeper = sleeper
        self.state_path = self.root / _OBSERVER_STATE_FILE
        self.command_path = self.root / _OBSERVER_COMMAND_FILE
        self._sequence = 0
        self._result: dict[str, Any] = _observer_state_base()

    def _control(self) -> SharedControlBlock:
        return SharedControlBlock(self.control_path)

    def _control_statuses(self) -> dict[str, int] | None:
        try:
            control = self._control()
            try:
                control.validate()
                return control.statuses()
            finally:
                control.close()
        except (OSError, RuntimeError):
            return None

    def _read_state(self) -> dict[str, Any]:
        state = _read_json(self.state_path)
        if state is not None:
            self._result = state
        return copy.deepcopy(self._result)

    def result(self) -> dict[str, Any]:
        state = self._read_state()
        if state.get("status") in {"waiting", "arming", "armed", "reacquiring", "reacquired", "disarmed", "stopped"}:
            state["status"] = "not-triggered"
            state["triggered"] = False
        state.setdefault("ring_size", len(state.get("ring") or []))
        mapping = state.get("mapping")
        if isinstance(mapping, dict):
            state.setdefault("mapping_name", mapping.get("mapping_name"))
            state.setdefault("mapping_lifetime", mapping.get("mapping_lifetime"))
            state.setdefault("mapping_schema_version", mapping.get("schema_version"))
        statuses = self._control_statuses()
        if statuses is not None:
            observer_status = statuses["observer"]
            supervisor_status = statuses["supervisor"]
            if observer_status == CONTROL_OBSERVER_FAILURE:
                state["status"] = "failed"
            elif observer_status == CONTROL_OBSERVER_TRIGGERED:
                state["status"] = "triggered"
                state["triggered"] = True
            if supervisor_status == CONTROL_SUPERVISOR_OBSERVER_DEATH_CONFIRMED:
                process = state.setdefault("process", {})
                process.update({"alive": False, "stopped": True})
            if supervisor_status in {CONTROL_SUPERVISOR_FORCED_OBSERVER_FAILURE, CONTROL_SUPERVISOR_EMERGENCY}:
                state.setdefault("process", {})["forced_termination"] = True
        return state

    @property
    def triggered(self) -> bool:
        state = self.result()
        return state.get("triggered") is True or state.get("status") == "triggered"

    @property
    def failed(self) -> bool:
        state = self.result()
        statuses = self._control_statuses()
        return (
            state.get("failure") is not None
            or state.get("status") == "failed"
            or (statuses is not None and statuses["supervisor"] in {
                CONTROL_SUPERVISOR_FAILURE,
                CONTROL_SUPERVISOR_FORCED_OBSERVER_FAILURE,
                CONTROL_SUPERVISOR_EMERGENCY,
            })
        )

    def _write_command(self, operation: str, **fields: Any) -> int:
        self._sequence += 1
        command = {"sequence": self._sequence, "operation": operation, **fields}
        _atomic_json(self.command_path, command)
        return self._sequence

    def _wait(self, predicate: Callable[[dict[str, Any]], bool], timeout: float, description: str, deadline: float | None = None) -> dict[str, Any]:
        end = self.clock() + max(0.0, float(timeout))
        if deadline is not None:
            end = min(end, deadline)
        while self.clock() < end:
            state = self._read_state()
            if predicate(state):
                return state
            if self.failed:
                raise DiagnosticAvailabilityError(f"observer {description} failed: {state.get('failure')}")
            remaining = end - self.clock()
            if remaining <= 0:
                break
            self.sleeper(min(0.05, remaining))
        raise TimeoutError(f"timed out waiting for observer {description}")

    def arm(self, repetition: int, reload_index: int, *, expected_identity: tuple[int, float], deadline: float) -> dict[str, Any]:
        if len(expected_identity) != 2 or int(expected_identity[0]) <= 0 or float(expected_identity[1]) <= 0:
            raise DiagnosticAvailabilityError("observer arm identity is invalid")
        self._write_command(
            "arm",
            repetition=int(repetition),
            reload_index=int(reload_index),
            bds_identity=[int(expected_identity[0]), float(expected_identity[1])],
            deadline_ns=int(deadline * 1_000_000_000),
            log_path=str(self.log_path),
            output_directory=str(self.output_directory),
        )
        state = self._wait(lambda value: value.get("status") == "armed", 5.0, "arm acknowledgement", deadline)
        mapping = state.get("mapping")
        if not isinstance(mapping, dict) or not isinstance(mapping.get("mapping_name"), str):
            raise DiagnosticAvailabilityError("observer arm acknowledgement has no mapping")
        return mapping

    def reacquire(self, *, timeout: float = RELOAD_MAPPING_GRACE_SECONDS, deadline: float, expected_identity: tuple[int, float]) -> dict[str, Any]:
        self.disarm(timeout=min(2.0, timeout), deadline=deadline)
        self._write_command(
            "reacquire",
            repetition=0,
            reload_index=0,
            bds_identity=[int(expected_identity[0]), float(expected_identity[1])],
            deadline_ns=int(deadline * 1_000_000_000),
            log_path=str(self.log_path),
            output_directory=str(self.output_directory),
        )
        state = self._wait(lambda value: value.get("status") == "reacquired", timeout, "reload mapping reacquisition", deadline)
        mapping = state.get("mapping")
        if not isinstance(mapping, dict):
            raise DiagnosticAvailabilityError("observer reacquisition has no mapping")
        return mapping

    def disarm(self, *, timeout: float = 2.0, deadline: float | None = None) -> None:
        self._write_command("disarm")
        self._wait(lambda value: value.get("status") == "disarmed", timeout, "disarm acknowledgement", deadline)

    def poll(self) -> dict[str, Any]:
        return self.result()

    def stop(self, *, timeout: float = 5.0, deadline: float | None = None) -> bool:
        control = self._control()
        try:
            control.validate()
            if control.controller_status() != CONTROL_CONTROLLER_FAILURE:
                control.set_controller_status(CONTROL_CONTROLLER_NORMAL_OBSERVER_STOP_REQUEST)
        finally:
            control.close()
        self._write_command("stop")
        end = self.clock() + min(max(0.0, timeout), 5.0)
        if deadline is not None:
            end = min(end, deadline)
        while self.clock() < end:
            statuses = self._control_statuses()
            if statuses is not None and statuses["supervisor"] in {
                CONTROL_SUPERVISOR_FAILURE,
                CONTROL_SUPERVISOR_FORCED_OBSERVER_FAILURE,
                CONTROL_SUPERVISOR_EMERGENCY,
            }:
                return False
            if statuses is not None and statuses["supervisor"] == CONTROL_SUPERVISOR_OBSERVER_DEATH_CONFIRMED and statuses["observer"] == CONTROL_OBSERVER_STOPPED:
                state = self.result()
                return state.get("process", {}).get("forced_termination") is not True
            self.sleeper(min(0.05, max(0.001, end - self.clock())))
        return False


def observer_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observer", action="store_true")
    parser.add_argument("--observer-root", type=pathlib.Path)
    parser.add_argument("--control-path", type=pathlib.Path)
    args = parser.parse_args()
    if not args.observer or args.observer_root is None or args.control_path is None:
        parser.error("--observer, --observer-root, and --control-path are required")
    return run_autonomous_observer(args.observer_root, args.control_path)


def _strict_positive_id(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a positive integer")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


def _strict_spark_discover(component: str, platform_name: str, expected_sha: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    import providers.artifact_provider as provider

    original_discover = getattr(_strict_spark_discover, "_original", None)
    if component != "spark":
        if not callable(original_discover):
            raise RuntimeError("provider discover delegate is unavailable")
        return original_discover(component, platform_name, expected_sha)
    if platform_name != "windows" or (expected_sha or "").strip().lower() != SPARK_CANDIDATE_SHA:
        raise RuntimeError("strict Spark discovery requires the frozen Windows candidate SHA")
    artifact = provider._get_json(f"/repos/{EXPECTED_SPARK_REPOSITORY}/actions/artifacts/{EXPECTED_SPARK_ARTIFACT_ID}")
    if _strict_positive_id(artifact.get("id"), "Spark artifact ID") != EXPECTED_SPARK_ARTIFACT_ID:
        raise RuntimeError("Spark artifact ID mismatch")
    if artifact.get("expired") is not False:
        raise RuntimeError("Spark artifact is expired or has no explicit nonexpired evidence")
    digest = str(artifact.get("digest") or "").strip().lower()
    if digest != EXPECTED_SPARK_ARTIFACT_DIGEST:
        raise RuntimeError("Spark API artifact digest mismatch")
    workflow_run = artifact.get("workflow_run")
    if (
        not isinstance(workflow_run, dict)
        or _strict_positive_id(workflow_run.get("id"), "Spark artifact workflow run ID") != EXPECTED_SPARK_ARTIFACT_RUN_ID
        or str(workflow_run.get("head_sha") or "").strip().lower() != SPARK_CANDIDATE_SHA
    ):
        raise RuntimeError("Spark artifact workflow run ID mismatch")
    run = provider._get_json(f"/repos/{EXPECTED_SPARK_REPOSITORY}/actions/runs/{EXPECTED_SPARK_ARTIFACT_RUN_ID}")
    repository = run.get("repository") if isinstance(run.get("repository"), dict) else {}
    head_repository = run.get("head_repository") if isinstance(run.get("head_repository"), dict) else {}
    if (
        _strict_positive_id(run.get("id"), "Spark workflow run ID") != EXPECTED_SPARK_ARTIFACT_RUN_ID
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("path") != EXPECTED_SPARK_WORKFLOW_PATH
        or str(run.get("head_sha") or "").strip().lower() != SPARK_CANDIDATE_SHA
        or repository.get("full_name") != EXPECTED_SPARK_REPOSITORY
        or head_repository.get("full_name") != EXPECTED_SPARK_REPOSITORY
    ):
        raise RuntimeError("Spark artifact workflow run provenance mismatch")
    return run, artifact


def _strict_safe_extract(archive: pathlib.Path, destination: pathlib.Path) -> None:
    import stat
    import zipfile

    root = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            normalized = member.filename.replace("\\", "/")
            parts = pathlib.PurePosixPath(normalized).parts
            if not normalized or normalized.startswith("/") or ":" in (parts[0] if parts else "") or ".." in parts:
                raise RuntimeError(f"artifact ZIP contains an unsafe path: {member.filename!r}")
            mode = (member.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"artifact ZIP contains a symbolic link: {member.filename!r}")
            target = (destination / pathlib.PurePosixPath(normalized)).resolve()
            if os.path.commonpath((str(root), str(target))) != str(root):
                raise RuntimeError(f"artifact ZIP escapes its destination: {member.filename!r}")
            zipped.extract(member, destination)


def _strict_download_artifact(repo: str, artifact: dict[str, Any], destination: pathlib.Path) -> pathlib.Path:
    import providers.artifact_provider as provider

    original_download = getattr(_strict_download_artifact, "_original", None)
    if repo != EXPECTED_SPARK_REPOSITORY:
        if not callable(original_download):
            raise RuntimeError("provider download delegate is unavailable")
        return original_download(repo, artifact, destination)
    if _strict_positive_id(artifact.get("id"), "Spark artifact ID") != EXPECTED_SPARK_ARTIFACT_ID:
        raise RuntimeError("Spark artifact ID mismatch before download")
    if artifact.get("expired") is not False or str(artifact.get("digest") or "").strip().lower() != EXPECTED_SPARK_ARTIFACT_DIGEST:
        raise RuntimeError("Spark API artifact evidence changed before download")
    workflow_run = artifact.get("workflow_run")
    if (
        not isinstance(workflow_run, dict)
        or _strict_positive_id(workflow_run.get("id"), "Spark artifact workflow run ID") != EXPECTED_SPARK_ARTIFACT_RUN_ID
        or str(workflow_run.get("head_sha") or "").strip().lower() != SPARK_CANDIDATE_SHA
    ):
        raise RuntimeError("Spark artifact workflow run evidence is missing")
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "artifact.zip"
    url = f"{provider.API}/repos/{repo}/actions/artifacts/{EXPECTED_SPARK_ARTIFACT_ID}/zip"
    opener = provider.urllib.request.build_opener(provider._SafeRedirect())
    digest = hashlib.sha256()
    total = 0
    try:
        with opener.open(provider._request(url, accept="application/vnd.github+json"), timeout=120) as response, archive.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARTIFACT_BYTES:
                    raise RuntimeError("Spark artifact ZIP exceeds the bounded download size")
                digest.update(chunk)
                output.write(chunk)
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    observed_digest = f"sha256:{digest.hexdigest()}"
    if observed_digest != EXPECTED_SPARK_ARTIFACT_DIGEST or observed_digest != str(artifact.get("digest") or "").strip().lower():
        archive.unlink(missing_ok=True)
        raise RuntimeError("Spark artifact ZIP bytes do not match both the API and frozen digest")
    payload = destination / "payload"
    _strict_safe_extract(archive, payload)
    for nested in sorted(payload.rglob("*.zip"), key=lambda value: (len(value.parts), str(value))):
        _strict_safe_extract(nested, nested.with_suffix(""))
    dlls = sorted((path for path in payload.rglob("endstone_spark.dll") if path.is_file()), key=lambda value: (len(value.parts), str(value)))
    if not dlls:
        archive.unlink(missing_ok=True)
        raise RuntimeError("verified Spark artifact contains no endstone_spark.dll")
    selected = dlls[0]
    evidence = {
        "artifact_id": EXPECTED_SPARK_ARTIFACT_ID,
        "artifact_digest": EXPECTED_SPARK_ARTIFACT_DIGEST,
        "download_sha256": observed_digest,
        "payload_dll_relative_path": selected.relative_to(destination).as_posix(),
        "payload_dll_sha256": hashlib.sha256(selected.read_bytes()).hexdigest(),
        "payload_layout": "nested-preserved",
    }
    _atomic_json(destination / "verified-spark-download.json", evidence)
    archive.unlink(missing_ok=True)
    return payload


def _verify_installed_spark_payload(validation: Spark51PostReloadDiagnosticValidation) -> dict[str, Any]:
    observed = validation.metadata.get("components", {}).get("spark") if isinstance(validation.metadata.get("components"), dict) else None
    artifact = observed.get("artifact") if isinstance(observed, dict) else None
    if not isinstance(artifact, dict):
        raise TypeError("Spark metadata is missing artifact byte evidence")
    if _strict_positive_id(artifact.get("id"), "Spark metadata artifact ID") != EXPECTED_SPARK_ARTIFACT_ID:
        raise RuntimeError("installed Spark metadata artifact ID mismatch")
    if str(artifact.get("digest") or "").strip().lower() != EXPECTED_SPARK_ARTIFACT_DIGEST:
        raise RuntimeError("installed Spark metadata API digest mismatch")
    download_sha = str(artifact.get("download_sha256") or "").strip().lower()
    if download_sha != EXPECTED_SPARK_ARTIFACT_DIGEST:
        raise RuntimeError("installed Spark metadata ZIP digest evidence is missing or wrong")
    payload_sha = str(artifact.get("payload_dll_sha256") or "").strip().lower()
    payload_path_value = artifact.get("payload_dll_relative_path")
    payload_root = validation.downloads / "spark" / "payload"
    if not isinstance(payload_path_value, str) or not payload_path_value:
        raise RuntimeError("installed Spark metadata payload DLL evidence is missing")
    payload_path = (validation.downloads / "spark" / pathlib.PurePosixPath(payload_path_value)).resolve()
    if os.path.commonpath((str(payload_root.resolve()), str(payload_path))) != str(payload_root.resolve()) or not payload_path.is_file():
        raise RuntimeError("verified Spark payload DLL path is invalid")
    if hashlib.sha256(payload_path.read_bytes()).hexdigest() != payload_sha:
        raise RuntimeError("verified Spark payload DLL bytes changed")
    installed = validation.server_dir / "plugins" / "endstone_spark.dll"
    if not installed.is_file():
        raise RuntimeError("installed Spark DLL is missing before BDS launch")
    installed_sha = hashlib.sha256(installed.read_bytes()).hexdigest()
    if installed_sha != payload_sha:
        raise RuntimeError("installed Spark DLL bytes do not match verified payload DLL")
    return {
        "artifact_id": EXPECTED_SPARK_ARTIFACT_ID,
        "artifact_digest": EXPECTED_SPARK_ARTIFACT_DIGEST,
        "download_sha256": download_sha,
        "payload_dll_sha256": payload_sha,
        "installed_dll_sha256": installed_sha,
        "payload_dll_relative_path": payload_path.relative_to(validation.root).as_posix(),
    }


def _install_artifacts_worker(bot_binary: str, metadata_path: str) -> None:
    import providers.artifact_provider as provider

    validation = Spark51PostReloadDiagnosticValidation(pathlib.Path(bot_binary), 30)
    original_discover = provider.discover
    original_download = provider._download_artifact
    _strict_spark_discover._original = original_discover  # type: ignore[attr-defined]
    _strict_download_artifact._original = original_download  # type: ignore[attr-defined]
    try:
        provider.discover = _strict_spark_discover
        provider._download_artifact = _strict_download_artifact
        Spark51WindowsBdsValidation.install_artifacts(validation)
        evidence = _read_json(validation.downloads / "spark" / "verified-spark-download.json")
        if evidence is None:
            raise RuntimeError("strict Spark download did not publish byte evidence")
        validation.metadata["components"]["spark"]["artifact"].update(evidence)
        validation._verify_spark_payload = _verify_installed_spark_payload(validation)
        validation.metadata["components"]["spark"]["artifact"].update(validation._verify_spark_payload)
        pathlib.Path(metadata_path).write_text(json.dumps(validation.metadata, sort_keys=True), encoding="utf-8")
    finally:
        provider.discover = original_discover
        provider._download_artifact = original_download


class Spark51PostReloadDiagnosticValidation(Spark51WindowsBdsValidation):
    enable_ci_diagnostics = True

    def __init__(
        self,
        bot_binary: pathlib.Path,
        profile_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._experiment_started = self._clock()
        self._supervisor_control_path = pathlib.Path(os.environ["SPARK51_CONTROL_PATH"]).resolve() if os.environ.get("SPARK51_CONTROL_PATH") else None
        self._control: SharedControlBlock | None = None
        supervisor_deadlines = self._read_supervisor_deadlines()
        if supervisor_deadlines is None:
            self._hard_deadline = self._experiment_started + HARD_DEADLINE_SECONDS
            self._experiment_deadline = self._hard_deadline
            self._work_deadline = self._experiment_started + WORK_DEADLINE_SECONDS
        else:
            self._experiment_started = supervisor_deadlines["origin_ns"] / 1_000_000_000
            self._hard_deadline = supervisor_deadlines["hard_deadline_ns"] / 1_000_000_000
            self._experiment_deadline = self._hard_deadline
            self._work_deadline = supervisor_deadlines["work_deadline_ns"] / 1_000_000_000
        super().__init__(bot_binary, profile_seconds)
        if self._supervisor_control_path is not None:
            self._control = SharedControlBlock(self._supervisor_control_path)
            self._control.validate()
        self._observer: SupervisorObserverClient | None = None
        self._observer_stopped = False
        self._active_repetition_deadline: float | None = None
        self._spark_byte_evidence: dict[str, Any] | None = None
        self.result.update(
            {
                "test_kind": "spark51-windows-post-reload-diagnostic-stress",
                "deadline": {
                    "hard_seconds": HARD_DEADLINE_SECONDS,
                    "cleanup_reserve_seconds": SHUTDOWN_RESERVE_SECONDS,
                    "work_seconds": WORK_TIMEOUT_SECONDS,
                    "started_at_monotonic": self._experiment_started,
                    "emergency_termination_seconds": EMERGENCY_TERMINATION_SECONDS,
                    "observer_zero_deadline_seconds": OBSERVER_ZERO_DEADLINE_SECONDS,
                    "controller_zero_deadline_seconds": CONTROLLER_ZERO_DEADLINE_SECONDS,
                },
                "reload_repetitions": RELOAD_REPETITIONS,
                "reloads_per_repetition": RELOADS_PER_REPETITION,
                "total_reload_target": TOTAL_RELOADS,
                "post_reload_profile_target": POST_RELOAD_PROFILES,
                "post_reload_profiles": [],
                "post_reload_diagnostic": {
                    "status": "not-started",
                    "triggered": False,
                    "failure": None,
                    "trigger": None,
                    "ring_size": 0,
                    "sample_count": 0,
                    "files": [],
                },
                "supervisor_verdict": {
                    "required": True,
                    "path": os.environ.get("SPARK51_SUPERVISOR_VERDICT", str(self.root / "supervisor-verdict.json")),
                    "status": "pending",
                },
            }
        )
        self._write_results()

    def _read_supervisor_deadlines(self) -> dict[str, int] | None:
        raw = {
            key: os.environ.get(key)
            for key in (
                "SPARK51_ORIGIN_MONOTONIC_NS",
                "SPARK51_WORK_DEADLINE_NS",
                "SPARK51_HARD_DEADLINE_NS",
            )
        }
        if not any(raw.values()):
            return None
        try:
            values = {key: int(value or "") for key, value in raw.items()}
        except (TypeError, ValueError) as exc:
            raise RuntimeError("supervisor deadline environment is malformed") from exc
        if values["work_deadline_ns"] <= values["origin_ns"] or values["hard_deadline_ns"] <= values["work_deadline_ns"]:
            raise RuntimeError("supervisor deadlines are not monotonic")
        return values

    def _record_candidate_provenance(self) -> None:
        super()._record_candidate_provenance()
        artifact = self.metadata.get("components", {}).get("spark", {}).get("artifact", {})
        if isinstance(artifact, dict) and artifact.get("download_sha256"):
            self._spark_byte_evidence = _verify_installed_spark_payload(self)
            self.result.setdefault("artifact_provenance", {}).setdefault("spark", {}).update(self._spark_byte_evidence)
            self.result["spark_byte_evidence"] = copy.deepcopy(self._spark_byte_evidence)
            self._write_results()

    def _common_profile_metadata(
        self,
        *,
        kind: str,
        viewer_url: str,
        player_count: int,
        reload_cycle: int,
        allocation_interval: int | None,
        command: str,
    ) -> dict[str, Any]:
        profile = super()._common_profile_metadata(
            kind=kind,
            viewer_url=viewer_url,
            player_count=player_count,
            reload_cycle=reload_cycle,
            allocation_interval=allocation_interval,
            command=command,
        )
        if self._spark_byte_evidence is None:
            raise RuntimeError("profile byte provenance is unavailable")
        profile.update(
            {
                "spark_download_sha256": self._spark_byte_evidence["download_sha256"],
                "spark_payload_dll_sha256": self._spark_byte_evidence["payload_dll_sha256"],
                "spark_installed_dll_sha256": self._spark_byte_evidence["installed_dll_sha256"],
            }
        )
        return profile

    def _effective_hard_deadline(self) -> float:
        values = (getattr(self, "_hard_deadline", math.inf), getattr(self, "_experiment_deadline", math.inf))
        return min(float(value) for value in values)

    def _effective_work_deadline(self) -> float:
        base = min(self._work_deadline, self._effective_hard_deadline() - SHUTDOWN_RESERVE_SECONDS)
        if self._active_repetition_deadline is not None:
            return min(base, self._active_repetition_deadline)
        return base

    def _remaining(self, deadline: float | None = None) -> float:
        target = self._effective_hard_deadline() if deadline is None else deadline
        return target - self._clock()

    def _clip(self, requested: float, *, deadline: float | None = None, stage: str) -> float:
        target = self._effective_work_deadline() if deadline is None else deadline
        return clipped_timeout(requested, target, now=self._clock())

    def _sleep(self, requested: float, *, deadline: float | None = None, stage: str) -> None:
        delay = self._clip(requested, deadline=deadline, stage=stage)
        self._sleeper(delay)

    def _require_budget(self, stage: str, *, cost: float = 0.0, reserve: float = SHUTDOWN_RESERVE_SECONDS) -> None:
        now = self._clock()
        deadline = self._effective_work_deadline()
        if self._control is not None and self._control._supervisor_status_is_failure():
            raise DeadlineExceeded(f"supervisor aborted the diagnostic at {stage}")
        if not math.isfinite(now) or not math.isfinite(deadline) or deadline - now <= cost:
            raise DeadlineExceeded(
                f"diagnostic deadline leaves insufficient time at {stage}; coverage incomplete"
            )

    def _run_work_stage(self, stage: str, action: Callable[[], Any]) -> Any:
        self._require_budget(stage)
        value = action()
        self._require_budget(f"{stage}-complete")
        return value

    def _sync_observer_result(self) -> None:
        if self._observer is not None:
            self.result["post_reload_diagnostic"] = self._observer.result()
            self._write_results()

    def _select_live_identity(self) -> tuple[int, float]:
        if self.server is None:
            raise RuntimeError("cannot select BDS identity without a live server")
        identity = _select_live_bds_identity(self.server.process_tree_snapshot())
        if identity[0] == self.server.pid:
            raise RuntimeError("BDS identity unexpectedly points to the Endstone wrapper")
        if not math.isfinite(identity[1]) or identity[1] <= 0:
            raise RuntimeError(f"invalid BDS create time: {identity!r}")
        return identity

    def _dispatch(self, command: str, timeout: float = 15.0) -> tuple[int, list[str], str]:
        limit = self._clip(timeout, stage=f"ACK {command}")
        return super()._dispatch(command, timeout=limit)

    def wait_player_count(self, expected: int, timeout: float = 45.0) -> tuple[list[str], float]:
        if self.server is None:
            raise RuntimeError("cannot wait for players without a live BDS")
        started = self._clock()
        end = min(started + timeout, self._effective_work_deadline())
        last: list[str] = []
        while self._clock() < end:
            last = list_players(self.server)
            for line in last:
                match = PLAYER_COUNT_RE.search(line)
                if match and int(match.group(1)) == expected:
                    return last, self._clock() - started
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while waiting for fleet player count")
            self._sleep(min(0.5, max(0.001, end - self._clock())), deadline=end, stage="player-count poll")
        raise TimeoutError(f"Expected {expected} online players, last list output: {' | '.join(last[-40:])}")

    def _wait_server_for(self, predicate: Callable[[list[str]], bool], timeout: float, description: str) -> list[str]:
        if self.server is None:
            raise RuntimeError(f"BDS unavailable while waiting for {description}")
        end = min(self._clock() + timeout, self._effective_work_deadline())
        while self._clock() < end:
            lines = self.server.snapshot()
            if predicate(lines):
                return lines
            if not self.server.is_alive():
                code = self.server.process.poll() if self.server.process else None
                raise RuntimeError(f"Server exited with code {code} while waiting for {description}")
            self._sleep(min(0.5, max(0.001, end - self._clock())), deadline=end, stage=description)
        raise TimeoutError(f"Timed out waiting for {description}")

    def start_server(self) -> None:
        self._require_budget("BDS start")
        if self._spark_byte_evidence is None:
            self._spark_byte_evidence = _verify_installed_spark_payload(self)
            self.result["spark_byte_evidence"] = copy.deepcopy(self._spark_byte_evidence)
            self._write_results()
        cmd = [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(self.server_dir)]
        self.server = _FrameworkShutdownServerProcess(cmd, self.root, self.log_path)
        self.server.ci_diagnostics_enabled = bool(self.enable_ci_diagnostics)
        capture = getattr(self, "capture", None)
        if capture is not None:
            self.server.timeout_diagnostic_directory = capture.output_dir
        IntegrationTest._prepare_bstats_before_start(self)
        self.server.start()
        self._wait_server_for(
            lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines),
            240,
            "BDS ready",
        )
        self.check("bds-start", "PASS")
        self.check("ready", "PASS")
        self._wait_server_for(
            lambda lines: any(
                "spark" in line.lower() and any(hint in line.lower() for hint in SPARK_LOAD_HINTS) for line in lines
            ),
            30,
            "Spark enable",
        )
        self.check("spark-load-enable", "PASS")
        lines = self._wait_server_for(
            lambda current: any(
                "ci lifecycle control enabled;" in line.lower()
                and ("file-control=" in line.lower() or "cishutdown registered" in line.lower())
                for line in current
            ),
            30,
            "CI lifecycle control registration",
        )
        registration = next(
            line
            for line in reversed(lines)
            if "ci lifecycle control enabled;" in line.lower()
            and ("file-control=" in line.lower() or "cishutdown registered" in line.lower())
        )
        if "file-control=" in registration.lower():
            raw_path = registration.split("file-control=", 1)[1].strip()
            request_path = pathlib.Path(raw_path)
            if not request_path.is_absolute():
                request_path = (self.root / request_path).resolve()
            command_path: pathlib.Path | None = None
            if "command-control=" in registration.lower():
                raw_command_path = registration.split("command-control=", 1)[1].split(";", 1)[0].strip()
                command_path = pathlib.Path(raw_command_path)
                if not command_path.is_absolute():
                    command_path = (self.root / command_path).resolve()
            self.server.lifecycle_request_path = request_path
            self.server.lifecycle_command_path = command_path
            self.server.lifecycle_registered = True
            self.check(
                "windows-framework-lifecycle",
                "PASS",
                shutdown_control="file-trigger",
                command_control="file-trigger" if command_path is not None else "console-compat",
                request_path=request_path.as_posix(),
                command_path=command_path.as_posix() if command_path is not None else None,
                compatibility_command="cishutdown",
            )
        else:
            self.server.lifecycle_request_path = None
            self.server.lifecycle_command_path = None
            self.server.lifecycle_registered = True
            self.check("windows-interactive-lifecycle", "PASS", shutdown_control="cishutdown")
        version_file = self.server_dir / "version.txt"
        if version_file.exists():
            self.result["bds_version"] = version_file.read_text(encoding="utf-8").strip()
            write_json(self.result_path, self.result)
        observed_protocol = validate_bds_version(self.result, self.server.snapshot())
        self.check(
            "exact-bds-version",
            "PASS",
            observed_protocol=observed_protocol,
            expected_protocol=os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION", "").strip() or None,
            expected_full=os.environ.get("EXPECTED_BDS_VERSION", "").strip() or None,
        )

    def wait_post_start_initialization(self) -> None:
        self._wait_server_for(
            lambda lines: any(
                "[spark] endstone-spark v" in line.lower()
                and "enabled. run /spark to get started." in line.lower()
                for line in lines
            ),
            30,
            "Spark post-start enable completion",
        )

    def command_check(self, name: str, command: str, timeout: float = 8.0) -> list[str]:
        if self.server is None:
            raise RuntimeError(f"cannot run {command!r} without a live BDS")
        limit = self._clip(timeout, stage=f"command {command}")
        start = self.server.command(command)
        output = self.server.wait_command_output(start, limit)
        joined = "\n".join(output).lower()
        if not self.server.is_alive():
            raise RuntimeError(f"Server exited while running console command: {command}")
        if "unknown command" in joined or "command not found" in joined:
            raise RuntimeError(f"Spark command rejected: {command}\n" + "\n".join(output[-20:]))
        self.check(name, "PASS", command)
        return output

    def run_basic_commands(self) -> None:
        self.command_check("spark-profiler-info", "spark profiler info")
        self.command_check("spark-tps", "spark tps")
        self.command_check("spark-health", "spark health")
        self.command_check("spark-activity", "spark activity")

    def start_fleet(self) -> None:
        if self.server is None:
            raise RuntimeError("cannot start fleet without a live BDS")
        self._require_budget("fleet start")
        self.bot = FleetBotProcess(self.bot_binary, self.bot_log, self.count, self.scenario)
        self.bot.start()
        end = min(self._clock() + max(90.0, self.count * 5.0), self._effective_work_deadline())
        online: dict[str, Any] | None = None
        while self._clock() < end:
            events = self.bot.event_snapshot()
            online = next((event for event in events if event.get("event") == "fleet_online"), None)
            if online is not None:
                break
            if not self.bot.is_alive():
                raise RuntimeError(f"Bot exited before fleet_online event: {self.bot.process.returncode if self.bot.process else None}")
            self._sleep(min(0.25, max(0.001, end - self._clock())), deadline=end, stage="fleet-online poll")
        if online is None:
            raise TimeoutError("Timed out waiting for bot fleet_online event")
        if int(online.get("online", -1)) != self.count or int(online.get("count", -1)) != self.count:
            raise RuntimeError(f"Invalid fleet_online event: {online}")
        self.result["fleet_online_event"] = online
        output, convergence = self.wait_player_count(self.count)
        joined = "\n".join(output).lower()
        missing = [name for name in self.expected_names() if name.lower() not in joined]
        if missing:
            raise RuntimeError(f"BDS list reached {self.count} players but names are missing: {missing}")
        self.check(
            "fleet-all-online",
            "PASS",
            f"{self.count} independent players visible in BDS",
            convergence_seconds=round(convergence, 3),
            fleet_online_event=online,
        )

    def stop_fleet(self) -> None:
        if self.bot is None or self.bot.process is None:
            return
        process = self.bot.process
        end = min(self._clock() + 20.0, self._effective_hard_deadline())
        if process.poll() is None:
            process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
            try:
                process.wait(timeout=max(0.001, end - self._clock()))
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=max(0.001, end - self._clock()))
                raise RuntimeError("Bot did not exit after graceful termination signal")
        if self.bot._reader is not None:
            self.bot._reader.join(timeout=max(0.0, min(3.0, end - self._clock())))
        code = int(process.returncode or 0)
        if code != 0:
            raise RuntimeError(f"Fleet exited with code {code} after SIGTERM")
        events = self.bot.event_snapshot()
        shutdown = next((event for event in reversed(events) if event.get("event") == "fleet_shutdown"), None)
        if shutdown is None or shutdown.get("graceful_shutdown") is not True:
            raise RuntimeError(f"Missing successful fleet_shutdown event: {shutdown}")
        stats = [event for event in events if event.get("event") == "bot_stats"]
        if len(stats) != self.count:
            raise RuntimeError(f"Expected {self.count} bot_stats events, got {len(stats)}")
        bad = [event for event in stats if not event.get("online") or int(event.get("auth_inputs_sent", 0)) <= 0]
        if bad:
            raise RuntimeError(f"Per-bot online/AuthInput statistics failed: {bad[:3]}")
        self.result["fleet_shutdown_event"] = shutdown
        self.result["bot_stats"] = stats
        output, propagation = self.wait_player_count(0, timeout=30)
        self.check(
            "fleet-graceful-shutdown",
            "PASS",
            f"all {self.count} bots disconnected cleanly",
            propagation_seconds=round(propagation, 3),
            output=" | ".join(output[-30:]),
            shutdown_event=shutdown,
        )

    def _profile(self, kind: str, player_count: int, reload_cycle: int, allocation_interval: int | None) -> str:
        if kind not in (CPU_BASELINE_KIND, CPU_LOAD_KIND, ALLOCATION_KIND):
            raise ValueError(f"unsupported baseline profile kind: {kind}")
        if self._profile_active or self.server is None:
            raise RuntimeError(f"cannot run profile {kind} while unavailable or another profile is active")
        command = f"spark profiler start --timeout {self.profile_seconds}"
        if allocation_interval is not None:
            command += f" --alloc --interval {allocation_interval}"
        self._require_budget(f"profile-{kind}")
        self._profile_active = True
        try:
            start, _, _ = self._dispatch(command, timeout=CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT)
            end = min(self._clock() + self.profile_seconds + PROFILE_COMPLETION_GRACE_SECONDS, self._effective_work_deadline())
            url: str | None = None
            while self._clock() < end:
                url = self._viewer_url(self.server.snapshot(), start)
                if url:
                    break
                recent = "\n".join(self.server.snapshot()[start:]).casefold()
                if "profiler status: failed" in recent or "incomplete profile data was discarded" in recent:
                    raise RuntimeError(f"Spark rejected {kind} profile")
                if not self.server.is_alive():
                    raise RuntimeError(f"BDS exited during {kind} profile")
                self._sleep(min(0.5, max(0.001, end - self._clock())), deadline=end, stage=f"profile-{kind} poll")
            if not url:
                stop_start, _, _ = self._dispatch("spark profiler stop", timeout=CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT)
                end = min(self._clock() + PROFILE_STOP_WAIT_SECONDS, self._effective_work_deadline())
                while self._clock() < end:
                    url = self._viewer_url(self.server.snapshot(), min(start, stop_start))
                    if url:
                        break
                    if not self.server.is_alive():
                        raise RuntimeError(f"BDS exited while finalizing {kind} profile")
                    self._sleep(min(0.5, max(0.001, end - self._clock())), deadline=end, stage=f"profile-{kind} finalization")
            if not url:
                raise RuntimeError(f"{kind} profile produced no Spark viewer URL")
            if any(profile.get("viewer_url") == url for profile in self.result["profiles"]):
                raise RuntimeError(f"profile {kind} reused viewer URL {url}")
            if player_count == 0:
                self.wait_player_count(0, timeout=20)
            else:
                self.assert_20_players(f"before-{kind}")
            self.result["profiles"].append(
                self._common_profile_metadata(
                    kind=kind,
                    viewer_url=url,
                    player_count=player_count,
                    reload_cycle=reload_cycle,
                    allocation_interval=allocation_interval,
                    command=command,
                )
            )
            self._write_results()
            self.check(f"profile-{kind}", "PASS", "distinct Spark viewer profile URL recorded with exact provenance", viewer_url=url)
            return url
        finally:
            self._profile_active = False

    def _wait_reload_complete(self, start: int, cycle: int) -> tuple[list[str], str, str, str]:
        if self.server is None:
            raise RuntimeError(f"BDS disappeared during /reload cycle {cycle}")
        end = min(self._clock() + RELOAD_WAIT_SECONDS, self._effective_work_deadline())
        while self._clock() < end:
            lines = self.server.snapshot()[start:]
            if any("reload complete." in line.casefold() for line in lines):
                disable, enable, completion = _ordered_reload_evidence(lines, cycle)
                return lines, disable, enable, completion
            if not self.server.is_alive():
                raise RuntimeError(f"BDS exited during Endstone /reload cycle {cycle}")
            self._sleep(min(0.25, max(0.001, end - self._clock())), deadline=end, stage=f"reload-{cycle} poll")
        raise TimeoutError(f"Endstone /reload cycle {cycle} did not complete before the work deadline")

    def _reload_for_repetition(self, repetition: int, reload_index: int, identity: tuple[int, float]) -> None:
        if self._observer is not None:
            self._observer.disarm(deadline=self._effective_work_deadline())
        self._reload(reload_index, identity)
        current = self._select_live_identity()
        if current != identity:
            raise RuntimeError(f"BDS child identity changed after reload {reload_index}: {identity!r} != {current!r}")
        if self._observer is not None:
            self._observer.reacquire(
                timeout=RELOAD_MAPPING_GRACE_SECONDS,
                deadline=self._effective_work_deadline(),
                expected_identity=identity,
            )
        record = self.result["plugin_reload_cycles"][-1]
        record.update({"repetition": repetition, "reload_index": reload_index})
        self._write_results()

    def _post_reload_profile(self, repetition: int, reload_index: int) -> str:
        if self.server is None or self._observer is None:
            raise RuntimeError("post-reload profile requires a live server and observer")
        self._require_budget(f"post-reload-profile-{repetition}")
        identity = self._select_live_identity()
        self.assert_20_players(f"before-post-reload-{repetition}")
        repetition_deadline = self._effective_work_deadline()
        mapping = self._observer.arm(
            repetition,
            reload_index,
            expected_identity=identity,
            deadline=repetition_deadline,
        )
        self.result["post_reload_diagnostic"] = {
            **self._observer.result(),
            "status": "armed",
            "mapping_lifetime": mapping["mapping_lifetime"],
            "mapping_name": mapping["mapping_name"],
        }
        self._write_results()
        self._profile_active = True
        start: int | None = None
        interval_completed = False
        profile_started = self._clock()
        try:
            self._require_budget(f"post-reload-profile-{repetition}-start", cost=float(self.profile_seconds))
            start, _, _ = self._dispatch(POST_RELOAD_PROFILE_COMMAND, timeout=CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT)
            end = min(profile_started + self.profile_seconds + PROFILE_COMPLETION_GRACE_SECONDS, repetition_deadline)
            url: str | None = None
            while self._clock() < end:
                state = self._observer.poll()
                if self._observer.failed:
                    self._sync_observer_result()
                    raise RuntimeError(f"post-reload diagnostic availability failure: {state}")
                if self._observer.triggered:
                    self._sync_observer_result()
                    if self._clock() - profile_started >= self.profile_seconds:
                        try:
                            self._dispatch("spark profiler stop", timeout=CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT)
                        finally:
                            raise RuntimeError("post-reload lifecycle/tick stall diagnostic triggered")
                    raise RuntimeError("post-reload lifecycle/tick stall diagnostic triggered before profile completion")
                url = self._viewer_url(self.server.snapshot(), start)
                if url:
                    if self._clock() - profile_started < self.profile_seconds:
                        raise DeadlineExceeded("shortened post-reload profile cannot count as completed")
                    break
                recent = "\n".join(self.server.snapshot()[start:]).casefold()
                if "profiler status: failed" in recent or "incomplete profile data was discarded" in recent:
                    raise RuntimeError(f"Spark rejected post-reload profile {repetition}")
                if not self.server.is_alive():
                    raise RuntimeError(f"BDS exited during post-reload profile {repetition}")
                delay = min(0.25, max(0.001, end - self._clock()))
                self._sleep(delay, deadline=end, stage="post-reload profile poll")
            if not url:
                if self._clock() - profile_started < self.profile_seconds:
                    raise DeadlineExceeded("repetition deadline shortened the requested post-reload profile")
                stop_start, _, _ = self._dispatch("spark profiler stop", timeout=CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT)
                end = min(self._clock() + PROFILE_STOP_WAIT_SECONDS, self._effective_work_deadline())
                while self._clock() < end:
                    state = self._observer.poll()
                    if self._observer.failed or self._observer.triggered:
                        self._sync_observer_result()
                        raise RuntimeError(f"post-reload diagnostic failed while finalizing profile: {state}")
                    url = self._viewer_url(self.server.snapshot(), min(start, stop_start))
                    if url:
                        break
                    if not self.server.is_alive():
                        raise RuntimeError(f"BDS exited while finalizing post-reload profile {repetition}")
                    self._sleep(min(0.5, max(0.001, end - self._clock())), deadline=end, stage="post-reload finalization")
            if not url:
                raise RuntimeError(f"post-reload profile {repetition} produced no Spark viewer URL")
            if self._clock() - profile_started < self.profile_seconds:
                raise DeadlineExceeded("post-reload profile completed before its requested duration")
            if any(profile.get("viewer_url") == url for profile in self.result["profiles"]):
                raise RuntimeError(f"post-reload profile {repetition} reused viewer URL {url}")
            self.assert_20_players(f"after-post-reload-{repetition}")
            profile = self._common_profile_metadata(
                kind=f"{POST_RELOAD_PROFILE_KIND_PREFIX}{repetition}",
                viewer_url=url,
                player_count=BOT_COUNT,
                reload_cycle=reload_index,
                allocation_interval=4,
                command=POST_RELOAD_PROFILE_COMMAND,
            )
            profile.update({"repetition": repetition, "reload_index": reload_index, "mapping_lifetime": mapping["mapping_lifetime"]})
            self.result["profiles"].append(profile)
            self.result["post_reload_profiles"].append(copy.deepcopy(profile))
            self._sync_observer_result()
            self._require_budget(f"post-reload-profile-{repetition}-complete")
            interval_completed = True
            self.check(
                f"post-reload-profile-{repetition}",
                "PASS",
                "automatic 30-second 4ms Spark profile completed with a distinct viewer URL",
                viewer_url=url,
                repetition=repetition,
                reload_index=reload_index,
            )
            return url
        finally:
            if interval_completed:
                self._observer.disarm(deadline=self._effective_work_deadline())
            self._profile_active = False

    def _validate_workload_success(self) -> None:
        profiles = self.result.get("profiles") or []
        baseline = [CPU_BASELINE_KIND, CPU_LOAD_KIND, ALLOCATION_KIND]
        expected_kinds = baseline + [f"{POST_RELOAD_PROFILE_KIND_PREFIX}{index}" for index in range(1, 11)]
        if [profile.get("kind") for profile in profiles] != expected_kinds:
            raise RuntimeError(f"post-reload profile kinds are not exact: {profiles!r}")
        if len(profiles) != 13:
            raise RuntimeError(f"expected 13 profiles, got {len(profiles)}")
        urls = [str(profile.get("viewer_url") or "").strip() for profile in profiles]
        if any(not url for url in urls) or len(set(urls)) != len(urls):
            raise RuntimeError("post-reload profile URLs are not distinct and nonempty")
        expected_baseline = (
            (CPU_BASELINE_KIND, 0, 0, None, "spark profiler start --timeout 30"),
            (CPU_LOAD_KIND, BOT_COUNT, 0, None, "spark profiler start --timeout 30"),
            (ALLOCATION_KIND, BOT_COUNT, 0, ALLOCATION_INTERVAL_BYTES, "spark profiler start --timeout 30 --alloc --interval 4096"),
        )
        for profile, expected in zip(profiles[:3], expected_baseline):
            actual = (profile.get("kind"), profile.get("player_count"), profile.get("reload_cycle"), profile.get("allocation_interval"), profile.get("command"))
            if actual != expected:
                raise RuntimeError(f"baseline profile metadata is not exact: expected={expected!r} observed={actual!r}")
        for repetition, profile in enumerate(profiles[3:], 1):
            expected = (f"{POST_RELOAD_PROFILE_KIND_PREFIX}{repetition}", BOT_COUNT, repetition * 3, 4, POST_RELOAD_PROFILE_COMMAND)
            actual = (profile.get("kind"), profile.get("player_count"), profile.get("reload_cycle"), profile.get("allocation_interval"), profile.get("command"))
            if actual != expected or profile.get("repetition") != repetition or profile.get("reload_index") != repetition * 3:
                raise RuntimeError(f"post-reload profile metadata is not exact: expected={expected!r} observed={actual!r}")
        reloads = self.result.get("plugin_reload_cycles") or []
        if len(reloads) != TOTAL_RELOADS:
            raise RuntimeError(f"expected exactly 30 published reload records, got {len(reloads)}")
        identities: set[tuple[int, float]] = set()
        for index, record in enumerate(reloads, 1):
            if record.get("cycle") != index or record.get("reload_index") != index or record.get("repetition") != (index - 1) // 3 + 1:
                raise RuntimeError(f"reload order/index drift at {index}: {record!r}")
            evidence = record.get("reload_evidence_lines")
            if not isinstance(evidence, list) or len(evidence) != 3:
                raise RuntimeError(f"reload evidence is missing at {index}")
            if _ordered_reload_evidence(evidence, index) != (record.get("spark_disable_evidence"), record.get("spark_enable_evidence"), record.get("reload_completion")):
                raise RuntimeError(f"reload evidence order drift at {index}")
            identity = (int(record["bds_pid"]), float(record["bds_create_time"]))
            before = (int(record["before_bds_pid"]), float(record["before_bds_create_time"]))
            if identity != before or not math.isfinite(identity[1]) or identity[1] <= 0:
                raise RuntimeError(f"BDS identity drift at reload {index}")
            identities.add(identity)
        if len(identities) != 1:
            raise RuntimeError(f"reload records do not share one BDS identity: {identities!r}")
        diagnostic = self.result.get("post_reload_diagnostic") or {}
        if diagnostic.get("triggered") is True or diagnostic.get("status") != "not-triggered":
            raise RuntimeError(f"post-reload diagnostic did not complete healthy: {diagnostic!r}")

    def _stop_observer(self) -> bool:
        if self._observer is None:
            return True
        if self._observer_stopped:
            return True
        stopped = self._observer.stop(timeout=5.0, deadline=self._effective_hard_deadline())
        self._sync_observer_result()
        if not stopped:
            raise RuntimeError(f"observer process termination was not clean: {self._observer.result()}")
        self._observer_stopped = True
        return True

    def _bounded_server_close(self) -> None:
        server = self.server
        if server is None:
            return
        reader = getattr(server, "_reader", None)
        if reader is not None:
            reader.join(timeout=max(0.0, min(3.0, self._remaining())))
        process = getattr(server, "process", None)
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
        log = getattr(server, "_log", None)
        if log is not None:
            log.close()
            server._log = None

    def _bounded_force_kill_server(self) -> None:
        server = self.server
        if server is None:
            return
        records = server.process_tree_snapshot()
        root_status = getattr(server, "_root_identity_status", "unknown")
        if root_status not in ("verified", "absent"):
            raise RuntimeError(f"cannot force cleanup with unverified wrapper identity: {root_status}")
        alive = [record for record in records if record.get("alive") is True and record.get("identity_match") is True]
        for record in alive:
            timeout = clipped_timeout(30.0, self._effective_hard_deadline(), now=self._clock())
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(record["pid"]), "/T", "/F"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            else:
                try:
                    os.kill(int(record["pid"]), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        process = getattr(server, "process", None)
        if process is not None and process.poll() is None:
            process.wait(timeout=clipped_timeout(15.0, self._effective_hard_deadline(), now=self._clock()))
        after = server.process_tree_snapshot()
        residual = [record for record in after if record.get("alive") is True and record.get("identity_match") is True]
        if residual:
            raise RuntimeError(f"residual managed process after bounded force cleanup: {residual[:4]}")

    def stop_server_for_phase_change(self, phase_name: str = "phase-change") -> None:
        if self.server is None:
            return
        self._set_phase_shutdown_context(phase_name)
        timeout = self._clip(60.0, deadline=self._effective_hard_deadline(), stage=f"{phase_name} shutdown")
        graceful = self.server.graceful_stop(timeout)
        self._record_phase_lifecycle()
        if not graceful:
            self._bounded_force_kill_server()
            self._record_phase_lifecycle()
            raise RuntimeError(f"BDS did not stop gracefully during {phase_name}")
        self._bounded_server_close()
        self.server = None

    def shutdown(self) -> None:
        if self.server is None:
            return
        timeout = self._clip(60.0, deadline=self._effective_hard_deadline(), stage="final BDS shutdown")
        graceful = self.server.graceful_stop(timeout)
        self.record_server_lifecycle()
        if not graceful:
            self._bounded_force_kill_server()
            self.record_server_lifecycle()
            self.result["shutdown_status"] = "forced"
            self._write_results()
            raise RuntimeError("BDS did not shut down gracefully within bounded timeout")
        self._bounded_server_close()
        leftovers = self.residual_processes()
        if leftovers:
            raise RuntimeError("Residual BDS process detected after shutdown: " + " | ".join(leftovers[:5]))
        self.result["shutdown_status"] = "graceful"
        self.record_server_lifecycle()
        self.check("shutdown", "PASS", "graceful; no residual BDS process")

    def install_artifacts(self) -> None:
        self._require_budget("artifact discovery")
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_install_artifacts_worker,
            args=(str(self.bot_binary), str(self.metadata_path)),
            name="spark51-artifact-installer",
        )
        process.daemon = False
        process.start()
        try:
            process.join(timeout=self._clip(300.0, deadline=self._effective_work_deadline(), stage="artifact discovery"))
            if process.is_alive():
                process.terminate()
                process.join(timeout=max(0.0, self._remaining(self._effective_work_deadline())))
                raise DeadlineExceeded("artifact discovery exceeded the work deadline")
            if process.exitcode != 0:
                raise RuntimeError(f"artifact discovery child failed with exit code {process.exitcode}")
            if not self.metadata_path.is_file():
                raise RuntimeError("artifact discovery child did not produce metadata")
            self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            self._record_candidate_provenance()
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=max(0.0, self._remaining(self._effective_work_deadline())))

    def _finalize_cleanup(self) -> None:
        observer_stopped = True
        try:
            if self._observer is not None:
                self._stop_observer()
        except Exception as exc:  # noqa: BLE001 - cleanup must continue with bounded evidence
            self.result.setdefault("cleanup_errors", []).append({"operation": "observer stop", "error": f"{type(exc).__name__}: {exc}"})
            self.result["status"] = "FAIL"
            observer_stopped = False
        if self._observer is not None and self._observer.result()["process"].get("stopped") is not True:
            observer_stopped = False
        if not observer_stopped:
            self.result["state"] = "failed"
            self.result["cleanup_blocked"] = "observer-not-proven-dead"
            self.result["completed_at"] = now_iso()
            try:
                self._write_results()
            except Exception as exc:  # noqa: BLE001 - preserve the observer failure evidence
                self.result.setdefault("cleanup_errors", []).append({"operation": "blocked cleanup persistence", "error": f"{type(exc).__name__}: {exc}"})
            return
        if self.bot is not None and self.bot.is_alive():
            try:
                self.stop_fleet()
            except Exception as exc:  # noqa: BLE001 - cleanup must continue
                self.result.setdefault("cleanup_errors", []).append({"operation": "fleet stop", "error": f"{type(exc).__name__}: {exc}"})
        if self.server is not None:
            try:
                if self.server.is_alive():
                    self._bounded_force_kill_server()
                self._bounded_server_close()
            except Exception as exc:  # noqa: BLE001 - cleanup must continue at hard deadline
                self.result.setdefault("cleanup_errors", []).append({"operation": "BDS cleanup", "error": f"{type(exc).__name__}: {exc}"})
        self.result["completed_at"] = now_iso()
        try:
            self.split_logs()
            self._write_results()
        except Exception as exc:  # noqa: BLE001 - final persistence is best effort
            self.result.setdefault("cleanup_errors", []).append({"operation": "final persistence", "error": f"{type(exc).__name__}: {exc}"})

    def execute_diagnostic(self) -> int:
        stage = "initialization"
        try:
            if self._control is None:
                raise RuntimeError("diagnostic requires the external Windows supervisor control block")
            self._control.set_controller_status(CONTROL_CONTROLLER_RUNNING)
            stage = "artifact-discovery"
            self._run_work_stage(stage, self.install_artifacts)
            stage = "world-bootstrap"
            self._run_work_stage(stage, self.bootstrap_scenario_world)
            stage = "bds-start"
            self._run_work_stage(stage, self.start_server)
            self._run_work_stage("post-start-initialization", self.wait_post_start_initialization)
            self._run_work_stage("basic-commands", self.run_basic_commands)
            stage = "cpu-baseline"
            self._run_work_stage("cpu-baseline-player-wait", lambda: self.wait_player_count(0, timeout=45))
            self._run_work_stage(stage, lambda: self._profile(CPU_BASELINE_KIND, 0, 0, None))
            stage = "20-player-fleet"
            self._run_work_stage(stage, self.start_fleet)
            self._sleep(20.0, stage="fleet settle")
            self._run_work_stage("20-player-assertion", lambda: self.assert_20_players("before-profiles"))
            stage = "20-player-cpu"
            self._run_work_stage(stage, lambda: self._profile(CPU_LOAD_KIND, BOT_COUNT, 0, None))
            stage = "20-player-allocation-4096"
            self._run_work_stage(stage, lambda: self._profile(ALLOCATION_KIND, BOT_COUNT, 0, ALLOCATION_INTERVAL_BYTES))
            if self.server is None:
                raise RuntimeError("BDS disappeared before observer startup")
            baseline_identity = self._select_live_identity()
            self._observer = SupervisorObserverClient(
                self.root,
                self._supervisor_control_path,
                self.root / "combined-health-capture-windows",
                self.log_path,
                clock=self._clock,
                sleeper=self._sleeper,
            )
            for repetition in range(1, RELOAD_REPETITIONS + 1):
                repetition_deadline, effective_deadline = admit_repetition(self._clock(), self._work_deadline)
                self._active_repetition_deadline = effective_deadline
                self._control.set_repetition(repetition, int(repetition_deadline * 1_000_000_000))
                self.result.setdefault("repetition_deadlines", []).append(
                    {
                        "repetition": repetition,
                        "deadline_seconds": repetition_deadline,
                        "effective_deadline_seconds": effective_deadline,
                    }
                )
                self._write_results()
                try:
                    for reload_number in range(1, RELOADS_PER_REPETITION + 1):
                        reload_index = (repetition - 1) * RELOADS_PER_REPETITION + reload_number
                        self._reload_for_repetition(repetition, reload_index, baseline_identity)
                    self._post_reload_profile(repetition, repetition * RELOADS_PER_REPETITION)
                    self._dispatch("spark profiler info", timeout=15.0)
                    self.check(
                        f"post-reload-command-responsive-{repetition}",
                        "PASS",
                        "file-transport command remained responsive after automatic profile completion",
                        repetition=repetition,
                    )
                    if self._clock() >= effective_deadline:
                        raise DeadlineExceeded(f"repetition {repetition} exceeded its aggregate deadline")
                finally:
                    self._active_repetition_deadline = None
                    self._control.clear_repetition()
                    self._write_results()
            self._validate_workload_success()
            self._stop_observer()
            stage = "graceful-shutdown"
            self._stop_fleet_once()
            self._set_phase_shutdown_context("candidate-final-shutdown")
            self.shutdown()
            self._set_shutdown_evidence()
            self._validate_final_shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self._control.set_controller_status(CONTROL_CONTROLLER_COMPLETE)
            self._write_results()
            return 0
        except Exception as exc:  # noqa: BLE001 - integration failures become evidence
            if self._control is not None:
                try:
                    self._control.set_controller_status(CONTROL_CONTROLLER_FAILURE)
                    self._control.clear_repetition()
                except (OSError, RuntimeError):
                    pass
            self.result["status"] = "FAIL"
            self.result["state"] = "failed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1200]
            diagnostic = traceback.format_exc()
            try:
                self.diagnostics.write_text(diagnostic, encoding="utf-8")
            except OSError:
                pass
            self._write_results()
            return 1
        finally:
            self._finalize_cleanup()
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def validate_result_contract(result_path: pathlib.Path, env: Mapping[str, str]) -> None:
    if not result_path.is_file():
        raise RuntimeError("fleet-spark-result.json is missing")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise TypeError("result root is not an object")
    if result.get("status") != "PASS" or result.get("state") != "completed":
        raise RuntimeError(f"diagnostic result did not pass: {result.get('status')!r}/{result.get('state')!r}")
    if env.get("EXPECTED_SPARK_SHA", "").strip().lower() != SPARK_CANDIDATE_SHA:
        raise RuntimeError("candidate SHA env drift")
    if result.get("spark_sha") != SPARK_CANDIDATE_SHA:
        raise RuntimeError("result Spark SHA drift")
    expected_artifact_id = _strict_positive_id(env.get("EXPECTED_SPARK_ARTIFACT_ID", EXPECTED_SPARK_ARTIFACT_ID), "EXPECTED_SPARK_ARTIFACT_ID")
    if expected_artifact_id != EXPECTED_SPARK_ARTIFACT_ID:
        raise RuntimeError("Spark artifact ID env drift")
    if str(env.get("EXPECTED_SPARK_ARTIFACT_DIGEST", EXPECTED_SPARK_ARTIFACT_DIGEST)).strip().lower() != EXPECTED_SPARK_ARTIFACT_DIGEST:
        raise RuntimeError("Spark artifact digest env drift")
    byte_evidence = result.get("spark_byte_evidence")
    required_bytes = {"artifact_id", "artifact_digest", "download_sha256", "payload_dll_sha256", "installed_dll_sha256"}
    if not isinstance(byte_evidence, dict) or not required_bytes.issubset(byte_evidence):
        raise RuntimeError("result is missing exact Spark artifact byte evidence")
    if (
        byte_evidence.get("artifact_id") != EXPECTED_SPARK_ARTIFACT_ID
        or byte_evidence.get("artifact_digest") != EXPECTED_SPARK_ARTIFACT_DIGEST
        or byte_evidence.get("download_sha256") != EXPECTED_SPARK_ARTIFACT_DIGEST
        or byte_evidence.get("payload_dll_sha256") != byte_evidence.get("installed_dll_sha256")
    ):
        raise RuntimeError("result Spark artifact byte evidence is inconsistent")
    provenance = result.get("artifact_provenance")
    spark_provenance = provenance.get("spark") if isinstance(provenance, dict) else None
    if not isinstance(spark_provenance, dict) or spark_provenance.get("artifact_id") != EXPECTED_SPARK_ARTIFACT_ID or spark_provenance.get("artifact_digest") != EXPECTED_SPARK_ARTIFACT_DIGEST:
        raise RuntimeError("result Spark provenance is missing exact artifact identity")
    verdict_path_value = env.get("SPARK51_SUPERVISOR_VERDICT") or result.get("supervisor_verdict", {}).get("path")
    verdict_path = pathlib.Path(verdict_path_value) if isinstance(verdict_path_value, str) and verdict_path_value else result_path.with_name("supervisor-verdict.json")
    verdict = _read_json(verdict_path)
    if not isinstance(verdict, dict):
        raise TypeError("supervisor verdict is missing")
    if (
        verdict.get("status") != "PASS"
        or verdict.get("state") != "complete"
        or verdict.get("controller_exit_code") != 0
        or verdict.get("observer_exit_code") != 0
        or verdict.get("controller_job_empty") is not True
        or verdict.get("observer_job_empty") is not True
        or verdict.get("observer_death_confirmed") is not True
        or verdict.get("forced_observer_termination") is True
    ):
        raise RuntimeError(f"supervisor verdict did not prove containment: {verdict!r}")
    deadline = result.get("deadline")
    if not isinstance(deadline, dict) or deadline.get("hard_seconds") != HARD_DEADLINE_SECONDS or deadline.get("cleanup_reserve_seconds") != SHUTDOWN_RESERVE_SECONDS:
        raise RuntimeError("diagnostic deadline contract drift")
    if result.get("reload_repetitions") != RELOAD_REPETITIONS or result.get("reloads_per_repetition") != 3:
        raise RuntimeError("reload repetition contract drift")
    if result.get("total_reload_target") != TOTAL_RELOADS or result.get("post_reload_profile_target") != 10:
        raise RuntimeError("diagnostic coverage target drift")
    profiles = result.get("profiles")
    if not isinstance(profiles, list) or len(profiles) != 13:
        raise RuntimeError("diagnostic profile count is not 13")
    reloads = result.get("plugin_reload_cycles")
    if not isinstance(reloads, list) or len(reloads) != TOTAL_RELOADS:
        raise RuntimeError("diagnostic reload count is not 30")
    diagnostic = result.get("post_reload_diagnostic")
    if not isinstance(diagnostic, dict) or diagnostic.get("status") != "not-triggered":
        raise RuntimeError("diagnostic observer did not finish without a trigger")
    process = diagnostic.get("process")
    if not isinstance(process, dict) or process.get("alive") is True or process.get("forced_termination") is True:
        raise RuntimeError("diagnostic observer process was not proven stopped")
    for index, record in enumerate(reloads, 1):
        if not isinstance(record, dict) or record.get("cycle") != index or record.get("reload_index") != index:
            raise RuntimeError(f"reload {index} ordering drift")
        if record.get("transport") != "stdin" or record.get("command_published") is not True:
            raise RuntimeError(f"reload {index} did not use the true stdin reload path")
    for profile in profiles:
        for key in ("spark_artifact_id", "spark_artifact_digest", "spark_download_sha256", "spark_payload_dll_sha256", "spark_installed_dll_sha256"):
            if key not in profile:
                raise RuntimeError(f"profile {profile.get('kind')!r} is missing {key} evidence")
        if profile.get("spark_artifact_id") != EXPECTED_SPARK_ARTIFACT_ID or profile.get("spark_artifact_digest") != EXPECTED_SPARK_ARTIFACT_DIGEST or profile.get("spark_download_sha256") != EXPECTED_SPARK_ARTIFACT_DIGEST or profile.get("spark_payload_dll_sha256") != profile.get("spark_installed_dll_sha256"):
            raise RuntimeError(f"profile {profile.get('kind')!r} has inconsistent Spark byte evidence")
    for repetition, profile in enumerate(profiles[3:], 1):
        if profile.get("command") != POST_RELOAD_PROFILE_COMMAND or profile.get("reload_index") != repetition * 3:
            raise RuntimeError(f"post-reload profile {repetition} command/index drift")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--supervised-controller", action="store_true")
    parser.add_argument("--observer", action="store_true")
    parser.add_argument("--observer-root", type=pathlib.Path)
    parser.add_argument("--control-path", type=pathlib.Path)
    parser.add_argument("--platform", choices=["windows"])
    parser.add_argument("--bot")
    parser.add_argument("--profile-seconds", type=int, default=30)
    parser.add_argument("--validate-result", action="store_true")
    parser.add_argument("--result", type=pathlib.Path, default=pathlib.Path("fleet-spark-result.json"))
    args = parser.parse_args()
    if args.observer:
        if args.observer_root is None or args.control_path is None:
            parser.error("--observer-root and --control-path are required for the observer")
        return run_autonomous_observer(args.observer_root, args.control_path)
    if args.validate_result:
        validate_result_contract(args.result, os.environ)
        print("Spark #51 post-reload diagnostic result contract PASS")
        return 0
    if args.platform != "windows" or not args.bot:
        parser.error("--platform windows and --bot are required for the diagnostic run")
    return Spark51PostReloadDiagnosticValidation(pathlib.Path(args.bot), args.profile_seconds).execute_diagnostic()


if __name__ == "__main__":
    raise SystemExit(main())
