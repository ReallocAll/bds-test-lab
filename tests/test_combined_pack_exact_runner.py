from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import controller.combined_pack_gamerule_fleet_exact_runner as exact


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
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.wait_timeouts: list[float] = []

    def wait(self, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        return self.returncode


class CombinedPackExactRunnerTest(unittest.TestCase):
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
        server = exact._FrameworkShutdownServerProcess.__new__(exact._FrameworkShutdownServerProcess)
        process = _WaitProcess(returncode=0)
        commands: list[str] = []
        server.process = process  # type: ignore[assignment]
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = commands.append  # type: ignore[method-assign]

        self.assertTrue(server.graceful_stop(7.5))

        self.assertEqual(commands, ["cishutdown"])
        self.assertEqual(process.wait_timeouts, [7.5])
        self.assertEqual(server.lifecycle_diagnostic["method"], "interactive-cishutdown")
        self.assertEqual(server.lifecycle_diagnostic["returncode"], 0)

    def test_framework_shutdown_requires_zero_exit_code(self) -> None:
        server = exact._FrameworkShutdownServerProcess.__new__(exact._FrameworkShutdownServerProcess)
        process = _WaitProcess(returncode=17)
        commands: list[str] = []
        server.process = process  # type: ignore[assignment]
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = commands.append  # type: ignore[method-assign]

        self.assertFalse(server.graceful_stop(3.0))
        self.assertEqual(commands, ["cishutdown"])
        self.assertEqual(server.lifecycle_diagnostic["outcome"], "nonzero-exit")

    def test_framework_shutdown_records_command_failure(self) -> None:
        server = exact._FrameworkShutdownServerProcess.__new__(exact._FrameworkShutdownServerProcess)
        process = _WaitProcess(returncode=0)
        server.process = process  # type: ignore[assignment]
        server.is_alive = lambda: True  # type: ignore[method-assign]
        server.command = mock.Mock(side_effect=RuntimeError("command route unavailable"))  # type: ignore[method-assign]

        self.assertFalse(server.graceful_stop(3.0))
        self.assertEqual(server.lifecycle_diagnostic["outcome"], "exception")
        self.assertEqual(server.lifecycle_diagnostic["exception_type"], "RuntimeError")

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
