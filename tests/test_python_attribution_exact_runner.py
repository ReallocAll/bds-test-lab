from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import controller.python_attribution_exact_runner as exact
from controller import run_test
from controller.python_attribution_validation import PythonAttributionValidation
from controller.run_test import IntegrationTest, ServerProcess


class _WaitProcess:
    def __init__(self, returncode: int = 0, *, timeout: bool = False) -> None:
        self.pid = 4242
        self.returncode = returncode
        self.timeout = timeout
        self.alive = True
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return None if self.alive else self.returncode

    def wait(self, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        if self.timeout and self.alive:
            raise subprocess.TimeoutExpired("test", timeout)
        self.alive = False
        return self.returncode

    def kill(self) -> None:
        self.alive = False
        self.returncode = -9


class _StartedServer:
    created_cmd: list[str] | None = None

    def __init__(self, cmd: list[str], _root: Path, _log_path: Path) -> None:
        type(self).created_cmd = list(cmd)
        self.lifecycle_registered = False

    def start(self) -> None:
        pass

    def wait_for(self, predicate, _timeout: float, description: str) -> list[str]:
        lines = [
            "Server started.",
            "[Endstone] Enabling spark v0.6.0",
            "CI lifecycle control enabled; cishutdown registered",
        ]
        if not predicate(lines):
            raise AssertionError(f"test fixture did not satisfy {description}")
        return lines

    def snapshot(self) -> list[str]:
        return ["[Endstone] Version: 1.26.44.3"]


class FrameworkShutdownTest(unittest.TestCase):
    def _server(self, process: _WaitProcess) -> exact._FrameworkShutdownServerProcess:
        server = exact._FrameworkShutdownServerProcess.__new__(exact._FrameworkShutdownServerProcess)
        server.process = process  # type: ignore[assignment]
        server.pid = process.pid
        server.create_time = None
        server.lifecycle_diagnostic = {}
        server._forced = False
        server._managed_processes = {}
        server._root_identity_status = "verified"
        server._root_identity_evidence = {"status": "verified"}
        server.process_tree_snapshot = mock.Mock(return_value=[])  # type: ignore[method-assign]
        server.lifecycle_registered = True
        return server

    def test_cishutdown_records_lifecycle_evidence(self) -> None:
        server = self._server(_WaitProcess())
        commands: list[str] = []
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = commands.append  # type: ignore[method-assign]
        server.snapshot = lambda: ["CI lifecycle shutdown requested"]  # type: ignore[method-assign]

        self.assertTrue(server.graceful_stop(7.5))

        self.assertEqual(commands, ["cishutdown"])
        diagnostic = server.lifecycle_diagnostic
        self.assertEqual(diagnostic["wrapper_pid"], 4242)
        self.assertEqual(diagnostic["method"], "interactive-cishutdown")
        self.assertEqual(diagnostic["stop_method"], "interactive-cishutdown")
        self.assertEqual(diagnostic["command"], "cishutdown")
        self.assertEqual(diagnostic["returncode"], 0)
        self.assertEqual(diagnostic["return_code"], 0)
        self.assertEqual(diagnostic["outcome"], "exited")
        self.assertEqual(diagnostic["cleanup_outcome"], "graceful-exit")
        evidence = diagnostic["acknowledgement_evidence"]
        self.assertTrue(evidence["command_sent"])
        self.assertTrue(evidence["registration_observed"])
        self.assertTrue(evidence["shutdown_requested"])
        self.assertTrue(evidence["observed"])
        self.assertIn("process_tree_before", diagnostic)
        self.assertIn("process_tree_after", diagnostic)

    def test_timeout_force_kill_never_becomes_graceful(self) -> None:
        server = self._server(_WaitProcess(timeout=True))
        server.is_alive = lambda: server.process.poll() is None  # type: ignore[method-assign]
        server.command = lambda _command: 0  # type: ignore[method-assign]

        with mock.patch.object(run_test.os, "name", "nt"), mock.patch.object(run_test.subprocess, "run"):
            self.assertFalse(server.graceful_stop(0.1))
            server.force_kill_tree()
            self.assertFalse(server.graceful_stop(0.1))

        self.assertEqual(server.lifecycle_diagnostic["outcome"], "forced")
        self.assertEqual(server.lifecycle_diagnostic["cleanup_outcome"], "forced")

    def test_already_exited_server_is_not_graceful(self) -> None:
        process = _WaitProcess()
        process.alive = False
        server = self._server(process)
        server._root_identity_status = "absent"
        server._root_identity_evidence = {"status": "absent"}
        server.is_alive = lambda: False  # type: ignore[method-assign]
        server.command = mock.Mock(return_value=0)  # type: ignore[method-assign]

        self.assertFalse(server.graceful_stop(1.0))

        diagnostic = server.lifecycle_diagnostic
        self.assertEqual(diagnostic["outcome"], "uncontrolled-exit")
        self.assertEqual(diagnostic["cleanup_outcome"], "clean")
        self.assertFalse(diagnostic["acknowledgement_evidence"]["command_sent"])
        self.assertFalse(diagnostic["acknowledgement_evidence"]["observed"])
        server.command.assert_not_called()

    def test_zero_exit_without_shutdown_ack_is_not_observed(self) -> None:
        server = self._server(_WaitProcess())
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = mock.Mock(return_value=0)  # type: ignore[method-assign]
        server.snapshot = list  # type: ignore[method-assign]

        self.assertFalse(server.graceful_stop(1.0))

        evidence = server.lifecycle_diagnostic["acknowledgement_evidence"]
        self.assertFalse(evidence["shutdown_requested"])
        self.assertFalse(evidence["observed"])
        self.assertEqual(server.lifecycle_diagnostic["returncode"], 0)

    def test_stale_shutdown_ack_before_command_is_not_observed(self) -> None:
        server = self._server(_WaitProcess())
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = lambda _command: 1  # type: ignore[method-assign]
        server.snapshot = lambda: ["CI lifecycle shutdown requested"]  # type: ignore[method-assign]

        self.assertFalse(server.graceful_stop(1.0))

        evidence = server.lifecycle_diagnostic["acknowledgement_evidence"]
        self.assertFalse(evidence["shutdown_requested"])
        self.assertFalse(evidence["observed"])


class PythonAttributionExactRunnerTest(unittest.TestCase):
    @staticmethod
    def _bootstrap_fixture(root: Path, platform: str) -> tuple[PythonAttributionValidation, list[tuple[bool, str | None]]]:
        validator = PythonAttributionValidation.__new__(PythonAttributionValidation)
        validator.platform = platform
        validator.server_dir = root / "bedrock_server"
        validator.server_dir.mkdir()
        (validator.server_dir / "server.properties").write_text("difficulty=easy\n", encoding="utf-8")
        validator.server = None
        validator.wait_plugin = mock.Mock()
        validator.command_check = mock.Mock()
        validator.record_server_lifecycle = mock.Mock()
        launch_states: list[tuple[bool, str | None]] = []
        servers = [mock.Mock(), mock.Mock()]
        for server in servers:
            server.graceful_stop.return_value = True

        def start_server() -> None:
            launch_states.append(
                ("SPARK_PYTHON_HOTSPOT_MODE" in os.environ, os.environ.get("SPARK_PYTHON_HOTSPOT_MODE"))
            )
            validator.server = servers.pop(0)

        validator.start_server = mock.Mock(side_effect=start_server)
        return validator, launch_states

    def test_windows_bootstrap_restores_hotspot_mode_before_real_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"SPARK_PYTHON_HOTSPOT_MODE": "dual"}, clear=False
        ):
            validator, launch_states = self._bootstrap_fixture(Path(temp), "windows")
            validator.bootstrap_server()

            self.assertEqual(launch_states, [(True, "off"), (True, "dual")])
            self.assertEqual(os.environ["SPARK_PYTHON_HOTSPOT_MODE"], "dual")

    def test_windows_bootstrap_restores_absent_hotspot_mode_before_real_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPARK_PYTHON_HOTSPOT_MODE", None)
            validator, launch_states = self._bootstrap_fixture(Path(temp), "windows")
            validator.bootstrap_server()

            self.assertEqual(launch_states, [(True, "off"), (False, None)])
            self.assertNotIn("SPARK_PYTHON_HOTSPOT_MODE", os.environ)

    def test_windows_bootstrap_restores_hotspot_mode_when_initial_start_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"SPARK_PYTHON_HOTSPOT_MODE": "mixed"}, clear=False
        ):
            validator, launch_states = self._bootstrap_fixture(Path(temp), "windows")

            def fail_start() -> None:
                launch_states.append(
                    ("SPARK_PYTHON_HOTSPOT_MODE" in os.environ, os.environ.get("SPARK_PYTHON_HOTSPOT_MODE"))
                )
                raise RuntimeError("bootstrap failed")

            validator.start_server.side_effect = fail_start

            with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                validator.bootstrap_server()

            self.assertEqual(launch_states, [(True, "off")])
            self.assertEqual(os.environ["SPARK_PYTHON_HOTSPOT_MODE"], "mixed")

    def test_linux_bootstrap_keeps_requested_hotspot_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"SPARK_PYTHON_HOTSPOT_MODE": "worker"}, clear=False
        ):
            validator, launch_states = self._bootstrap_fixture(Path(temp), "linux")
            validator.bootstrap_server()

            self.assertEqual(launch_states, [(True, "worker"), (True, "worker")])
            self.assertEqual(os.environ["SPARK_PYTHON_HOTSPOT_MODE"], "worker")

    def test_windows_start_uses_interactive_command_map_and_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            validator = PythonAttributionValidation.__new__(PythonAttributionValidation)
            validator.platform = "windows"
            validator.root = root
            validator.server_dir = root / "bedrock_server"
            validator.server_dir.mkdir()
            validator.log_path = root / "bds.log"
            validator.result_path = root / "test-results.json"
            validator.evidence_path = root / "python-attribution-result.json"
            validator.result = {"checks": []}
            validator.disable_bstats = False
            _StartedServer.created_cmd = None
            with mock.patch.object(exact, "_FrameworkShutdownServerProcess", _StartedServer):
                exact._start_windows_interactive_server(validator)

        self.assertIsNotNone(_StartedServer.created_cmd)
        assert _StartedServer.created_cmd is not None
        self.assertNotIn("--no-interactive", _StartedServer.created_cmd)
        self.assertIn("--server-folder", _StartedServer.created_cmd)
        self.assertTrue(validator.server.lifecycle_registered)
        self.assertEqual(
            [(item["name"], item["status"]) for item in validator.result["checks"]],
            [
                ("bds-start", "PASS"),
                ("ready", "PASS"),
                ("spark-load-enable", "PASS"),
                ("windows-interactive-lifecycle", "PASS"),
            ],
        )

    def test_linux_start_server_delegates_to_existing_behavior(self) -> None:
        validator = mock.Mock()
        validator.platform = "linux"
        validator.result = {}
        server = mock.Mock()
        server.snapshot.return_value = ["Version: 1.26.44.3"]
        validator.server = server
        with (
            mock.patch.object(exact, "_ORIGINAL_START_SERVER") as original,
            mock.patch.object(exact, "validate_bds_version", return_value="26.44"),
        ):
            original.side_effect = lambda _self: None
            exact._start_exact_server(validator)  # type: ignore[arg-type]
        original.assert_called_once_with(validator)

    def test_windows_start_server_routes_to_interactive_behavior(self) -> None:
        validator = mock.Mock()
        validator.platform = "windows"
        validator.result = {}
        validator.server = mock.Mock()
        validator.server.snapshot.return_value = ["Version: 1.26.44.3"]
        with (
            mock.patch.object(exact, "_start_windows_interactive_server") as interactive,
            mock.patch.object(exact, "validate_bds_version", return_value="26.44"),
        ):
            exact._start_exact_server(validator)  # type: ignore[arg-type]
        interactive.assert_called_once_with(validator)

    def test_python_attribution_workflow_installs_lifecycle_fixture_on_windows(self) -> None:
        workflow = Path(__file__).parents[1] / ".github" / "workflows" / "python-attribution-bds-e2e.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("if: matrix.platform == 'windows'", text)
        self.assertIn("./fixtures/endstone-ci-lifecycle-control", text)


