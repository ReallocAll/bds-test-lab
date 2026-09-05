from __future__ import annotations

import unittest
from unittest import mock

from controller import pr49_hot_reload_runner as runner


class _Stdin:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, value: str) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        self.flushes += 1


class _Process:
    def __init__(self) -> None:
        self.stdin = _Stdin()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class Pr49HotReloadRunnerTest(unittest.TestCase):
    def test_native_console_transport_queues_command_then_ordered_ack(self) -> None:
        server = runner.exact._FrameworkShutdownServerProcess.__new__(
            runner.exact._FrameworkShutdownServerProcess
        )
        process = _Process()
        server.process = process  # type: ignore[assignment]
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.snapshot = lambda: ["ready"]  # type: ignore[method-assign]

        with mock.patch.object(runner.uuid, "uuid4", return_value=mock.Mock(hex="abc123")):
            start = runner._native_console_command(server, "execute run function spark_probe_alpha")

        self.assertEqual(start, 1)
        self.assertEqual(
            process.stdin.writes,
            ["execute run function spark_probe_alpha\n", "ciack abc123\n"],
        )
        self.assertEqual(process.stdin.flushes, 1)
        self.assertEqual(server._pending_native_console_commands, {1: "abc123"})

    def test_native_console_transport_waits_for_framework_ack(self) -> None:
        server = runner.exact._FrameworkShutdownServerProcess.__new__(
            runner.exact._FrameworkShutdownServerProcess
        )
        server._pending_native_console_commands = {0: "abc123"}
        server.snapshot = lambda: [  # type: ignore[method-assign]
            "SparkPackAlphaActive",
            "CI command transport acknowledged; token=abc123",
        ]
        server.is_alive = lambda: True  # type: ignore[method-assign]

        output = runner._wait_native_console_command_output(server, 0, 0.2)

        self.assertEqual(len(output), 2)
        self.assertNotIn(0, server._pending_native_console_commands)

    def test_native_console_transport_fails_closed_when_server_exits_before_ack(self) -> None:
        server = runner.exact._FrameworkShutdownServerProcess.__new__(
            runner.exact._FrameworkShutdownServerProcess
        )
        server._pending_native_console_commands = {0: "abc123"}
        server.snapshot = lambda: []  # type: ignore[method-assign]
        server.is_alive = lambda: False  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "before native-console CI command acknowledgement"):
            runner._wait_native_console_command_output(server, 0, 0.2)

    def test_select_live_bds_identity_requires_one_verified_bds_process(self) -> None:
        records = [
            {"pid": 100, "name": "python.exe", "alive": True, "identity_match": True, "create_time": 10.0},
            {
                "pid": 200,
                "name": "bedrock_server.exe",
                "alive": True,
                "identity_match": True,
                "create_time": 20.0,
            },
        ]
        self.assertEqual(runner._select_live_bds_identity(records), (200, 20.0))
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            runner._select_live_bds_identity(records[:1])
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            runner._select_live_bds_identity(records + [dict(records[1], pid=201)])

    def test_reload_output_requires_completion_and_rejects_spark_load_failure(self) -> None:
        runner._validate_reload_output(["Reloading...", "Reload complete."])
        with self.assertRaisesRegex(RuntimeError, "did not report completion"):
            runner._validate_reload_output(["Reloading..."])
        with self.assertRaisesRegex(RuntimeError, "failed to reload"):
            runner._validate_reload_output(
                ["Failed to load c++ plugin from plugins/endstone_spark.dll", "Reload complete."]
            )
        with self.assertRaisesRegex(RuntimeError, "enable failure"):
            runner._validate_reload_output(
                ["An error occurred when enabling Spark v0.5.1", "Reload complete."]
            )

    def test_post_reload_profiler_info_requires_auto_resumed_background_profiler(self) -> None:
        runner._validate_post_reload_profiler_info(
            [
                "Profiler is already running!",
                "It was started automatically when spark enabled and has been running in the background for 1s.",
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "restore its background profiler"):
            runner._validate_post_reload_profiler_info(["The profiler isn't running."])


if __name__ == "__main__":
    unittest.main()
