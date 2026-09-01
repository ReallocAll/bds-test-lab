from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import controller.combined_pack_gamerule_fleet_exact_runner as exact
import controller.combined_pack_gamerule_fleet_validation as validation
from controller.combined_pack_gamerule_fleet_validation import (
    BEHAVIOR_PACKS,
    CombinedPackGameruleFleetValidation,
)
from controller.run_test import IntegrationTest
from controller.windows_evidence_matrix import COMBINED_MATRIX, resolve_matrix


class _Server:
    def snapshot(self) -> list[str]:
        return [
            "[INFO] Starting Server",
            "[INFO] Version: 1.26.44.3",
        ]


class _Validator:
    def __init__(self) -> None:
        self.server = _Server()
        self.result = {"bds_version": "26.44"}
        self.metadata = {
            "components": {
                "spark": {
                    "sha": "a" * 40,
                    "run_id": 11,
                    "artifact": {"id": 12},
                },
                "endstone": {
                    "sha": "b" * 40,
                    "run_id": 21,
                    "artifact": {"id": 22},
                },
            }
        }
        self.checks: list[tuple[str, str, dict[str, object]]] = []

    def check(self, name: str, status: str, *args: object, **kwargs: object) -> None:
        self.checks.append((name, status, dict(kwargs)))


class _StartedServer:
    created_cmd: list[str] | None = None

    def __init__(self, cmd: list[str], root: Path, log_path: Path) -> None:
        del root, log_path
        type(self).created_cmd = list(cmd)

    def start(self) -> None:
        pass

    def wait_for(self, predicate, timeout: float, description: str) -> list[str]:
        del timeout, description
        lines = [
            "Server started.",
            "[EndstoneServer] Version: 1.26.44.3",
            "[Endstone] Enabling spark v0.6.0",
            "[CiLifecycleControl] CI lifecycle control enabled; cishutdown registered",
        ]
        if not predicate(lines):
            raise AssertionError("test fixture did not satisfy wait predicate")
        return lines

    def snapshot(self) -> list[str]:
        return ["[EndstoneServer] Version: 1.26.44.3"]


