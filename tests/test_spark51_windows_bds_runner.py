from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from controller.spark51_windows_bds_runner import (
    ALLOCATION_INTERVAL_BYTES,
    ALLOCATION_KIND,
    BOT_COUNT,
    CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT,
    CPU_BASELINE_KIND,
    CPU_LOAD_KIND,
    POST_RELOAD_KIND,
    RELOAD_CYCLES,
    SPARK_CANDIDATE_SHA,
    Spark51WindowsBdsValidation,
    _artifact_record,
    _ordered_reload_evidence,
    _select_live_bds_identity,
)


def _profiles(seconds: int = 30) -> list[dict[str, object]]:
    command = f"spark profiler start --timeout {seconds}"
    allocation_command = f"{command} --alloc --interval {ALLOCATION_INTERVAL_BYTES}"
    return [
        {
            "kind": CPU_BASELINE_KIND,
            "viewer_url": "https://spark.lucko.me/cpu-baseline",
            "player_count": 0,
            "reload_cycle": 0,
            "allocation_interval": None,
            "command": command,
        },
        {
            "kind": CPU_LOAD_KIND,
            "viewer_url": "https://spark.lucko.me/20-player-load",
            "player_count": BOT_COUNT,
            "reload_cycle": 0,
            "allocation_interval": None,
            "command": command,
        },
        {
            "kind": ALLOCATION_KIND,
            "viewer_url": "https://spark.lucko.me/allocation-4096",
            "player_count": BOT_COUNT,
            "reload_cycle": 0,
            "allocation_interval": ALLOCATION_INTERVAL_BYTES,
            "command": allocation_command,
        },
        {
            "kind": POST_RELOAD_KIND,
            "viewer_url": "https://spark.lucko.me/post-reload-cycle-3",
            "player_count": BOT_COUNT,
            "reload_cycle": RELOAD_CYCLES,
            "allocation_interval": None,
            "command": command,
        },
    ]


def _reloads() -> list[dict[str, object]]:
    return [
        {
            "cycle": cycle,
            "command": "reload",
            "transport": "stdin",
            "command_published": True,
            "dispatch_acknowledged": False,
            "dispatch_acknowledgement": None,
            "command_acknowledged": False,
            "command_acknowledgement": None,
            "reload_complete": True,
            "spark_enabled": True,
            "spark_disable_evidence": "[Endstone] Disabling spark",
            "spark_enable_evidence": "[Endstone] Enabling spark",
            "reload_completion": "Reload complete.",
            "reload_evidence_order": ["spark-disable", "spark-enable", "reload-complete"],
            "reload_evidence_lines": [
                "[Endstone] Disabling spark",
                "[Endstone] Enabling spark",
                "Reload complete.",
            ],
            "bds_pid": 4242,
            "bds_create_time": 100.0,
            "before_bds_pid": 4242,
            "before_bds_create_time": 100.0,
            "same_bds_identity": True,
            "player_count": BOT_COUNT,
        }
        for cycle in range(1, RELOAD_CYCLES + 1)
    ]


def _lifecycle_event(phase: str, *, return_code: int = 0, acknowledged: bool = True) -> dict[str, object]:
    return {
        "phase_name": phase,
        "wrapper_return_code": return_code,
        "returncode": return_code,
        "process_tree_verification": "clean",
        "forced": False,
        "acknowledgement_evidence": {"observed": acknowledged},
    }


def _validator() -> Spark51WindowsBdsValidation:
    validator = Spark51WindowsBdsValidation.__new__(Spark51WindowsBdsValidation)
    validator.profile_seconds = 30
    validator.result = {
        "profiles": _profiles(),
        "plugin_reload_cycles": _reloads(),
        "shutdown_status": "not_started",
        "shutdown_lifecycle_events": [],
    }
    validator._write_results = mock.Mock()
    return validator


