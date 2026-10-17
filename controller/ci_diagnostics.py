from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Callable
from typing import Any

CI_DIAGNOSTICS_MAGIC = 0x454E4453544F4E45
CI_DIAGNOSTICS_VERSION = 2
CI_DIAGNOSTICS_READY = 1
CI_DIAGNOSTICS_HEADER_SIZE = 5 * 8
CI_DIAGNOSTICS_RECORD_COUNT = 15
CI_DIAGNOSTICS_RECORD_SIZE = 8 * 8
CI_DIAGNOSTICS_REGION_SIZE = CI_DIAGNOSTICS_HEADER_SIZE + CI_DIAGNOSTICS_RECORD_COUNT * CI_DIAGNOSTICS_RECORD_SIZE
_PHASE_MASK = 0xFFFF
_TRANSITION_MASK = (1 << 48) - 1

_CONTEXT_NAMES = (
    "ApplicationTick",
    "PluginTick",
    "ApplicationCommand",
    "PluginCommand",
    "Timeout",
    "Profiler",
    "Completion",
    "Notification",
    "SamplerLifecycleMain",
    "SamplerLifecycleLiveExport",
    "TimeoutWorker",
    "Export",
    "SamplerWorker",
    "Capture",
    "AggregatorWorker",
)
_PHASE_NAMES = {
    0: "Unknown",
    4: "ApplicationTickEnter",
    5: "ApplicationTickExit",
    6: "ApplicationTickExceptionalExit",
    7: "PluginTickEnter",
    8: "PluginTickExit",
    9: "PluginTickExceptionalExit",
    13: "ApplicationCommandEnter",
    14: "ApplicationCommandExit",
    15: "ApplicationCommandExceptionalExit",
    16: "PluginCommandEnter",
    17: "PluginCommandExit",
    18: "PluginCommandExceptionalExit",
    32: "TimeoutArm",
    33: "TimeoutCancel",
    34: "TimeoutCompletion",
    35: "TimeoutFired",
    48: "ProfilerStart",
    49: "ProfilerStartFailed",
    50: "ProfilerStopSamplingEnter",
    51: "ProfilerStopSamplingExit",
    52: "ProfilerStopSamplingExceptionalExit",
    53: "ProfilerShutdownEnter",
    54: "ProfilerShutdownExit",
    55: "ProfilerShutdownExceptionalExit",
    68: "CompletionEnter",
    69: "CompletionExit",
    70: "CompletionExceptionalExit",
    80: "NotificationEnter",
    81: "NotificationExit",
    82: "NotificationExceptionalExit",
    96: "SamplerStart",
    97: "SamplerStartFailed",
    98: "SamplerDbgHelpAcquired",
    99: "SamplerStopRequested",
    100: "SamplerCancel",
    101: "SamplerJoin",
    102: "SamplerJoinComplete",
    103: "SamplerAggregatorJoin",
    104: "SamplerAggregatorJoinComplete",
    105: "SamplerDisarm",
    106: "SamplerDisarmComplete",
    107: "SamplerPause",
    108: "SamplerPauseJoin",
    109: "SamplerPauseJoinComplete",
    110: "SamplerResume",
    111: "SamplerWorkerStart",
    112: "SamplerWorkerRunning",
    113: "SamplerWorkerExit",
    114: "AggregatorWorkerStart",
    115: "AggregatorWorkerExit",
    128: "CaptureEnter",
    129: "CaptureOpenFailed",
    130: "CaptureSuspendAttempt",
    131: "CaptureSuspended",
    132: "CaptureSuspendFailed",
    133: "CaptureContextCall",
    134: "CaptureContextReturn",
    135: "CaptureWalkCall",
    136: "CaptureWalkReturn",
    137: "CaptureResumeAttempt",
    138: "CaptureResumed",
    139: "CaptureResumeFailed",
    140: "CaptureTargetExited",
    141: "CaptureComplete",
    142: "CaptureFailed",
    161: "ExportEnter",
    162: "ExportComplete",
    163: "ExportFailed",
    164: "ExportCompletionQueued",
    165: "ExportCompletionFallback",
}

ByteReader = Callable[[int, int], bytes]


class _MalformedMapping(Exception):
    pass


