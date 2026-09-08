from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from controller import ci_diagnostics, run_test
from controller.run_test import ServerProcess


def _region() -> bytearray:
    data = bytearray(ci_diagnostics.CI_DIAGNOSTICS_REGION_SIZE)
    struct.pack_into(
        "<5Q",
        data,
        0,
        ci_diagnostics.CI_DIAGNOSTICS_MAGIC,
        ci_diagnostics.CI_DIAGNOSTICS_VERSION,
        ci_diagnostics.CI_DIAGNOSTICS_REGION_SIZE,
        17,
        ci_diagnostics.CI_DIAGNOSTICS_READY,
    )
    return data


class CiDiagnosticsReaderTest(unittest.TestCase):
    def test_valid_snapshot_decodes_all_contexts_and_preserves_values(self) -> None:
        data = _region()
        offset = ci_diagnostics.CI_DIAGNOSTICS_HEADER_SIZE
        struct.pack_into("<8Q", data, offset, 2, 9, 999 | (23 << 16), 101, 102, 103, 104, 105)

        result = ci_diagnostics.read_ci_diagnostics(4242, data)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["mapping_name"], "Local\\EndstoneSparkCiDiag-v2-4242")
        self.assertEqual(result["mapping_lifetime"], 17)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(len(result["contexts"]), ci_diagnostics.CI_DIAGNOSTICS_RECORD_COUNT)
        record = result["contexts"][0]
        self.assertEqual(record["context"], "ApplicationTick")
        self.assertEqual(record["sequence"], 2)
        self.assertEqual(record["session_generation"], 9)
        self.assertEqual(record["phase"], "unknown")
        self.assertEqual(record["phase_value"], 999)
        self.assertEqual(record["transition_sequence"], 23)
        self.assertEqual(record["worker_tid"], 101)
        self.assertEqual(record["target_tid"], 102)
        self.assertEqual(record["suspend_success_count"], 103)
        self.assertEqual(record["resume_success_count"], 104)
        self.assertEqual(record["walk_call_count"], 105)

    def test_missing_and_invalid_headers_are_unavailable(self) -> None:
        missing = ci_diagnostics.read_ci_diagnostics(4242, lambda _offset, _size: (_ for _ in ()).throw(FileNotFoundError()))
        self.assertEqual(missing["status"], "unavailable")
        self.assertEqual(ci_diagnostics.read_ci_diagnostics(4242, _region()[:-1])["status"], "unavailable")
        for field, value in ((0, 1), (8, 1), (16, 1), (32, 0)):
            data = _region()
            struct.pack_into("<Q", data, field, value)
            result = ci_diagnostics.read_ci_diagnostics(4242, data)
            self.assertEqual(result["status"], "unavailable")

    def test_odd_or_changed_record_is_unknown_without_retry(self) -> None:
        data = _region()
        offset = ci_diagnostics.CI_DIAGNOSTICS_HEADER_SIZE
        struct.pack_into("<Q", data, offset, 3)
        result = ci_diagnostics.read_ci_diagnostics(4242, data)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["contexts"][0], {"context": "ApplicationTick", "context_value": 0, "status": "unknown"})

        data = _region()
        calls: list[tuple[int, int]] = []

        def changed_reader(read_offset: int, size: int) -> bytes:
            calls.append((read_offset, size))
            value = bytes(data[read_offset : read_offset + size])
            if read_offset == offset and size == 8 and len([item for item in calls if item == (offset, 8)]) == 2:
                return struct.pack("<Q", 4)
            return value

        result = ci_diagnostics.read_ci_diagnostics(4242, changed_reader)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["contexts"][0]["status"], "unknown")
        self.assertEqual(len([item for item in calls if item == (offset, 8)]), 2)

    def test_mapping_lifetime_and_ready_changes_invalidate_snapshot(self) -> None:
        for field, changed in ((24, 18), (32, 0)):
            data = _region()
            header_reads = 0

            def reader(offset: int, size: int, *, data: bytearray = data, field: int = field, changed: int = changed) -> bytes:
                nonlocal header_reads
                value = bytes(data[offset : offset + size])
                if offset == 0:
                    header_reads += 1
                    if header_reads == 2:
                        changed_header = bytearray(value)
                        struct.pack_into("<Q", changed_header, field, changed)
                        return bytes(changed_header)
                return value

            result = ci_diagnostics.read_ci_diagnostics(4242, reader)
            self.assertEqual(result["status"], "unavailable")

    def test_snapshot_is_bounded_and_contains_no_private_process_fields(self) -> None:
        result = ci_diagnostics.read_ci_diagnostics(4242, _region())
        text = json.dumps(result, separators=(",", ":"), sort_keys=True)
        self.assertLess(len(text.encode("utf-8")), 32 * 1024)
        for forbidden in ("address", "commandline", "cwd", "memory", "dump", "private"):
            self.assertNotIn(forbidden, text.casefold())


class CiDiagnosticsEnvironmentTest(unittest.TestCase):
    def test_server_process_only_enables_diagnostics_for_opted_in_child(self) -> None:
        class FakeProcess:
            pid = 4242

            class Stream:
                def close(self) -> None:
                    pass

            stdin = Stream()
            stdout = Stream()

        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"ENDSTONE_SPARK_CI_DIAGNOSTICS": "parent-value"}, clear=False
        ), mock.patch.object(run_test.subprocess, "Popen", return_value=FakeProcess()) as popen, mock.patch.object(
            run_test.psutil, "Process", return_value=SimpleNamespace(create_time=lambda: 1.0)
        ), mock.patch.object(run_test.threading, "Thread") as thread:
            thread.return_value.start = mock.Mock()
            enabled = ServerProcess(["bds"], Path(temp), Path(temp) / "enabled.log")
            enabled.ci_diagnostics_enabled = True
            enabled.start()
            disabled = ServerProcess(["bds"], Path(temp), Path(temp) / "disabled.log")
            disabled.start()
            enabled.close()
            disabled.close()

        self.assertEqual(popen.call_args_list[0].kwargs["env"]["ENDSTONE_SPARK_CI_DIAGNOSTICS"], "1")
        self.assertNotIn("ENDSTONE_SPARK_CI_DIAGNOSTICS", popen.call_args_list[1].kwargs["env"])


if __name__ == "__main__":
    unittest.main()