class _WaitProcess:
    def __init__(self, returncode: int = 0, *, timeout: bool = False) -> None:
        self.pid = 4242
        self.returncode = returncode
        self.timeout = timeout
        self.wait_timeouts: list[float] = []

    def wait(self, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        if self.timeout:
            raise subprocess.TimeoutExpired("cishutdown", timeout)
        return self.returncode


def _framework_server(process: _WaitProcess) -> exact._FrameworkShutdownServerProcess:
    server = exact._FrameworkShutdownServerProcess.__new__(exact._FrameworkShutdownServerProcess)
    server.process = process  # type: ignore[assignment]
    server.pid = process.pid
    server.create_time = None
    server.lifecycle_diagnostic = {}
    server._managed_processes = {}
    server._root_identity_status = "verified"
    server._root_identity_evidence = {"status": "verified"}
    server.process_tree_snapshot = mock.Mock(return_value=[])  # type: ignore[method-assign]
    server.lifecycle_registered = True
    return server


class CombinedPackExactRunnerTest(unittest.TestCase):
    def test_combined_dispatch_selector_keeps_default_matrix_and_targets_windows(self) -> None:
        workflow = Path(__file__).parents[1] / ".github" / "workflows" / "combined-pack-gamerule-20p.yml"
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        dispatch = data[True]["workflow_dispatch"]["inputs"]["target"]
        jobs = data["jobs"]
        combined = jobs["combined-e2e"]

        self.assertEqual(dispatch["type"], "choice")
        self.assertEqual(dispatch["options"], ["all", "windows"])
        self.assertNotIn("if", combined)
        self.assertEqual(combined["strategy"]["matrix"], "${{ fromJSON(needs.resolve-target.outputs.matrix) }}")
        self.assertEqual(
            resolve_matrix("combined", "push", None),
            {"include": list(COMBINED_MATRIX)},
        )
        self.assertEqual(
            resolve_matrix("combined", "workflow_dispatch", "windows"),
            {"include": [COMBINED_MATRIX[1]]},
        )
        resolver_run = jobs["resolve-target"]["steps"][1]["run"]
        self.assertIn("controller.windows_evidence_matrix", resolver_run)
        for invalid in ("linux", "bogus"):
            with self.subTest(target=invalid), self.assertRaisesRegex(ValueError, "target"):
                resolve_matrix("combined", "workflow_dispatch", invalid)
        with self.assertRaisesRegex(ValueError, "complete matrix"):
            resolve_matrix("combined", "push", "windows")

    def test_behavior_pack_functions_use_execute_wrapper_and_distinct_markers(self) -> None:
        validator = mock.Mock()
        outputs = {
            f"execute run function {pack['function']}": [pack["marker"]]
            for pack in BEHAVIOR_PACKS
        }
        validator.command_check.side_effect = lambda _name, command: outputs[command]

        exact.CombinedPackGameruleFleetValidation.verify_behavior_pack_functions(validator)

        self.assertEqual(
            [item.args[1] for item in validator.command_check.call_args_list],
            list(outputs),
        )
        validator.check.assert_called_once_with(
            "behavior-packs-real-load",
            "PASS",
            "all three behavior-pack functions executed inside real BDS",
            count=len(BEHAVIOR_PACKS),
        )

    def test_behavior_pack_function_rejections_remain_fail_closed(self) -> None:
        for index, pack in enumerate(BEHAVIOR_PACKS):
            with self.subTest(function=pack["function"]):
                validator = mock.Mock()
                target = f"execute run function {pack['function']}"

                def command_check(
                    _name: str,
                    command: str,
                    *,
                    target: str = target,
                    previous_packs: tuple[dict[str, str], ...] = BEHAVIOR_PACKS[:index],
                ) -> list[str]:
                    if command == target:
                        return ["Syntax error: unknown function"]
                    for previous in previous_packs:
                        if command == f"execute run function {previous['function']}":
                            return [previous["marker"]]
                    raise AssertionError(f"unexpected command: {command}")

                validator.command_check.side_effect = command_check

                with self.assertRaisesRegex(RuntimeError, "rejected behavior-pack function"):
                    exact.CombinedPackGameruleFleetValidation.verify_behavior_pack_functions(validator)

                self.assertEqual(validator.command_check.call_args_list[-1].args, (
                    f"behavior-pack-function-{pack['function']}",
                    f"execute run function {pack['function']}",
                ))

    def test_behavior_pack_function_missing_marker_remains_fail_closed(self) -> None:
        validator = mock.Mock()
        validator.command_check.return_value = ["unrelated output"]

        with self.assertRaisesRegex(RuntimeError, "executed without expected marker"):
            exact.CombinedPackGameruleFleetValidation.verify_behavior_pack_functions(validator)

    def test_install_artifacts_requires_exact_component_identity(self) -> None:
        validator = _Validator()
        env = {
            "EXPECTED_SPARK_SHA": "a" * 40,
            "EXPECTED_ENDSTONE_SHA": "b" * 40,
            "EXPECTED_ENDSTONE_RUN_ID": "21",
            "EXPECTED_ENDSTONE_ARTIFACT_ID": "22",
            "EXPECTED_ENDSTONE_VERSION": "0.11.10.dev387",
        }
        with (
            mock.patch.object(exact, "_ORIGINAL_INSTALL_ARTIFACTS", lambda _self: None),
            mock.patch.object(exact, "validate_endstone_runtime_version", return_value="0.11.10.dev387"),
            mock.patch.dict(os.environ, env, clear=True),
        ):
            exact._install_exact_artifacts(validator)  # type: ignore[arg-type]

        name, status, fields = validator.checks[-1]
        self.assertEqual((name, status), ("exact-artifact-provenance", "PASS"))
        self.assertEqual(fields["spark_sha"], "a" * 40)
        self.assertEqual(fields["endstone_sha"], "b" * 40)
        self.assertEqual(fields["endstone_run_id"], 21)
        self.assertEqual(fields["endstone_artifact_id"], 22)

    def test_start_server_requires_protocol_and_full_runtime_version(self) -> None:
        validator = _Validator()
        validator.platform = "linux"  # type: ignore[attr-defined]
        with (
            mock.patch.object(exact, "_ORIGINAL_START_SERVER", lambda _self: None),
            mock.patch.dict(os.environ, {"EXPECTED_BDS_VERSION": "1.26.44.3"}, clear=True),
        ):
            exact._start_exact_server(validator)  # type: ignore[arg-type]

        name, status, fields = validator.checks[-1]
        self.assertEqual((name, status), ("exact-bds-version", "PASS"))
        self.assertEqual(fields["observed_protocol"], "26.44")
        self.assertEqual(fields["expected_full"], "1.26.44.3")

    def test_start_server_rejects_full_runtime_drift(self) -> None:
        validator = _Validator()
        validator.platform = "linux"  # type: ignore[attr-defined]
        validator.server.snapshot = lambda: ["Version: 1.26.45.0"]  # type: ignore[method-assign]
        with (
            mock.patch.object(exact, "_ORIGINAL_START_SERVER", lambda _self: None),
            mock.patch.dict(os.environ, {"EXPECTED_BDS_VERSION": "1.26.44.3"}, clear=True),
            self.assertRaisesRegex(RuntimeError, "full version mismatch"),
        ):
            exact._start_exact_server(validator)  # type: ignore[arg-type]

    def test_framework_shutdown_process_sends_cishutdown(self) -> None:
        process = _WaitProcess(returncode=0)
        server = _framework_server(process)
        commands: list[str] = []
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = lambda command: commands.append(command) or 0  # type: ignore[method-assign]
        server.snapshot = lambda: ["CI lifecycle shutdown requested"]  # type: ignore[method-assign]

        self.assertTrue(server.graceful_stop(7.5))

        self.assertEqual(commands, ["cishutdown"])
        self.assertEqual(process.wait_timeouts, [7.5])
        self.assertEqual(server.lifecycle_diagnostic["method"], "interactive-cishutdown")
        self.assertEqual(server.lifecycle_diagnostic["returncode"], 0)
        self.assertEqual(server.lifecycle_diagnostic["wrapper_outcome"], "exited")
        self.assertEqual(server.lifecycle_diagnostic["wrapper_return_code"], 0)
        self.assertTrue(server.lifecycle_diagnostic["acknowledgement_evidence"]["observed"])

    def test_framework_shutdown_requires_zero_exit_code(self) -> None:
        process = _WaitProcess(returncode=17)
        server = _framework_server(process)
        commands: list[str] = []
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = lambda command: commands.append(command) or 0  # type: ignore[method-assign]

        self.assertFalse(server.graceful_stop(3.0))
        self.assertEqual(commands, ["cishutdown"])
        self.assertEqual(server.lifecycle_diagnostic["outcome"], "nonzero-exit")
        self.assertEqual(server.lifecycle_diagnostic["wrapper_outcome"], "nonzero-exit")
        self.assertEqual(server.lifecycle_diagnostic["wrapper_return_code"], 17)

    def test_framework_shutdown_records_command_failure(self) -> None:
        process = _WaitProcess(returncode=0)
        server = _framework_server(process)
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = mock.Mock(side_effect=RuntimeError("command route unavailable"))  # type: ignore[method-assign]

        self.assertFalse(server.graceful_stop(3.0))
        self.assertEqual(server.lifecycle_diagnostic["outcome"], "exception")
        self.assertEqual(server.lifecycle_diagnostic["exception_type"], "RuntimeError")
        self.assertEqual(server.lifecycle_diagnostic["wrapper_outcome"], "exception")
        self.assertEqual(server.lifecycle_diagnostic["wrapper_return_code"], 0)

    def test_framework_shutdown_records_timeout_and_captured_acknowledgement(self) -> None:
        process = _WaitProcess(timeout=True)
        server = _framework_server(process)
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = lambda _command: 0  # type: ignore[method-assign]
        server.snapshot = lambda: ["CI lifecycle shutdown requested"]  # type: ignore[method-assign]

        self.assertFalse(server.graceful_stop(3.0))

        diagnostic = server.lifecycle_diagnostic
        self.assertEqual(diagnostic["outcome"], "timeout")
        self.assertEqual(diagnostic["wrapper_outcome"], "timeout")
        self.assertEqual(diagnostic["wrapper_return_code"], 0)
        self.assertEqual(diagnostic["timeout_reason"], "process did not exit within 3.0s")
        self.assertEqual(diagnostic["cleanup_outcome"], "timeout")
        self.assertEqual(diagnostic["process_tree_verification"], "clean")
        evidence = diagnostic["acknowledgement_evidence"]
        self.assertTrue(evidence["command_sent"])
        self.assertTrue(evidence["observed"])
        self.assertEqual(evidence["source"], "captured-output")

    def test_framework_shutdown_preserves_residual_tree_evidence_without_changing_success_rule(self) -> None:
        process = _WaitProcess(returncode=0)
        server = _framework_server(process)
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = lambda _command: 0  # type: ignore[method-assign]
        residual = [{"pid": 7, "name": "bedrock_server", "alive": True, "identity_match": True}]
        server.process_tree_snapshot = mock.Mock(side_effect=[[], residual])  # type: ignore[method-assign]

        self.assertTrue(server.graceful_stop(3.0))

        diagnostic = server.lifecycle_diagnostic
        self.assertEqual(diagnostic["process_tree_after"], residual)
        self.assertEqual(diagnostic["bds_child_liveness_after"], residual)
        self.assertEqual(diagnostic["process_tree_verification"], "residual-processes")
        self.assertEqual(diagnostic["cleanup_outcome"], "graceful-exit")

    def test_phase_shutdown_persists_distinct_phase_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            validator = CombinedPackGameruleFleetValidation.__new__(CombinedPackGameruleFleetValidation)
            validator._shutdown_phase_ordinal = 0
            validator.result = {"shutdown_lifecycle_events": []}
            validator.result_path = root / "test-results.json"
            validator.fleet_result = root / "fleet-spark-result.json"
            servers = []
            for phase_name in ("bootstrap-provisioning", "bootstrap-world"):
                server = mock.Mock()
                server.lifecycle_diagnostic = {
                    "phase_ordinal": None,
                    "phase_name": None,
                    "outcome": "exited",
                    "returncode": 0,
                    "process_tree_before": [],
                    "process_tree_after": [],
                    "process_tree_verification": "clean",
                    "forced": False,
                    "cleanup_outcome": "graceful-exit",
                    "acknowledgement_evidence": {
                        "command_sent": True,
                        "observed": True,
                    },
                }

                def graceful_stop(_timeout: float, *, server: mock.Mock = server) -> bool:
                    phase = server.lifecycle_phase
                    server.lifecycle_diagnostic["phase_ordinal"] = phase["ordinal"]
                    server.lifecycle_diagnostic["phase_name"] = phase["name"]
                    return True

                server.graceful_stop.side_effect = graceful_stop
                servers.append(server)

            for server, phase_name in zip(servers, ("bootstrap-provisioning", "bootstrap-world")):
                validator.server = server
                validator.stop_server_for_phase_change(phase_name)

            persisted = json.loads(validator.result_path.read_text(encoding="utf-8"))
            events = persisted["shutdown_lifecycle_events"]
            self.assertEqual(len(events), 2)
            self.assertEqual(
                [(event["phase_ordinal"], event["phase_name"]) for event in events],
                [(1, "bootstrap-provisioning"), (2, "bootstrap-world")],
            )
            self.assertTrue(all(event["acknowledgement_evidence"]["observed"] for event in events))
            self.assertTrue(all(event["process_tree_verification"] == "clean" for event in events))
            self.assertTrue(all(event["forced"] is False for event in events))

    def test_public_profile_assigns_third_distinct_phase_before_inherited_shutdown(self) -> None:
        validator = CombinedPackGameruleFleetValidation.__new__(CombinedPackGameruleFleetValidation)
        validator._shutdown_phase_ordinal = 0
        validator.server = mock.Mock()
        phases: list[dict[str, object]] = []
        validator._set_phase_shutdown_context("bootstrap-provisioning")
        phases.append(dict(validator.server.lifecycle_phase))
        validator._set_phase_shutdown_context("bootstrap-world")
        phases.append(dict(validator.server.lifecycle_phase))
        validator._set_phase_shutdown_context("public-profile-final", phase_ordinal=3)
        phases.append(dict(validator.server.lifecycle_phase))

        self.assertEqual(
            phases,
            [
                {"ordinal": 1, "name": "bootstrap-provisioning"},
                {"ordinal": 2, "name": "bootstrap-world"},
                {"ordinal": 3, "name": "public-profile-final"},
            ],
        )
        self.assertEqual(len({(phase["ordinal"], phase["name"]) for phase in phases}), 3)

    def test_public_profile_phase_sets_final_identity_before_inherited_shutdown(self) -> None:
        validator = CombinedPackGameruleFleetValidation.__new__(CombinedPackGameruleFleetValidation)
        validator._shutdown_phase_ordinal = 2
        validator.result = {"health_upload_viewer_url": "health"}
        validator.public_bot_log = Path("public-bot.log")
        validator.server = mock.Mock()
        server = validator.server
        validator.start_server = mock.Mock()
        validator.wait_post_start_initialization = mock.Mock()
        validator.verify_behavior_pack_functions = mock.Mock()
        validator.apply_modified_gamerules = mock.Mock()
        validator.start_fleet = mock.Mock()
        validator.assert_20_players = mock.Mock()
        validator.command_check = mock.Mock()
        validator.run_public_health_upload = mock.Mock(return_value="health")
        validator.profile_execution = mock.Mock(return_value=("execution", []))
        validator.run_profiler = mock.Mock(return_value="allocation")
        validator.parse_spark_metrics = mock.Mock(return_value={})
        validator.stop_fleet = mock.Mock()
        validator._write_results = mock.Mock()
        validator.check = mock.Mock()
        validator.server.command.return_value = 0
        validator.server.wait_command_output.return_value = []

        with (
            mock.patch.object(validation.time, "sleep"),
            mock.patch.object(IntegrationTest, "shutdown") as inherited_shutdown,
        ):
            validator.run_public_profile_phase()

        inherited_shutdown.assert_called_once_with()
        self.assertEqual(
            server.lifecycle_phase,
            {"ordinal": 3, "name": "public-profile-final"},
        )

    def test_phase_shutdown_persists_forced_cleanup_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            validator = CombinedPackGameruleFleetValidation.__new__(CombinedPackGameruleFleetValidation)
            validator._shutdown_phase_ordinal = 0
            validator.result = {"shutdown_lifecycle_events": []}
            validator.result_path = root / "test-results.json"
            validator.fleet_result = root / "fleet-spark-result.json"
            server = mock.Mock()
            server.lifecycle_diagnostic = {
                "outcome": "timeout",
                "returncode": None,
                "process_tree_before": [],
                "process_tree_after": [],
                "process_tree_verification": "clean",
                "forced": False,
                "cleanup_outcome": "timeout",
                "acknowledgement_evidence": {"command_sent": True, "observed": False},
            }

            def graceful_stop(_timeout: float) -> bool:
                phase = server.lifecycle_phase
                server.lifecycle_diagnostic.update(phase_ordinal=phase["ordinal"], phase_name=phase["name"])
                return False

            def force_kill_tree() -> None:
                server.lifecycle_diagnostic.update(
                    forced=True,
                    process_tree_after_force=[],
                    bds_child_liveness_after_force=[],
                    process_tree_verification="clean",
                    cleanup_outcome="clean",
                    outcome="forced",
                )

            server.graceful_stop.side_effect = graceful_stop
            server.force_kill_tree.side_effect = force_kill_tree
            validator.server = server

            with self.assertRaisesRegex(RuntimeError, "did not stop gracefully"):
                validator.stop_server_for_phase_change("bootstrap-provisioning")

            persisted = json.loads(validator.result_path.read_text(encoding="utf-8"))
            events = persisted["shutdown_lifecycle_events"]
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual((event["phase_ordinal"], event["phase_name"]), (1, "bootstrap-provisioning"))
            self.assertTrue(event["forced"])
            self.assertEqual(event["cleanup_outcome"], "clean")
            self.assertEqual(event["process_tree_verification"], "clean")

    def test_force_cleanup_preserves_wrapper_outcome_and_return_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            validator = CombinedPackGameruleFleetValidation.__new__(CombinedPackGameruleFleetValidation)
            validator._shutdown_phase_ordinal = 0
            validator.result = {"shutdown_lifecycle_events": []}
            validator.result_path = root / "test-results.json"
            validator.fleet_result = root / "fleet-spark-result.json"
            server = mock.Mock()
            server.lifecycle_diagnostic = {
                "phase_ordinal": 1,
                "phase_name": "bootstrap-provisioning",
                "wrapper_outcome": "timeout",
                "wrapper_return_code": 0,
                "outcome": "timeout",
                "returncode": 0,
                "process_tree_before": [],
                "process_tree_after": [],
                "process_tree_verification": "clean",
                "forced": False,
                "cleanup_outcome": "timeout",
                "acknowledgement_evidence": {"command_sent": True, "observed": False},
            }
            server.graceful_stop.return_value = False

            def force_kill_tree() -> None:
                server.lifecycle_diagnostic.update(outcome="forced", returncode=-9, forced=True, cleanup_outcome="clean")

            server.force_kill_tree.side_effect = force_kill_tree
            validator.server = server

            with self.assertRaisesRegex(RuntimeError, "did not stop gracefully"):
                validator.stop_server_for_phase_change("bootstrap-provisioning")

            event = json.loads(validator.result_path.read_text(encoding="utf-8"))["shutdown_lifecycle_events"][0]
            self.assertEqual(event["wrapper_outcome"], "timeout")
            self.assertEqual(event["wrapper_return_code"], 0)
            self.assertEqual(event["outcome"], "forced")
            self.assertEqual(event["returncode"], -9)

    def test_force_cleanup_retry_updates_one_stable_phase_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            validator = CombinedPackGameruleFleetValidation.__new__(CombinedPackGameruleFleetValidation)
            validator._shutdown_phase_ordinal = 0
            validator.result = {"shutdown_lifecycle_events": []}
            validator.result_path = root / "test-results.json"
            validator.fleet_result = root / "fleet-spark-result.json"
            server = mock.Mock()
            server.lifecycle_diagnostic = {
                "phase_ordinal": 1,
                "phase_name": "bootstrap-provisioning",
                "wrapper_outcome": "exception",
                "wrapper_return_code": None,
                "outcome": "exception",
                "returncode": None,
                "process_tree_before": [],
                "process_tree_after": [],
                "process_tree_verification": "verification-failed",
                "forced": False,
                "cleanup_outcome": "command-failed",
                "acknowledgement_evidence": {"command_sent": False, "observed": False},
            }

            def force_kill_tree() -> None:
                if server.force_kill_tree.call_count == 1:
                    raise RuntimeError("transient cleanup failure")
                server.lifecycle_diagnostic.update(
                    outcome="forced",
                    returncode=-9,
                    forced=True,
                    cleanup_outcome="clean",
                    process_tree_verification="clean",
                )

            server.graceful_stop.return_value = False
            server.force_kill_tree.side_effect = force_kill_tree
            validator.server = server

            with self.assertRaisesRegex(RuntimeError, "transient cleanup failure"):
                validator.stop_server_for_phase_change("bootstrap-provisioning")

            server.force_kill_tree()
            validator._record_phase_lifecycle()
            events = json.loads(validator.result_path.read_text(encoding="utf-8"))["shutdown_lifecycle_events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["phase_name"], "bootstrap-provisioning")
            self.assertEqual(events[0]["wrapper_outcome"], "exception")
            self.assertEqual(events[0]["outcome"], "forced")

    def test_windows_start_uses_interactive_command_map_and_lifecycle_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            validator = _Validator()
            validator.platform = "windows"  # type: ignore[attr-defined]
            validator.root = root  # type: ignore[attr-defined]
            validator.server_dir = root / "bedrock_server"  # type: ignore[attr-defined]
            validator.server_dir.mkdir()  # type: ignore[attr-defined]
            validator.log_path = root / "bds.log"  # type: ignore[attr-defined]
            validator.result_path = root / "test-results.json"  # type: ignore[attr-defined]
            validator.server_dir.joinpath("version.txt").write_text("26.44", encoding="utf-8")  # type: ignore[attr-defined]
            _StartedServer.created_cmd = None
            with (
                mock.patch.object(exact, "_FrameworkShutdownServerProcess", _StartedServer),
                mock.patch.dict(os.environ, {"EXPECTED_BDS_VERSION": "1.26.44.3"}, clear=True),
            ):
                exact._start_exact_server(validator)  # type: ignore[arg-type]

        self.assertIsNotNone(_StartedServer.created_cmd)
        assert _StartedServer.created_cmd is not None
        self.assertNotIn("--no-interactive", _StartedServer.created_cmd)
        self.assertIn("--server-folder", _StartedServer.created_cmd)
        self.assertEqual(validator.result["bds_version"], "26.44")
        lifecycle = [
            fields
            for name, status, fields in validator.checks
            if name == "windows-interactive-lifecycle" and status == "PASS"
        ]
        self.assertEqual(lifecycle, [{"shutdown_control": "cishutdown"}])
        self.assertEqual(validator.checks[-1][0:2], ("exact-bds-version", "PASS"))


if __name__ == "__main__":
    unittest.main()