def mapping_name_for_pid(pid: int) -> str:
    if isinstance(pid, bool) or pid <= 0 or pid > 0xFFFFFFFF:
        raise ValueError("invalid process id")
    return f"Local\\EndstoneSparkCiDiag-v2-{pid}"


def _unavailable(mapping_name: str | None, reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "mapping_name": mapping_name,
        "reason": reason,
        "mapping_lifetime": None,
        "schema_version": None,
        "contexts": [],
    }


def _reader_call(reader: object, offset: int, size: int) -> bytes:
    if isinstance(reader, (bytes, bytearray, memoryview)):
        value = reader[offset : offset + size]
    elif callable(reader):
        value = reader(offset, size)
    elif hasattr(reader, "read_at"):
        value = reader.read_at(offset, size)  # type: ignore[attr-defined]
    elif hasattr(reader, "read"):
        try:
            value = reader.read(offset, size)  # type: ignore[attr-defined]
        except TypeError:
            seek = reader.seek  # type: ignore[attr-defined]
            seek(offset)
            value = reader.read(size)  # type: ignore[attr-defined]
    else:
        raise TypeError("byte reader is not callable")
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("byte reader did not return bytes")
    return bytes(value)


def _header(data: bytes) -> tuple[int, int, int, int, int] | None:
    if len(data) != CI_DIAGNOSTICS_HEADER_SIZE:
        return None
    return struct.unpack("<5Q", data)


def _valid_header(header: tuple[int, int, int, int, int], data_length: int) -> bool:
    magic, version, size, _generation, ready = header
    return (
        magic == CI_DIAGNOSTICS_MAGIC
        and version == CI_DIAGNOSTICS_VERSION
        and size == CI_DIAGNOSTICS_REGION_SIZE
        and data_length >= size
        and ready == CI_DIAGNOSTICS_READY
    )


def _reader_length(reader: object) -> int:
    if isinstance(reader, (bytes, bytearray, memoryview)):
        return len(reader)
    return CI_DIAGNOSTICS_REGION_SIZE


def _phase_name(value: int) -> str:
    return _PHASE_NAMES.get(value, "unknown")


def _record(context_value: int, reader: object) -> dict[str, Any]:
    offset = CI_DIAGNOSTICS_HEADER_SIZE + context_value * CI_DIAGNOSTICS_RECORD_SIZE
    sequence_before_data = _reader_call(reader, offset, 8)
    if len(sequence_before_data) != 8:
        raise _MalformedMapping("short record sequence")
    sequence_before = struct.unpack("<Q", sequence_before_data)[0]
    if sequence_before & 1:
        return {
            "context": _CONTEXT_NAMES[context_value],
            "context_value": context_value,
            "status": "unknown",
        }
    record_data = _reader_call(reader, offset, CI_DIAGNOSTICS_RECORD_SIZE)
    sequence_after_data = _reader_call(reader, offset, 8)
    if len(record_data) != CI_DIAGNOSTICS_RECORD_SIZE or len(sequence_after_data) != 8:
        raise _MalformedMapping("short record")
    sequence_after = struct.unpack("<Q", sequence_after_data)[0]
    record_sequence = struct.unpack("<Q", record_data[:8])[0]
    if sequence_before != record_sequence or sequence_before != sequence_after or sequence_after & 1:
        return {
            "context": _CONTEXT_NAMES[context_value],
            "context_value": context_value,
            "status": "unknown",
        }
    generation, packed_phase, worker_tid, target_tid, suspend_count, resume_count, walk_count = struct.unpack(
        "<7Q", record_data[8:]
    )
    phase_value = packed_phase & _PHASE_MASK
    return {
        "context": _CONTEXT_NAMES[context_value],
        "context_value": context_value,
        "status": "available",
        "sequence": sequence_after,
        "session_generation": generation,
        "phase": _phase_name(phase_value),
        "phase_value": phase_value,
        "transition_sequence": (packed_phase >> 16) & _TRANSITION_MASK,
        "worker_tid": worker_tid,
        "target_tid": target_tid,
        "suspend_success_count": suspend_count,
        "resume_success_count": resume_count,
        "walk_call_count": walk_count,
    }