class Spark51WindowsBdsRunnerTest(unittest.TestCase):
    def test_workload_validation_accepts_exact_profiles_before_shutdown(self) -> None:
        validator = _validator()
        validator._validate_workload_success()
        self.assertIsNone(validator.result.get("shutdown_evidence"))

    def test_workload_validation_rejects_duplicate_urls_and_wrong_metadata(self) -> None:
        validator = _validator()
        validator.result["profiles"][1]["viewer_url"] = validator.result["profiles"][0]["viewer_url"]
        with self.assertRaisesRegex(RuntimeError, "distinct nonempty"):
            validator._validate_workload_success()

        validator = _validator()
        validator.result["profiles"][2]["command"] = "spark profiler start --timeout 30"
        with self.assertRaisesRegex(RuntimeError, "metadata is not exact"):
            validator._validate_workload_success()

    def test_reload_evidence_requires_disable_enable_complete_order(self) -> None:
        lines = [
            "[Endstone] Disabling spark",
            "[Endstone] Enabling spark v0.6.0",
            "Reload complete.",
        ]
        self.assertEqual(_ordered_reload_evidence(lines, 1), (lines[0], lines[1], lines[2]))
        for invalid in (
            [lines[1], lines[0], lines[2]],
            [lines[0], lines[2], lines[1]],
            [lines[0], lines[2]],
        ):
            with self.subTest(lines=invalid), self.assertRaisesRegex(RuntimeError, "evidence|completion"):
                _ordered_reload_evidence(invalid, 1)

    def test_reload_evidence_rejects_generic_load_and_failure_text(self) -> None:
        valid_disable = "[Endstone] Disabling spark"
        valid_enable = "[Endstone] Enabling spark"
        valid_complete = "Reload complete."
        invalid_cases = (
            [valid_disable, "Spark v0.6.0", valid_complete],
            [valid_disable, "Loaded spark", valid_complete],
            [valid_disable, "[Endstone] Not enabling spark", valid_complete],
            [valid_disable, "[Endstone] Failed to load spark", valid_complete],
            ["[Endstone] Enabling spark", valid_disable, valid_complete],
            [valid_disable, valid_enable, "Exception while reloading", valid_complete],
            [valid_disable, valid_enable, "Reload rejected by Endstone", valid_complete],
            [valid_disable, valid_enable, "dispatch result: false", valid_complete],
        )
        for lines in invalid_cases:
            with self.subTest(lines=lines), self.assertRaisesRegex(RuntimeError, "evidence|failure|enable"):
                _ordered_reload_evidence(lines, 1)

    def test_reload_evidence_is_case_insensitive_and_preserves_exact_lines(self) -> None:
        lines = [
            "prefix [eNdStOnE] dIsAbLiNg SpArK",
            "prefix [ENDSTONE] ENABLING SPARK v0.6.0",
            "Reload Complete.",
        ]
        self.assertEqual(_ordered_reload_evidence(lines, 1), tuple(lines))

    def test_workload_validation_rejects_reload_identity_drift(self) -> None:
        validator = _validator()
        validator.result["plugin_reload_cycles"][2]["bds_pid"] = 9999
        with self.assertRaisesRegex(RuntimeError, "identity"):
            validator._validate_workload_success()

    def test_reload_bypasses_file_command_override_and_publishes_to_stdin(self) -> None:
        class Stdin:
            def __init__(self) -> None:
                self.writes: list[str] = []

            def write(self, value: str) -> int:
                self.writes.append(value)
                return len(value)

            def flush(self) -> None:
                pass

        class FileOverrideServer:
            def __init__(self) -> None:
                self.stdin = Stdin()
                self.process = SimpleNamespace(stdin=self.stdin)

            def command(self, command: str) -> int:
                raise AssertionError(f"reload entered file command override: {command}")

            def is_alive(self) -> bool:
                return True

            def snapshot(self) -> list[str]:
                return []

            def process_tree_snapshot(self) -> list[dict[str, object]]:
                return [{
                    "name": "bedrock_server.exe",
                    "pid": 4242,
                    "create_time": 100.0,
                    "alive": True,
                    "identity_match": True,
                }]

        validator = _validator()
        validator._profile_active = False
        server = FileOverrideServer()
        validator.server = server  # type: ignore[assignment]
        validator.assert_20_players = mock.Mock()  # type: ignore[method-assign]
        validator._wait_reload_complete = mock.Mock(  # type: ignore[method-assign]
            return_value=(
                ["[Endstone] Disabling spark", "[Endstone] Enabling spark", "Reload complete."],
                "[Endstone] Disabling spark",
                "[Endstone] Enabling spark",
                "Reload complete.",
            )
        )

        validator._reload(1, (4242, 100.0))

        self.assertEqual(server.stdin.writes, ["reload\n"])
        record = validator.result["plugin_reload_cycles"][0]
        self.assertEqual(record["transport"], "stdin")
        self.assertTrue(record["command_published"])
        self.assertFalse(record["dispatch_acknowledged"])
        self.assertIsNone(record["dispatch_acknowledgement"])

    def test_shutdown_validation_requires_all_clean_events_and_final_phase(self) -> None:
        validator = _validator()
        validator.result["shutdown_status"] = "graceful"
        validator.result["shutdown_lifecycle_events"] = [
            _lifecycle_event("bootstrap-provisioning"),
            _lifecycle_event("candidate-final-shutdown"),
        ]
        validator._set_shutdown_evidence()
        validator._validate_final_shutdown()

        for field, value, pattern in (
            ("wrapper_return_code", 17, "missing"),
            ("process_tree_verification", "residual-processes", "missing"),
            ("forced", True, "missing"),
            ("acknowledgement_evidence", {"observed": False}, "missing"),
        ):
            with self.subTest(field=field):
                broken = _validator()
                broken.result["shutdown_status"] = "graceful"
                event = _lifecycle_event("candidate-final-shutdown")
                event[field] = value
                broken.result["shutdown_lifecycle_events"] = [event]
                broken._set_shutdown_evidence()
                with self.assertRaisesRegex(RuntimeError, pattern):
                    broken._validate_final_shutdown()

    def test_shutdown_failure_is_not_reported_as_graceful(self) -> None:
        validator = _validator()
        validator.result["shutdown_status"] = "forced"
        validator.result["shutdown_lifecycle_events"] = [_lifecycle_event("candidate-final-shutdown", return_code=1)]
        validator._set_shutdown_evidence()
        self.assertFalse(validator.result["shutdown_evidence"]["graceful"])
        with self.assertRaisesRegex(RuntimeError, "graceful"):
            validator._validate_final_shutdown()

    def test_provenance_rejects_malformed_required_fields(self) -> None:
        valid = {
            "repository": "ReallocAll/spark",
            "sha": SPARK_CANDIDATE_SHA,
            "run_id": 11,
            "run_url": "https://github.com/ReallocAll/spark/actions/runs/11",
            "artifact": {
                "id": 12,
                "name": "spark-windows",
                "digest": "sha256:" + "a" * 64,
            },
        }
        _artifact_record("spark", valid)
        cases = (
            ("repository", "", "repository identity"),
            ("run_url", "", "run URL"),
            ("artifact", {"id": 12, "name": "", "digest": "sha256:" + "a" * 64}, "artifact name"),
            ("artifact", {"id": 12, "name": "spark", "digest": "sha256:" + "A" * 64}, "invalid API digest"),
        )
        for field, value, pattern in cases:
            with self.subTest(field=field):
                malformed = dict(valid)
                if field == "artifact":
                    malformed["artifact"] = value
                else:
                    malformed[field] = value
                with self.assertRaisesRegex(RuntimeError, pattern):
                    _artifact_record("spark", malformed)

    def test_identity_selection_rejects_missing_or_nonfinite_identity(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            _select_live_bds_identity([])
        record = {"name": "bedrock_server", "alive": True, "identity_match": True, "pid": 12, "create_time": "nan"}
        with self.assertRaisesRegex(RuntimeError, "create time"):
            _select_live_bds_identity([record])

    def test_execute_exception_persists_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            validator = _validator()
            root = Path(temp)
            validator.result_path = root / "result.json"
            validator.diagnostics = root / "failure-diagnostics.txt"
            validator.log_path = root / "missing.log"
            validator.endstone_log = root / "endstone.log"
            validator.spark_log = root / "spark.log"
            validator.bot = None
            validator.server = None
            validator._fleet_stopped = False
            validator._write_results = lambda: validator.result_path.write_text(
                json.dumps(validator.result), encoding="utf-8"
            )
            validator.install_artifacts = mock.Mock(side_effect=RuntimeError("artifact failure"))
            validator.split_logs = mock.Mock()

            self.assertEqual(validator.execute_candidate(), 1)
            self.assertEqual(validator.result["status"], "FAIL")
            self.assertEqual(validator.result["state"], "failed")
            self.assertIn("artifact failure", validator.result["error_summary"])
            self.assertTrue(validator.result_path.exists())
            persisted = json.loads(validator.result_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["state"], "failed")
            self.assertIn("artifact failure", persisted["error_summary"])

    def test_candidate_profile_dispatch_uses_bounded_long_ack_budget(self) -> None:
        validator = _validator()
        server = mock.Mock()
        server.command.return_value = 0
        server.wait_command_output.return_value = [
            "CI command dispatch completed; token=abc123; dispatched=true"
        ]
        validator.server = server

        validator._dispatch("spark profiler start --timeout 30", timeout=CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT)

        server.wait_command_output.assert_called_once_with(0, CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT)

    def test_cleanup_bypasses_graceful_stop_when_command_ack_remains_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            events: list[str] = []
            validator = _validator()
            validator.bot = None
            validator._fleet_stopped = False
            validator.shutdown = mock.Mock()
            request = Path(temp) / "command.request"
            request.write_text('{"token":"late-token","command":"spark tps"}\n', encoding="utf-8")

            class PendingServer:
                _pending_file_commands = {0: "late-token"}

                def __init__(self) -> None:
                    self.alive = True
                    self.lifecycle_command_path = request

                def wait_for_pending_file_commands(self, timeout: float) -> bool:
                    self.timeout = timeout
                    return False

                def is_alive(self) -> bool:
                    return self.alive

                def force_kill_tree(self) -> None:
                    events.append("force_kill_tree")
                    self.alive = False

            server = PendingServer()
            validator.server = server  # type: ignore[assignment]
            validator._cleanup_after_failure("primary failure")

            validator.shutdown.assert_not_called()
            self.assertEqual(events, ["force_kill_tree"])
            self.assertEqual(server._pending_file_commands, {})
            self.assertFalse(request.exists())
            diagnostic = validator.result["cleanup_diagnostics"][0]
            self.assertEqual(diagnostic["status"], "terminated")
            self.assertEqual(diagnostic["pending"], {0: "late-token"})
            self.assertEqual(diagnostic["request_path"], str(request))
            self.assertTrue(diagnostic["request_removed"])

    def test_terminal_pending_request_removal_failure_is_recorded_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            validator = _validator()
            validator.bot = None
            validator._fleet_stopped = False
            validator.shutdown = mock.Mock()
            request = Path(temp) / "command.request"
            request.write_text("pending\n", encoding="utf-8")

            class PendingServer:
                _pending_file_commands = {3: "stale-token"}

                def __init__(self) -> None:
                    self.alive = True
                    self.lifecycle_command_path = request

                def wait_for_pending_file_commands(self, timeout: float) -> bool:
                    del timeout
                    return False

                def is_alive(self) -> bool:
                    return self.alive

                def force_kill_tree(self) -> None:
                    self.alive = False

            server = PendingServer()
            validator.server = server  # type: ignore[assignment]
            with mock.patch.object(Path, "unlink", side_effect=OSError("request is locked")):
                validator._cleanup_after_failure("primary failure")

            self.assertEqual(server._pending_file_commands, {})
            self.assertTrue(request.exists())
            self.assertTrue(validator._write_results.called)
            diagnostic = validator.result["cleanup_diagnostics"][0]
            self.assertFalse(diagnostic["request_removed"])
            self.assertIn("request is locked", diagnostic["request_removal_error"])
            self.assertTrue(
                any(
                    item["operation"] == "pending CI command request removal"
                    for item in validator.result["cleanup_errors"]
                )
            )

    def test_cleanup_continues_and_preserves_primary_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            events: list[str] = []
            validator = _validator()
            root = Path(temp)
            validator.result_path = root / "result.json"
            validator.diagnostics = root / "failure-diagnostics.txt"
            validator.log_path = root / "missing.log"
            validator.endstone_log = root / "endstone.log"
            validator.spark_log = root / "spark.log"
            validator._fleet_stopped = False

            class ThrowingBot:
                def is_alive(self) -> bool:
                    return True

                def force_close(self) -> None:
                    events.append("bot.force_close")
                    raise RuntimeError("bot close failure")

            class ThrowingServer:
                def is_alive(self) -> bool:
                    return True

                def force_kill_tree(self) -> None:
                    events.append("server.force_kill_tree")
                    raise RuntimeError("force kill failure")

                def close(self) -> None:
                    events.append("server.close")
                    raise RuntimeError("close failure")

                def snapshot(self) -> list[str]:
                    return []

            validator.bot = ThrowingBot()
            validator.server = ThrowingServer()

            def stop_fleet() -> None:
                events.append("stop_fleet")
                raise RuntimeError("fleet stop failure")

            def shutdown() -> None:
                events.append("shutdown")
                raise RuntimeError("shutdown failure")

            validator.stop_fleet = stop_fleet
            validator.shutdown = shutdown
            validator.install_artifacts = mock.Mock(side_effect=RuntimeError("primary failure"))

            def split_logs() -> None:
                events.append("split")
                raise RuntimeError("split failure")

            validator.split_logs = split_logs

            def persist() -> None:
                events.append("persist")
                validator.result_path.write_text(json.dumps(validator.result), encoding="utf-8")

            validator._write_results = persist

            self.assertEqual(validator.execute_candidate(), 1)
            self.assertIn("primary failure", validator.result["error_summary"])
            self.assertTrue(validator.result_path.exists())
            self.assertTrue(validator.diagnostics.exists())
            self.assertIn("bot.force_close", events)
            self.assertIn("server.force_kill_tree", events)
            self.assertIn("server.close", events)
            self.assertIn("split", events)
            self.assertIn("persist", events)
            cleanup_operations = {item["operation"] for item in validator.result["cleanup_errors"]}
            self.assertTrue(
                {"bot force_close", "BDS force_kill_tree", "server close", "log splitting"}
                <= cleanup_operations
            )
            persisted = json.loads(validator.result_path.read_text(encoding="utf-8"))
            self.assertIn("primary failure", persisted["error_summary"])
            self.assertTrue(persisted["cleanup_errors"])


if __name__ == "__main__":
    unittest.main()
