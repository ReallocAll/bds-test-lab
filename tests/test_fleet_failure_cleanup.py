from __future__ import annotations

import copy
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psutil

from controller import bot_validation, fleet_spark_validation, run_test
from controller.cross_platform_fleet_validation import CrossPlatformFleetBotProcess


class OwnedProcessAPITests(unittest.TestCase):
    def process(self, pid, created, executable="python", modules=()):
        process = mock.Mock()
        process.pid = pid
        process.create_time.return_value = created
        process.exe.return_value = str(executable)
        process.memory_maps.return_value = [SimpleNamespace(path=str(path)) for path in modules]
        process.is_running.return_value = True
        process.children.return_value = []
        process.name.return_value = Path(executable).name
        return process

    def server(self, root):
        server = run_test.ServerProcess(["python"], root, root / "server.log")
        server.pid, server.create_time = 101, 1.0
        server.process = mock.Mock(pid=101)
        server.process.poll.return_value = None
        server._managed_processes = {101: 1.0}
        return server

    def test_identity_accepts_owned_executable_and_host_module(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "bedrock_server.exe"
            binary.touch()
            for hosted in (False, True):
                with self.subTest(hosted=hosted):
                    server = self.server(root)
                    launcher = self.process(101, 1.0, modules=[binary] if hosted else [])
                    child = self.process(202, 2.0, binary)
                    launcher.children.return_value = [] if hosted else [child]
                    processes = {101: launcher, 202: child}
                    with mock.patch.object(run_test.psutil, "Process", side_effect=processes.__getitem__):
                        evidence = server.bds_identity_snapshot(root)
                    self.assertEqual(evidence["status"], "VERIFIED")
                    self.assertEqual(evidence["bds"]["pid"], 101 if hosted else 202)
                    self.assertEqual(evidence["bds"]["source"], "module" if hosted else "executable")
                    self.assertEqual(evidence["launch"], {"pid": 101, "create_time": 1.0})
                    self.assertTrue(evidence["bds"]["owned"])

    def test_identity_rejects_unknown_unowned_ambiguous_and_inaccessible(self):
        for failure in ("missing", "unowned", "ambiguous", "access-denied", "root-reused", "child-reused", "wrong-path"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                binary = root / "bedrock_server"
                binary.touch()
                server = self.server(root)
                launcher = self.process(101, 3.0 if failure == "root-reused" else 1.0)
                child = self.process(202, 2.0, binary)
                launcher.children.return_value = [child]
                processes = {101: launcher, 202: child}
                if failure == "missing":
                    binary.unlink()
                elif failure == "unowned":
                    launcher.children.return_value = []
                elif failure == "ambiguous":
                    second = self.process(303, 3.0, binary)
                    launcher.children.return_value.append(second)
                    processes[303] = second
                elif failure == "access-denied":
                    child.exe.side_effect = psutil.AccessDenied(202)
                elif failure == "child-reused":
                    server._managed_processes[202] = 0.5
                elif failure == "wrong-path":
                    child.exe.return_value = str(root / "other" / "bedrock_server")
                with mock.patch.object(run_test.psutil, "Process", side_effect=processes.__getitem__):
                    evidence = server.bds_identity_snapshot(root)
                self.assertEqual(evidence["status"], "UNVERIFIED")
                self.assertIsNone(evidence["bds"])

    def test_transient_utility_children_do_not_change_bds_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "bedrock_server"
            binary.touch()
            server = self.server(root)
            launcher = self.process(101, 1.0)
            child = self.process(202, 2.0, binary)
            utility = self.process(303, 3.0, "utility")
            launcher.children.return_value = [child, utility]
            processes = {101: launcher, 202: child, 303: utility}
            def lookup(pid):
                if pid not in processes:
                    raise psutil.NoSuchProcess(pid)
                return processes[pid]
            with mock.patch.object(run_test.psutil, "Process", side_effect=lookup):
                first = server.bds_identity_snapshot(root)
                del processes[303]
                launcher.children.return_value = [child]
                second = server.bds_identity_snapshot(root)
            self.assertEqual(first["status"], "VERIFIED")
            self.assertEqual(first, second)

    def test_owned_fallback_never_kills_reused_or_unverified_pids(self):
        records = [
            {"pid": 101, "create_time": 1.0, "identity_match": True, "alive": True},
            {"pid": 202, "create_time": 2.0, "identity_match": True, "alive": True},
            {"pid": 303, "create_time": None, "identity_match": False, "alive": None},
        ]
        owned = self.process(101, 1.0)
        reused = self.process(202, 9.0)
        owned.kill.side_effect = lambda: owned.is_running.configure_mock(return_value=False)
        with (
            mock.patch.object(run_test.psutil, "Process", side_effect={101: owned, 202: reused}.__getitem__),
            mock.patch.object(run_test.psutil, "wait_procs") as wait,
            mock.patch.object(run_test.subprocess, "run") as external,
            mock.patch.object(run_test.os, "killpg", create=True) as group,
        ):
            outcome = run_test.force_owned_processes(records)
        owned.kill.assert_called_once()
        reused.kill.assert_not_called()
        group.assert_not_called()
        external.assert_not_called()
        self.assertLessEqual(wait.call_args.kwargs["timeout"], 5.0)
        self.assertEqual(outcome["killed"], [{"pid": 101, "create_time": 1.0}])
        self.assertIn({"pid": 202, "reason": "identity-changed"}, outcome["skipped"])
        self.assertIn({"pid": 303, "reason": "unverified-identity"}, outcome["skipped"])

    def test_fallback_uses_one_total_deadline_and_no_kill_after_expiry(self):
        clock = [0.0]
        first, second = self.process(101, 1.0), self.process(202, 2.0)
        first.kill.side_effect = lambda: clock.__setitem__(0, 5.0)
        records = [{"pid": pid, "create_time": created, "identity_match": True, "alive": True}
                   for pid, created in ((101, 1.0), (202, 2.0))]
        with (
            mock.patch.object(run_test.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(run_test.psutil, "Process", side_effect={101: first, 202: second}.__getitem__),
            mock.patch.object(run_test.psutil, "wait_procs") as wait,
        ):
            outcome = run_test.force_owned_processes(records, timeout=100)
        first.kill.assert_called_once()
        second.kill.assert_not_called()
        wait.assert_called_once_with([first], timeout=0.0)
        self.assertIn({"pid": 202, "reason": "deadline"}, outcome["skipped"])

    def test_server_fallback_records_ownership_and_not_started(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = run_test.ServerProcess([], root, root / "log")
            self.assertEqual(server.force_kill_owned()["outcome"], "not_started")
            server = self.server(root)
            records = [{"pid": 101, "create_time": 1.0, "identity_match": True, "alive": True}]
            server.process_tree_snapshot = mock.Mock(return_value=records)
            with mock.patch.object(run_test, "force_owned_processes", return_value={"forced": True}) as force:
                server.force_kill_owned()
            self.assertEqual(force.call_args.args[0], records)
            self.assertLessEqual(force.call_args.args[1], 5.0)
            self.assertTrue(server.was_forced)

    def test_fleet_graceful_timeout_has_no_implicit_force(self):
        bot = bot_validation.BotProcess(Path("bot"), Path("bot.log"))
        bot.process = mock.Mock(pid=101)
        bot.process.poll.return_value = None
        process = self.process(101, 1.0)
        with mock.patch.object(bot_validation.psutil, "Process", return_value=process):
            bot.capture_process_identity()
            self.assertEqual(bot.create_time, 1.0)
            bot.process.wait.side_effect = subprocess.TimeoutExpired("bot", 20)
            outcome = bot.graceful_stop(20)
        self.assertFalse(outcome["success"])
        self.assertEqual(outcome["outcome"], "timeout")
        bot.process.kill.assert_not_called()
        bot.process.send_signal.assert_called_once()
        self.assertLessEqual(bot.process.wait.call_args.kwargs["timeout"], 20)

    def test_fleet_graceful_rejects_pid_reuse_without_signal(self):
        bot = bot_validation.BotProcess(Path("bot"), Path("bot.log"))
        self.assertEqual(bot.graceful_stop()["outcome"], "not_started")
        self.assertEqual(bot.force_kill_owned()["outcome"], "not_started")
        bot.pid, bot.create_time = 101, 1.0
        bot.process = mock.Mock(pid=101)
        with mock.patch.object(bot_validation.psutil, "Process", return_value=self.process(101, 2.0)):
            outcome = bot.graceful_stop()
        self.assertFalse(outcome["success"])
        bot.process.send_signal.assert_not_called()

    def test_new_graceful_api_preserves_linux_signal_normalization(self):
        bot = CrossPlatformFleetBotProcess(Path("bot"), Path("bot.log"), 1, "idle")
        bot.events = [
            {"event": "bot_stats", "index": 1, "online": True},
            {"event": "fleet_shutdown", "graceful_shutdown": True, "reason": "signal", "launched": 1, "online": 1},
        ]
        with (
            mock.patch("controller.cross_platform_fleet_validation.sys.platform", "linux"),
            mock.patch.object(bot_validation.BotProcess, "graceful_stop", return_value={"returncode": -15, "success": False}),
        ):
            self.assertTrue(bot.graceful_stop()["success"])


class FleetFailureCleanupTests(unittest.TestCase):
    def validator(self, root):
        validator = object.__new__(fleet_spark_validation.FleetSparkValidation)
        validator.result = {}
        validator.count = 1
        validator.diagnostics = root / "diagnostics.log"
        validator.bot = mock.Mock()
        validator.server = mock.Mock()
        validator.server._process_tree_error = None
        validator.bot.graceful_stop.return_value = {"success": True, "returncode": 0}
        validator.server.graceful_stop.return_value = True
        for resource in (validator.bot, validator.server):
            resource.force_kill_owned.return_value = {"forced": True, "outcome": "completed", "residual": []}
            resource.owned_snapshot.return_value = []
            resource.process_tree_snapshot.return_value = []
        validator.server.snapshot.return_value = []
        validator.install_artifacts = mock.Mock()
        validator.bootstrap_offline_server = mock.Mock()
        validator.run_basic_commands = mock.Mock()
        validator.start_fleet = mock.Mock()
        validator.wait_player_count = mock.Mock(return_value=(["players"], 0))
        validator.profile_execution = mock.Mock(return_value=("viewer", [1]))
        validator.parse_spark_metrics = mock.Mock(return_value={})
        validator.stop_fleet = mock.Mock()
        validator._shutdown_owned_runtime = mock.Mock()
        validator.check = mock.Mock()
        validator.split_logs = mock.Mock()
        validator.persisted = []
        validator._write_results = lambda: validator.persisted.append(copy.deepcopy(validator.result))
        return validator

    def test_failures_preserve_original_error_and_attempt_both_resources(self):
        for failed_method, expected_stage in (
            ("bootstrap_offline_server", "bds-bootstrap"),
            ("profile_execution", "fleet-profile"),
            ("stop_fleet", "fleet-disconnect"),
        ):
            with self.subTest(stage=expected_stage), tempfile.TemporaryDirectory() as temporary:
                validator = self.validator(Path(temporary))
                getattr(validator, failed_method).side_effect = RuntimeError("original failure")
                validator.bot.graceful_stop.side_effect = RuntimeError("fleet graceful failed")
                validator.bot.force_kill_owned.side_effect = RuntimeError("fleet fallback failed")
                validator.server.graceful_stop.return_value = False
                with mock.patch.object(fleet_spark_validation.time, "sleep"), mock.patch("sys.stdout", new=io.StringIO()):
                    self.assertEqual(validator.execute(), 1)
                self.assertEqual(validator.result["failed_stage"], expected_stage)
                self.assertEqual(validator.result["error_summary"], "RuntimeError: original failure")
                validator.bot.graceful_stop.assert_called_once_with(20.0)
                validator.bot.force_kill_owned.assert_called_once_with(5.0)
                validator.server.graceful_stop.assert_called_once_with(30.0)
                validator.server.force_kill_owned.assert_called_once_with(5.0)
                cleanup = validator.result["failure_cleanup"]
                self.assertIn("fallback_error", cleanup["fleet"])
                self.assertTrue(cleanup["server"]["forced"])
                self.assertEqual(validator.persisted[-1]["failure_cleanup"], cleanup)
                self.assertIn("original failure", validator.diagnostics.read_text())
                validator.bot.force_close.assert_not_called()
                validator.server.force_kill_tree.assert_not_called()

    def test_graceful_success_never_forces_and_cleanup_order_is_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.validator(Path(temporary))
            calls = []
            validator.bot.graceful_stop.side_effect = lambda *_: (calls.append("fleet"), {"success": True})[1]
            validator.server.graceful_stop.side_effect = lambda *_: (calls.append("server"), True)[1]
            validator.cleanup_after_failure()
            self.assertEqual(calls, ["fleet", "server"])
            for resource in (validator.bot, validator.server):
                resource.force_kill_owned.assert_not_called()
            for resource in validator.result["failure_cleanup"].values():
                self.assertEqual(resource["outcome"], "graceful")
                self.assertFalse(resource["forced"])

    def test_not_started_and_persistence_failures_do_not_skip_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.validator(Path(temporary))
            validator.bot = None
            validator.cleanup_after_failure()
            self.assertFalse(validator.result["failure_cleanup"]["fleet"]["attempted"])
            self.assertEqual(validator.result["failure_cleanup"]["fleet"]["outcome"], "not_started")
            validator = self.validator(Path(temporary))
            validator._write_results = mock.Mock(side_effect=OSError("disk error"))
            validator.cleanup_after_failure()
            validator.server.graceful_stop.assert_called_once_with(30.0)
            self.assertIn("persistence_error", validator.result["failure_cleanup"]["fleet"])

    def test_graceful_wrapper_exit_with_owned_child_triggers_fallback_and_recheck(self):
        for resource_name in ("fleet", "server"):
            with self.subTest(resource=resource_name), tempfile.TemporaryDirectory() as temporary:
                validator = self.validator(Path(temporary))
                resource = validator.bot if resource_name == "fleet" else validator.server
                snapshot = resource.owned_snapshot if resource_name == "fleet" else resource.process_tree_snapshot
                child = {"pid": 202, "create_time": 2.0, "identity_match": True, "alive": True}
                exited = child | {"alive": False}
                calls = []
                def residuals(calls=calls, child=child, exited=exited):
                    calls.append("snapshot")
                    return [child] if len(calls) == 1 else [exited]
                snapshot.side_effect = residuals
                resource.force_kill_owned.side_effect = lambda *_, calls=calls: (calls.append("fallback"), {"forced": True, "outcome": "completed"})[1]
                validator.cleanup_after_failure()
                self.assertEqual(calls, ["snapshot", "fallback", "snapshot"])
                resource.force_kill_owned.assert_called_once_with(5.0)
                outcome = validator.result["failure_cleanup"][resource_name]
                self.assertEqual(outcome["residual_before_fallback"], [child])
                self.assertEqual(outcome["residual"], [exited])
                self.assertEqual(outcome["outcome"], "forced")
                self.assertEqual(outcome["residual_status"], "clean")
                validator.bot.graceful_stop.assert_called_once_with(20.0)
                validator.server.graceful_stop.assert_called_once_with(30.0)

    def test_unknown_residuals_never_report_graceful_or_force_unverified_pid(self):
        for unknown in (
            [{"pid": 202, "create_time": None, "identity_match": False, "alive": None}],
            [{"pid": 202, "create_time": 2.0, "identity_match": False, "alive": True}],
            OSError("snapshot failed"),
        ):
            with self.subTest(unknown=unknown), tempfile.TemporaryDirectory() as temporary:
                validator = self.validator(Path(temporary))
                if isinstance(unknown, Exception):
                    validator.bot.owned_snapshot.side_effect = unknown
                else:
                    validator.bot.owned_snapshot.return_value = unknown
                validator.cleanup_after_failure()
                validator.bot.force_kill_owned.assert_not_called()
                self.assertEqual(validator.result["failure_cleanup"]["fleet"]["outcome"], "UNVERIFIED")
                validator.server.graceful_stop.assert_called_once_with(30.0)
                self.assertEqual(validator.result["failure_cleanup"]["server"]["outcome"], "graceful")

    def test_snapshot_tree_error_is_unverified_even_with_no_visible_residuals(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.validator(Path(temporary))
            validator.server._process_tree_error = "AccessDenied"
            validator.cleanup_after_failure()
            outcome = validator.result["failure_cleanup"]["server"]
            self.assertEqual(outcome["outcome"], "UNVERIFIED")
            self.assertEqual(outcome["residual_error"], "AccessDenied")
            validator.server.force_kill_owned.assert_not_called()

    def test_fallback_surviving_or_unknown_residuals_remain_explicit_failures(self):
        for final in (
            [{"pid": 202, "create_time": 2.0, "identity_match": True, "alive": True}],
            [{"pid": 202, "create_time": None, "identity_match": False, "alive": None}],
            OSError("final snapshot failed"),
        ):
            with self.subTest(final=final), tempfile.TemporaryDirectory() as temporary:
                validator = self.validator(Path(temporary))
                before = [{"pid": 202, "create_time": 2.0, "identity_match": True, "alive": True}]
                validator.server.process_tree_snapshot.side_effect = [before, final]
                validator.cleanup_after_failure()
                outcome = validator.result["failure_cleanup"]["server"]
                expected = "residual_processes" if final == before else "UNVERIFIED"
                self.assertEqual(outcome["outcome"], expected)
                self.assertEqual(validator.result["shutdown_status"], expected)
                self.assertTrue(outcome["forced"])
                self.assertEqual(validator.persisted[-1]["failure_cleanup"]["server"], outcome)

    def test_known_live_child_still_gets_safe_fallback_alongside_unknown_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.validator(Path(temporary))
            known = {"pid": 202, "create_time": 2.0, "identity_match": True, "alive": True}
            unknown = {"pid": 303, "create_time": None, "identity_match": False, "alive": None}
            validator.server.process_tree_snapshot.return_value = [known, unknown]
            validator.cleanup_after_failure()
            validator.server.force_kill_owned.assert_called_once_with(5.0)
            self.assertEqual(validator.result["failure_cleanup"]["server"]["outcome"], "UNVERIFIED")

    def test_normal_success_uses_no_failure_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.validator(Path(temporary))
            with mock.patch.object(fleet_spark_validation.time, "sleep"), mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(validator.execute(), 0)
            self.assertNotIn("failure_cleanup", validator.result)
            validator.bot.force_kill_owned.assert_not_called()
            validator.server.force_kill_owned.assert_not_called()
            validator.bot.force_close.assert_not_called()

    def test_actual_shutdown_success_keeps_residual_check_and_never_forces(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.validator(Path(temporary))
            validator.record_server_lifecycle = mock.Mock()
            validator.residual_processes = mock.Mock(return_value=[])
            fleet_spark_validation.FleetSparkValidation._shutdown_owned_runtime(validator)
            validator.server.close.assert_called_once()
            validator.residual_processes.assert_called_once()
            validator.check.assert_called_once_with("shutdown", "PASS", "graceful; no residual BDS process")
            validator.server.force_kill_tree.assert_not_called()
            validator.server.force_kill_owned.assert_not_called()
            validator.server.graceful_stop.return_value = False
            with self.assertRaisesRegex(RuntimeError, "did not shut down gracefully"):
                fleet_spark_validation.FleetSparkValidation._shutdown_owned_runtime(validator)
            validator.server.force_kill_tree.assert_not_called()

    def test_actual_bootstrap_failure_defers_force_to_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.validator(Path(temporary))
            validator.start_server = mock.Mock()
            validator.wait_post_start_initialization = mock.Mock()
            validator.server.graceful_stop.return_value = False
            with self.assertRaisesRegex(RuntimeError, "server.properties bootstrap"):
                fleet_spark_validation.FleetSparkValidation.bootstrap_offline_server(validator)
            validator.server.force_kill_tree.assert_not_called()
            validator.server.force_kill_owned.assert_not_called()

    def test_diagnostic_io_failure_does_not_replace_original_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.validator(Path(temporary))
            validator.profile_execution.side_effect = RuntimeError("original")
            validator.diagnostics = mock.Mock()
            validator.diagnostics.write_text.side_effect = OSError("disk error")
            validator.split_logs.side_effect = OSError("split error")
            with mock.patch.object(fleet_spark_validation.time, "sleep"), mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(validator.execute(), 1)
            self.assertEqual(validator.result["error_summary"], "RuntimeError: original")
            self.assertIn("OSError", validator.result["diagnostic_error"])
            self.assertTrue(validator.result["finalization_errors"])


if __name__ == "__main__":
    unittest.main()