def read_ci_diagnostics(pid: int | None, byte_reader: object | None = None) -> dict[str, Any]:
    """Read one bounded v2 mapping without creating or waiting on it."""

    try:
        parsed_pid = int(pid) if pid is not None else 0
    except (TypeError, ValueError):
        parsed_pid = 0
    if isinstance(pid, bool) or parsed_pid <= 0 or parsed_pid > 0xFFFFFFFF:
        return _unavailable(None, "invalid-pid")
    name = mapping_name_for_pid(parsed_pid)
    closer: Callable[[], None] = lambda: None
    reader = byte_reader
    if reader is None:
        if os.name != "nt":
            return _unavailable(name, "unsupported-platform")
        try:
            reader, closer = _open_windows_reader(name)
        except (OSError, RuntimeError, AttributeError, TypeError):
            return _unavailable(name, "mapping-unavailable")
    try:
        header_before_data = _reader_call(reader, 0, CI_DIAGNOSTICS_HEADER_SIZE)
        header_before = _header(header_before_data)
        if header_before is None or not _valid_header(header_before, _reader_length(reader)):
            reason = "mapping-too-small"
            if header_before is not None:
                reason = "not-ready" if header_before[4] != CI_DIAGNOSTICS_READY else "malformed-header"
            return _unavailable(name, reason)
        contexts = [_record(context_value, reader) for context_value in range(CI_DIAGNOSTICS_RECORD_COUNT)]
        header_after_data = _reader_call(reader, 0, CI_DIAGNOSTICS_HEADER_SIZE)
        header_after = _header(header_after_data)
        if header_after is None or not _valid_header(header_after, _reader_length(reader)):
            reason = "not-ready" if header_after is not None and header_after[4] != CI_DIAGNOSTICS_READY else "mapping-changed"
            return _unavailable(name, reason)
        if header_before[3] != header_after[3] or header_before[4] != header_after[4]:
            return _unavailable(name, "mapping-changed")
        status = "unknown" if any(context["status"] == "unknown" for context in contexts) else "available"
        return {
            "status": status,
            "mapping_name": name,
            "mapping_lifetime": header_before[3],
            "schema_version": header_before[1],
            "contexts": contexts,
        }
    except Exception:  # noqa: BLE001 - diagnostics must never replace a timeout
        return _unavailable(name, "mapping-unavailable")
    finally:
        closer()


def _open_windows_reader(name: str) -> tuple[ByteReader, Callable[[], None]]:
    if os.name != "nt":
        raise RuntimeError("Windows mapping is unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_mapping = kernel32.OpenFileMappingW
    open_mapping.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    open_mapping.restype = ctypes.c_void_p
    map_view = kernel32.MapViewOfFile
    map_view.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
    map_view.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    unmap_view = kernel32.UnmapViewOfFile
    unmap_view.argtypes = [ctypes.c_void_p]
    unmap_view.restype = ctypes.c_int
    file_map_read = 0x0004
    handle = open_mapping(file_map_read, 0, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenFileMappingW failed")
    view = map_view(handle, file_map_read, 0, 0, CI_DIAGNOSTICS_REGION_SIZE)
    if not view:
        close_handle(handle)
        raise OSError(ctypes.get_last_error(), "MapViewOfFile failed")

    def read_at(offset: int, size: int) -> bytes:
        return ctypes.string_at(view + offset, size)  # type: ignore[operator]

    def close() -> None:
        unmap_view(view)
        close_handle(handle)

    return read_at, close


class CiDiagnosticsReader:
    def __init__(self, pid: int, byte_reader: object | None = None) -> None:
        self.pid = pid
        self.byte_reader = byte_reader

    def read(self) -> dict[str, Any]:
        return read_ci_diagnostics(self.pid, self.byte_reader)


__all__ = [
    "CI_DIAGNOSTICS_HEADER_SIZE",
    "CI_DIAGNOSTICS_MAGIC",
    "CI_DIAGNOSTICS_READY",
    "CI_DIAGNOSTICS_RECORD_COUNT",
    "CI_DIAGNOSTICS_RECORD_SIZE",
    "CI_DIAGNOSTICS_REGION_SIZE",
    "CI_DIAGNOSTICS_VERSION",
    "CiDiagnosticsReader",
    "mapping_name_for_pid",
    "read_ci_diagnostics",
]
