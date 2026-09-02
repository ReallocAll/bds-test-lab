from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from controller import combined_windows_final_runner as final
from controller.combined_pack_gamerule_fleet_validation import WORLD_NAME


class CombinedWindowsFinalRunnerTest(unittest.TestCase):
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