class ManagedProcessCleanupTest(unittest.TestCase):
    @staticmethod
    def _server_with_psutil_root(*, expected: float | None, observed: float) -> tuple[ServerProcess, mock.Mock, mock.Mock]:
        server = ServerProcess.__new__(ServerProcess)
        process = _WaitProcess(returncode=1)
        process.alive = False
        server.process = process  # type: ignore[assignment]
        server.pid = process.pid
        server.create_time = expected
        server.lifecycle_diagnostic = {"outcome": "timeout"}
        server._forced = False
        server._managed_processes = {process.pid: expected}
        root = mock.Mock()
        root.create_time.return_value = observed
        root.children.return_value = [mock.Mock()]
        return server, root, process

    def test_reused_wrapper_pid_does_not_adopt_or_kill_unrelated_children(self) -> None:
        server, root, _process = self._server_with_psutil_root(expected=10.0, observed=11.0)
        with (
            mock.patch.object(run_test.psutil, "Process", return_value=root),
            mock.patch.object(run_test.subprocess, "run") as run,
            mock.patch.object(run_test.os, "name", "nt"),
        ):
            records = server.process_tree_snapshot()
            server.force_kill_tree()

        root.children.assert_not_called()
        run.assert_not_called()
        self.assertEqual(server._managed_processes, {4242: 10.0})
        self.assertEqual(records[0]["error"], "wrapper-identity-mismatch")

    def test_unknown_wrapper_creation_identity_does_not_adopt_or_kill_children(self) -> None:
        server, root, _process = self._server_with_psutil_root(expected=None, observed=11.0)
        with (
            mock.patch.object(run_test.psutil, "Process", return_value=root),
            mock.patch.object(run_test.subprocess, "run") as run,
            mock.patch.object(run_test.os, "name", "nt"),
        ):
            records = server.process_tree_snapshot()
            server.force_kill_tree()

        root.children.assert_not_called()
        run.assert_not_called()
        self.assertEqual(server._managed_processes, {4242: None})
        self.assertEqual(records[0]["error"], "wrapper-identity-unknown")

    def test_unknown_post_kill_liveness_is_not_clean(self) -> None:
        server = ServerProcess.__new__(ServerProcess)
        process = _WaitProcess(returncode=1)
        process.alive = False
        server.process = process  # type: ignore[assignment]
        server.pid = process.pid
        server.create_time = 10.0
        server.lifecycle_diagnostic = {"outcome": "timeout"}
        server._forced = False
        server._managed_processes = {4242: 10.0}
        before = [{"pid": 4242, "is_wrapper": True, "identity_match": True, "alive": False}]
        after = [{"pid": 4242, "is_wrapper": True, "identity_match": True, "alive": None}]
        server.process_tree_snapshot = mock.Mock(side_effect=[before, after])  # type: ignore[method-assign]

        with mock.patch.object(run_test.os, "name", "nt"), mock.patch.object(run_test.subprocess, "run"):
            server.force_kill_tree()

        self.assertEqual(server.lifecycle_diagnostic["cleanup_outcome"], "verification-failed")

    def test_reused_managed_descendant_pid_is_not_clean(self) -> None:
        server = ServerProcess.__new__(ServerProcess)
        process = _WaitProcess(returncode=1)
        process.alive = False
        server.process = process  # type: ignore[assignment]
        server.pid = process.pid
        server.create_time = 10.0
        server._forced = False
        server._managed_processes = {4242: 10.0, 4343: 11.0}
        server._unverified_processes = {}
        server._process_tree_error = None
        server._root_identity_status = "verified"
        server.process_tree_snapshot = mock.Mock(
            return_value=[
                {"pid": 4242, "is_wrapper": True, "identity_match": True, "alive": False},
                {
                    "pid": 4343,
                    "is_wrapper": False,
                    "identity_match": False,
                    "alive": False,
                    "error": "identity-mismatch",
                },
            ]
        )  # type: ignore[method-assign]

        self.assertEqual(server._process_tree_cleanup_outcome(server.process_tree_snapshot()), "verification-failed")

    def test_windows_residual_check_uses_only_managed_process_tree(self) -> None:
        fixture = IntegrationTest.__new__(IntegrationTest)
        fixture.platform = "windows"
        fixture.server = mock.Mock()
        fixture.server.managed_residual_processes.return_value = ["bedrock_server.exe (pid=22)"]
        with mock.patch.object(run_test.subprocess, "run") as run:
            self.assertEqual(fixture.residual_processes(), ["bedrock_server.exe (pid=22)"])
        run.assert_not_called()

    def test_linux_residual_check_keeps_server_folder_filter(self) -> None:
        fixture = IntegrationTest.__new__(IntegrationTest)
        fixture.platform = "linux"
        fixture.server_dir = Path.cwd() / "owned-server"
        completed = subprocess.CompletedProcess(
            ["pgrep"],
            0,
            stdout=f"100 python {fixture.server_dir}\n200 python {Path.cwd() / 'other-server'}\n",
        )
        with mock.patch.object(run_test.subprocess, "run", return_value=completed) as run:
            self.assertEqual(fixture.residual_processes(), [f"100 python {fixture.server_dir}"])
        run.assert_called_once_with(
            ["pgrep", "-af", "bedrock_server"],
            stdout=run_test.subprocess.PIPE,
            stderr=run_test.subprocess.DEVNULL,
            text=True,
            check=False,
        )

    def test_windows_force_cleanup_targets_only_captured_descendants(self) -> None:
        server = ServerProcess.__new__(ServerProcess)
        process = _WaitProcess(returncode=1)
        process.alive = False
        server.process = process  # type: ignore[assignment]
        server.pid = process.pid
        server.create_time = 10.0
        server.lifecycle_diagnostic = {"outcome": "timeout"}
        server._forced = False
        server._managed_processes = {4242: 10.0, 4343: 11.0}
        before = [
            {"pid": 4242, "is_wrapper": True, "identity_match": True, "alive": False},
            {"pid": 4343, "is_wrapper": False, "identity_match": True, "alive": True},
        ]
        after = [
            {"pid": 4242, "is_wrapper": True, "identity_match": True, "alive": False},
            {"pid": 4343, "is_wrapper": False, "identity_match": True, "alive": False},
        ]
        server.process_tree_snapshot = mock.Mock(side_effect=[before, after])  # type: ignore[method-assign]

        with mock.patch.object(run_test.os, "name", "nt"), mock.patch.object(run_test.subprocess, "run") as run:
            server.force_kill_tree()

        run.assert_called_once_with(
            ["taskkill", "/PID", "4343", "/T", "/F"],
            stdout=run_test.subprocess.PIPE,
            stderr=run_test.subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(server.lifecycle_diagnostic["cleanup_outcome"], "clean")

    def test_linux_graceful_stop_keeps_native_stop_command(self) -> None:
        server = ServerProcess.__new__(ServerProcess)
        process = _WaitProcess()
        server.process = process  # type: ignore[assignment]
        server.pid = process.pid
        server.create_time = None
        server.lifecycle_diagnostic = {}
        server._forced = False
        server._managed_processes = {}
        commands: list[str] = []
        server.command = commands.append  # type: ignore[method-assign]
        server.process_tree_snapshot = mock.Mock(return_value=[])  # type: ignore[method-assign]
        with mock.patch.object(run_test.os, "name", "posix"):
            self.assertTrue(server.graceful_stop(3.0))
        self.assertEqual(commands, ["stop"])
        self.assertEqual(server.lifecycle_diagnostic["method"], "native-stop")

    def test_shutdown_status_stays_forced_after_late_dead_observation(self) -> None:
        class ForcedServer:
            def __init__(self) -> None:
                self.lifecycle_diagnostic: dict[str, object] = {}
                self.forced = False

            def graceful_stop(self, _timeout: float) -> bool:
                return False

            def force_kill_tree(self) -> None:
                self.forced = True
                self.lifecycle_diagnostic = {"outcome": "forced", "cleanup_outcome": "clean"}

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as temp:
            fixture = IntegrationTest.__new__(IntegrationTest)
            fixture.server = ForcedServer()
            fixture.result = {"checks": [], "shutdown_status": "not_started"}
            fixture.result_path = Path(temp) / "test-results.json"
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "did not shut down gracefully"):
                    fixture.shutdown()
                self.assertEqual(fixture.result["shutdown_status"], "forced")


if __name__ == "__main__":
    unittest.main()
