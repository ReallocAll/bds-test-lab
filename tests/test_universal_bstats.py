from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

import controller.combined_pack_gamerule_fleet_exact_runner as combined_exact
from controller.bstats import B_STATS_CONFIG_BYTES, B_STATS_EVIDENCE_PATH
from controller.combined_pack_gamerule_fleet_validation import (
    CombinedPackGameruleFleetValidation,
)
from controller.cross_platform_fleet_validation import CrossPlatformFleetSparkValidation
from controller.python_attribution_performance import PythonAttributionPerformance
from controller.python_attribution_validation import PythonAttributionValidation
from controller.recovery_multisession_validation import RecoveryMultiSessionValidation
from controller.run_test import IntegrationTest
from controller.verify_bstats_evidence import verify_result


class _LaunchProbe:
    launches: ClassVar[list[bytes]] = []

    def __init__(self, command: list[str], _root: Path, _log_path: Path) -> None:
        self.server_dir = Path(command[-1])

    def start(self) -> None:
        config = self.server_dir / "plugins" / "bstats" / "config.toml"
        type(self).launches.append(config.read_bytes())

    def wait_for(self, predicate, _timeout: float, _description: str) -> list[str]:
        lines = [
            "Server started.",
            "[Endstone] Enabling spark",
            "CI lifecycle control enabled; cishutdown registered",
        ]
        if not predicate(lines):
            raise AssertionError("launch probe did not satisfy wait predicate")
        return lines


