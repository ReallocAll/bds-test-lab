from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from controller import combined_windows_final_runner as final
from controller.combined_pack_gamerule_fleet_validation import WORLD_NAME


class _FileCommandServer:
    def __init__(self, root: Path, lines: list[str]) -> None:
        self.cwd = root
        self._lines = lines
        self._pending_file_commands: dict[int, str] = {}

    def snapshot(self) -> list[str]:
        return list(self._lines)

    def is_alive(self) -> bool:
        return True


class CombinedWindowsFinalRunnerTest(unittest.TestCase):
    def test_file_control_command_writes_tokenized_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "command.request"
            server = _FileCommandServer(
                root,
                [f"CI lifecycle control enabled; command-control={request}; file-control={root / 'shutdown.request'}"],
            )

            start = final._file_control_command(server, "spark tps")  # type: ignore[arg-type]

            self.assertEqual(start, 1)
            payload = json.loads(request.read_text(encoding="utf-8"))
            self.assertEqual(payload["command"], "spark tps")
            self.assertTrue(payload["token"])
            self.assertEqual(server._pending_file_commands[start], payload["token"])

    def test_file_control_command_rejects_pending_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "command.request"
            request.write_text("pending", encoding="utf-8")
            server = _FileCommandServer(
                root,
                [f"CI lifecycle control enabled; command-control={request}; file-control={root / 'shutdown.request'}"],
            )

            with self.assertRaisesRegex(RuntimeError, "previous CI command request is still pending"):
                final._file_control_command(server, "spark tps")  # type: ignore[arg-type]

    def test_file_control_wait_requires_positive_dispatch_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = _FileCommandServer(root, [])
            server._pending_file_commands[0] = "abc123"
            server._lines = [
                "CI command dispatch requested; token=abc123; command=spark tps",
                "CI command dispatch completed; token=abc123; dispatched=true",
            ]

            output = final._wait_file_control_command_output(server, 0, 0.2)  # type: ignore[arg-type]

            self.assertEqual(output, server._lines)
            self.assertNotIn(0, server._pending_file_commands)

    def test_file_control_wait_fails_closed_on_rejected_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = _FileCommandServer(root, [])
            server._pending_file_commands[0] = "abc123"
            server._lines = ["CI command dispatch completed; token=abc123; dispatched=false"]

            with self.assertRaisesRegex(RuntimeError, "rejected CI command transport request"):
                final._wait_file_control_command_output(server, 0, 0.2)  # type: ignore[arg-type]

    def test_windows_bootstrap_reuses_exactly_one_bds_created_world(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server_dir = Path(tmp)
            worlds = server_dir / "worlds"
            properties = server_dir / "server.properties"
            validator = mock.Mock()
            validator.platform = "windows"
            validator.server_dir = server_dir

            def start_server() -> None:
                worlds.mkdir(parents=True, exist_ok=True)
                (worlds / "Bedrock level").mkdir()
                properties.write_text(
                    "server-name=Dedicated Server\n"
                    "level-name=Bedrock level\n"
                    "online-mode=true\n"
                    "allow-cheats=false\n"
                    "max-players=10\n"
                    "player-idle-timeout=30\n",
                    encoding="utf-8",
                )

            validator.start_server.side_effect = start_server

            final._bootstrap_windows_from_provisioned_world(validator)

            validator.start_server.assert_called_once_with()
            validator.wait_post_start_initialization.assert_called_once_with()
            validator.stop_server_for_phase_change.assert_called_once_with("bootstrap-provisioning")
            target = worlds / WORLD_NAME
            self.assertTrue(target.is_dir())
            self.assertFalse((worlds / "Bedrock level").exists())
            validator.install_behavior_packs.assert_called_once_with(target)
            rendered = properties.read_text(encoding="utf-8")
            self.assertIn(f"level-name={WORLD_NAME}", rendered)
            self.assertIn("max-players=30", rendered)
            self.assertIn("allow-cheats=true", rendered)
            self.assertIn("player-idle-timeout=0", rendered)
            validator.check.assert_called_once()
            self.assertEqual(validator.check.call_args.args[:2], ("combined-world-bootstrap", "PASS"))
            self.assertEqual(validator.check.call_args.kwargs["windows_bootstrap_server_starts"], 1)

    def test_windows_bootstrap_fails_closed_when_world_creation_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server_dir = Path(tmp)
            validator = mock.Mock()
            validator.platform = "windows"
            validator.server_dir = server_dir

            def start_server() -> None:
                worlds = server_dir / "worlds"
                worlds.mkdir(parents=True, exist_ok=True)
                (worlds / "World A").mkdir()
                (worlds / "World B").mkdir()
                (server_dir / "server.properties").write_text("level-name=World A\n", encoding="utf-8")

            validator.start_server.side_effect = start_server

            with self.assertRaisesRegex(RuntimeError, "exactly one fresh world"):
                final._bootstrap_windows_from_provisioned_world(validator)

            validator.install_behavior_packs.assert_not_called()

    def test_non_windows_keeps_original_bootstrap(self) -> None:
        validator = mock.Mock()
        validator.platform = "linux"
        with mock.patch.object(final, "_ORIGINAL_BOOTSTRAP_SCENARIO_WORLD") as original:
            final._bootstrap_windows_from_provisioned_world(validator)
        original.assert_called_once_with(validator)


if __name__ == "__main__":
    unittest.main()