class UniversalBStatsTest(unittest.TestCase):
    def _fixture(self, root: Path, enabled: bool) -> IntegrationTest:
        fixture = IntegrationTest.__new__(IntegrationTest)
        fixture.platform = "linux"
        fixture.root = root
        fixture.downloads = root / "downloads"
        fixture.log_path = root / "bds.log"
        fixture.metadata_path = root / "metadata.json"
        fixture.server_dir = root / "work" / "linux" / "bedrock_server"
        fixture.result = {"checks": []}
        fixture.result_path = root / "test-results.json"
        fixture.disable_bstats = enabled
        fixture.check = mock.Mock()
        fixture.server_dir.mkdir(parents=True)
        (fixture.downloads / "endstone" / "payload").mkdir(parents=True)
        spark_root = fixture.downloads / "spark" / "payload"
        spark_root.mkdir(parents=True)
        (fixture.downloads / "endstone" / "payload" / "endstone-test.whl").write_bytes(b"wheel")
        (spark_root / "endstone_spark.so").write_bytes(b"spark")
        return fixture

    def test_opt_in_install_writes_config_before_plugin_copy_and_records_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self._fixture(root, enabled=True)
            copy_order: list[str] = []

            def fake_copy(source: Path, target: Path) -> None:
                copy_order.append(source.name)
                target.write_bytes(source.read_bytes())

            with (
                mock.patch("controller.run_test.resolve_artifacts", return_value={}),
                mock.patch("controller.run_test.run_checked"),
                mock.patch("controller.run_test.shutil.copy2", side_effect=fake_copy),
            ):
                fixture.install_artifacts()

            config_path = fixture.server_dir / "plugins" / "bstats" / "config.toml"
            evidence_path = root / B_STATS_EVIDENCE_PATH
            self.assertEqual(config_path.read_bytes(), B_STATS_CONFIG_BYTES)
            self.assertEqual(evidence_path.read_bytes(), B_STATS_CONFIG_BYTES)
            self.assertEqual(copy_order, ["endstone_spark.so"])
            self.assertEqual(fixture.result["bstats_config"]["canonical_enabled"], False)
            self.assertEqual(fixture.result["bstats_config"]["bytes"], len(B_STATS_CONFIG_BYTES))
            self.assertTrue(fixture.result["bstats_config"]["sha256"])
            fixture.result_path.write_text(json.dumps(fixture.result), encoding="utf-8")
            self.assertEqual(verify_result(fixture.result_path), fixture.result["bstats_config"])

    def test_default_install_does_not_create_bstats_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self._fixture(root, enabled=False)
            with (
                mock.patch("controller.run_test.resolve_artifacts", return_value={}),
                mock.patch("controller.run_test.run_checked"),
                mock.patch(
                    "controller.run_test.shutil.copy2",
                    side_effect=lambda source, target: target.write_bytes(source.read_bytes()),
                ),
            ):
                fixture.install_artifacts()
            self.assertNotIn("bstats_config", fixture.result)
            self.assertFalse((root / B_STATS_EVIDENCE_PATH).exists())

    def test_verifier_rejects_tampered_config_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self._fixture(root, enabled=True)
            with (
                mock.patch("controller.run_test.resolve_artifacts", return_value={}),
                mock.patch("controller.run_test.run_checked"),
            ):
                fixture.install_artifacts()
            fixture.result_path.write_text(json.dumps(fixture.result), encoding="utf-8")
            evidence_path = root / B_STATS_EVIDENCE_PATH
            evidence_path.write_text("enabled = true\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not canonical"):
                verify_result(fixture.result_path)
            evidence_path.write_bytes(B_STATS_CONFIG_BYTES)
            fixture.result["bstats_config"]["sha256"] = "0" * 64
            fixture.result_path.write_text(json.dumps(fixture.result), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "metadata does not match"):
                verify_result(fixture.result_path)

    def test_formal_controllers_opt_in_and_base_remains_opt_out(self) -> None:
        self.assertFalse(IntegrationTest.disable_bstats)
        self.assertFalse(PythonAttributionPerformance.disable_bstats)
        for controller in (
            CrossPlatformFleetSparkValidation,
            CombinedPackGameruleFleetValidation,
            PythonAttributionValidation,
            RecoveryMultiSessionValidation,
        ):
            with self.subTest(controller=controller.__name__):
                self.assertTrue(controller.disable_bstats)

    def test_base_restart_reasserts_bstats_after_tampering_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self._fixture(root, enabled=True)
            with (
                mock.patch("controller.run_test.resolve_artifacts", return_value={}),
                mock.patch("controller.run_test.run_checked"),
                mock.patch("controller.run_test.shutil.copy2", side_effect=lambda source, target: target.write_bytes(source.read_bytes())),
            ):
                fixture.install_artifacts()
            config = fixture.server_dir / "plugins" / "bstats" / "config.toml"
            config.unlink()
            _LaunchProbe.launches = []
            with mock.patch("controller.run_test.ServerProcess", _LaunchProbe):
                fixture.start_server()
                config.write_text("enabled = true\n", encoding="utf-8")
                fixture.start_server()
            self.assertEqual(_LaunchProbe.launches, [B_STATS_CONFIG_BYTES, B_STATS_CONFIG_BYTES])
            self.assertEqual((root / B_STATS_EVIDENCE_PATH).read_bytes(), B_STATS_CONFIG_BYTES)
            self.assertEqual(fixture.result["bstats_config"]["canonical_enabled"], False)

    def test_python_custom_starter_reasserts_bstats_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self._fixture(root, enabled=True)
            config = fixture.server_dir / "plugins" / "bstats" / "config.toml"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text("enabled = true\n", encoding="utf-8")
            fixture.platform = "windows"
            _LaunchProbe.launches = []
            with mock.patch("controller.python_attribution_validation.ServerProcess", _LaunchProbe):
                PythonAttributionValidation.start_server(fixture)  # type: ignore[arg-type]
            self.assertEqual(_LaunchProbe.launches, [B_STATS_CONFIG_BYTES])

    def test_windows_custom_starter_reasserts_bstats_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = self._fixture(root, enabled=True)
            config = fixture.server_dir / "plugins" / "bstats" / "config.toml"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text("enabled = true\n", encoding="utf-8")
            fixture.platform = "windows"
            _LaunchProbe.launches = []
            with mock.patch.object(combined_exact, "_FrameworkShutdownServerProcess", _LaunchProbe):
                combined_exact._start_windows_interactive_server(fixture)  # type: ignore[arg-type]
            self.assertEqual(_LaunchProbe.launches, [B_STATS_CONFIG_BYTES])


if __name__ == "__main__":
    unittest.main()
