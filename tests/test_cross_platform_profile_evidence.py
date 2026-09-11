from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from controller import cross_platform_fleet_validation as module
from controller.fleet_spark_validation import FleetSparkValidation
from controller.python_profile_payload import parse_sampler_data
from tests.test_final_profiler_matrix import diagnostic_values, profile_fixture


class ProfileEvidenceTests(unittest.TestCase):
    def validator(self, root, mode="execution"):
        validator = object.__new__(module.CrossPlatformFleetSparkValidation)
        validator.root = Path(root)
        validator.result = {"player_snapshots": []}
        validator.profiler_mode = mode
        validator.profile_seconds = 30
        validator.count = 1
        validator.case_identity = f"linux-idle-1-{mode}"
        validator.generation = "current-generation"
        validator._write_results = mock.Mock()
        validator.server = mock.Mock()
        validator.server.process.pid = 123
        validator.server_dir = Path(root)
        validator.server.bds_identity_snapshot.return_value = {
            "status": "VERIFIED",
            "launch": {"pid": 123, "create_time": 1.0},
            "bds": {"pid": 456, "create_time": 2.0, "owned": True,
                    "binary_path": "bedrock_server", "source": "executable",
                    "binary_identity": {"device": 1, "inode": 1, "size": 1, "mtime_ns": 1}},
        }
        return validator

    def quality(self, mode="execution", players=True, **kwargs):
        raw = profile_fixture(
            "allocation" if mode == "allocation" else "default", end_ms=31000, **kwargs
        )
        return module.profile_quality(parse_sampler_data(raw), mode, 30, players)

    def test_modes_and_diagnostics(self):
        for mode in ("execution", "allocation"):
            with self.subTest(mode=mode):
                quality = self.quality(mode)
                self.assertEqual(quality["status"], "PASS")
                self.assertGreater(quality["observed"]["interval"], 0)
                self.assertTrue(quality["diagnostics"]["raw"])
        self.assertEqual(self.quality(diagnostics={})["status"], "UNVERIFIED")
        self.assertEqual(
            self.quality("allocation", diagnostics={})["status"], "UNVERIFIED"
        )

    def test_drops_and_incomplete_are_degraded_and_preserved(self):
        for allocation in (False, True):
            values = diagnostic_values(allocation=allocation)
            prefix = "Allocation" if allocation else "Execution"
            values[f"{prefix} samples dropped"] = "123"
            values[f"{prefix} data incomplete"] = "true"
            quality = self.quality(
                "allocation" if allocation else "execution", diagnostics=values
            )
            self.assertEqual(quality["status"], "DEGRADED")
            self.assertEqual(
                quality["diagnostics"]["drops"][f"{prefix} samples dropped"], 123
            )
            self.assertTrue(
                quality["diagnostics"]["incomplete_flags"][f"{prefix} data incomplete"]
            )

    def test_invalid_payload_semantics_and_player_window(self):
        for changes in (
            {"sampler_mode": 1},
            {"include_thread": False},
            {"thread_times": [0]},
            {"thread_times": [float("nan")]},
            {"start_ms": 20000},
            {"players": False},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.quality(**changes)["status"], "FAIL")
        values = diagnostic_values(allocation=False)
        values["Other native records dropped"] = "invalid"
        self.assertEqual(self.quality(diagnostics=values)["status"], "FAIL")
        values["Other native records dropped"] = "3"
        self.assertEqual(self.quality(diagnostics=values)["status"], "DEGRADED")

    def test_payload_parse_and_hash_and_parse_failure_preserve_url(self):
        for raw, expected in (
            (profile_fixture("default", end_ms=31000), "PASS"),
            (b"\x80", "FAIL"),
        ):
            with tempfile.TemporaryDirectory() as root:
                validator = self.validator(root)
                validator.player_window_valid = mock.Mock(return_value=True)
                with mock.patch.object(
                    module, "fetch_viewer_payload", return_value=raw
                ):
                    validator.collect_payload("https://spark.lucko.me/fixture")
                self.assertEqual(validator.result["quality"]["status"], expected)
                self.assertEqual(
                    validator.result["profile"]["raw_sha256"],
                    hashlib.sha256(raw).hexdigest(),
                )
                self.assertEqual(
                    (Path(root) / validator.result["profile"]["raw_path"]).read_bytes(),
                    raw,
                )
                self.assertEqual(
                    validator.result["spark_profile_viewer_url"],
                    "https://spark.lucko.me/fixture",
                )

    def test_current_list_snapshots_reject_historical_joins_and_generation(self):
        validator = self.validator(".")
        validator.server.command.side_effect = range(10, 100)
        validator.server.wait_command_output.return_value = [
            "[INFO] There are 1/30 players online:",
            "[INFO] TestBot",
        ]
        with mock.patch.object(module.time, "monotonic", side_effect=[1, 2, 3, 4]):
            validator.player_snapshot("before")
            validator.player_snapshot("during", 0)
            validator.player_snapshot("during", 0)
            validator.player_snapshot("after")
        self.assertTrue(validator.player_window_valid())
        validator.result["player_snapshots"][1]["generation"] = "previous-server"
        self.assertFalse(validator.player_window_valid())
        validator.server.wait_command_output.return_value = [
            "Player connected: TestBot",
            "There are 1/30 players online:",
            "SomeoneElse",
        ]
        self.assertFalse(validator.player_snapshot("after")["valid"])
        validator.server.wait_command_output.return_value = [
            "There are 1/30 players online:",
            "TestBot-01",
        ]
        self.assertFalse(validator.player_snapshot("after")["valid"])

    def test_exact_commands_both_modes_and_inherited_contract(self):
        for mode in ("execution", "allocation"):
            validator = self.validator(".", mode)
            validator.player_snapshot = mock.Mock()
            validator.collect_payload = mock.Mock()
            validator._viewer_url = mock.Mock(
                return_value="https://spark.lucko.me/fixture"
            )
            url, rss = validator.profile_execution()
            expected = "spark profiler start --timeout 30" + (
                " --alloc" if mode == "allocation" else ""
            )
            validator.server.command.assert_called_once_with(expected)
            self.assertEqual(url, "https://spark.lucko.me/fixture")
            self.assertIsInstance(rss, list)
            self.assertEqual(
                [call.args[0] for call in validator.player_snapshot.call_args_list],
                ["before", "after"],
            )

    def test_process_identity_continuity_counterexamples(self):
        validator = self.validator(".")
        identity = validator.server.bds_identity_snapshot.return_value
        baseline = [
            {"phase": phase, "valid": True, "generation": validator.generation,
             "server_pid": 123,
             "command_start_index": index, "sent_offset_seconds": index + 1,
             "completed_offset_seconds": index + 2,
             "process_identity_before": copy.deepcopy(identity),
             "process_identity_after": copy.deepcopy(identity)}
            for index, phase in enumerate(("before", "during", "during", "after"))
        ]
        validator.result["player_snapshots"] = baseline
        self.assertTrue(validator.player_window_valid())
        for part, field, value in (
            ("launch", "pid", 202), ("launch", "pid", None),
            ("launch", "create_time", 99.0), ("launch", "create_time", None),
            ("bds", "pid", 303), ("bds", "pid", None),
            ("bds", "create_time", 99.0), ("bds", "create_time", None),
            ("bds", "owned", False), ("bds", "binary_path", "other/bedrock_server"),
            ("bds", "binary_identity", None), ("bds", "binary_identity", {"inode": 2}),
            ("bds", "source", None),
        ):
            with self.subTest(part=part, field=field, value=value):
                validator.result["player_snapshots"] = copy.deepcopy(baseline)
                validator.result["player_snapshots"][1]["process_identity_after"][part][field] = value
                self.assertFalse(validator.player_window_valid())
        for identity_value in (None, {}, {"status": "UNVERIFIED"}):
            validator.result["player_snapshots"] = copy.deepcopy(baseline)
            validator.result["player_snapshots"][2]["process_identity_before"] = identity_value
            self.assertFalse(validator.player_window_valid())
        validator.result["player_snapshots"] = copy.deepcopy(baseline)
        for snapshot, pid in zip(validator.result["player_snapshots"], (101, 202, None, 303)):
            snapshot["server_pid"] = pid
        self.assertFalse(validator.player_window_valid())

    def test_identity_is_captured_around_each_current_list(self):
        validator = self.validator(".")
        events = []
        identity = validator.server.bds_identity_snapshot.return_value
        validator.server.bds_identity_snapshot.side_effect = lambda *_: (events.append("identity"), identity)[1]
        validator.server.command.side_effect = lambda *_: (events.append("list"), 1)[1]
        validator.server.wait_command_output.side_effect = lambda *_: (events.append("output"), ["There are 1/30 players online: TestBot"])[1]
        validator.player_snapshot("before")
        self.assertEqual(events, ["identity", "list", "output", "identity"])

    def test_two_fresh_probes_run_during_measurement(self):
        validator = self.validator(".")
        clock = [0.0]
        validator.player_snapshot = mock.Mock()
        validator.collect_payload = mock.Mock()
        validator.bds_rss_bytes = mock.Mock(return_value=100)
        validator._viewer_url = lambda *_: (
            "https://spark.lucko.me/fixture" if clock[0] >= 30 else None
        )
        with (
            mock.patch.object(module.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(
                module.time,
                "sleep",
                side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            ),
        ):
            url, rss = validator.profile_execution()
        self.assertTrue(url)
        self.assertEqual(len(rss), 30)
        self.assertEqual(
            [call.args[0] for call in validator.player_snapshot.call_args_list],
            ["before", "during", "during", "after"],
        )

    def test_workflow_matrix_provenance_and_mode_artifact_identity(self):
        path = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/cross-platform-spark-scenarios.yml"
        )
        workflow = yaml.safe_load(path.read_text())
        inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
        self.assertEqual(
            inputs["profiler_mode"]["options"], ["execution", "allocation", "all"]
        )
        self.assertEqual(inputs["profiler_mode"]["default"], "execution")
        self.assertEqual(
            inputs["bot_ref"]["default"], "a0a83240e9e8f12d4806ba31194fb1807d7b5dd1"
        )
        job = workflow["jobs"]["player-scenario"]
        matrix = job["strategy"]["matrix"]
        self.assertFalse(job["strategy"]["fail-fast"])
        self.assertEqual(
            len(matrix["platform"])
            * len(matrix["scenario"])
            * len(matrix["count"])
            * 2,
            16,
        )
        self.assertIn('["execution","allocation"]', matrix["profiler_mode"])
        self.assertEqual(
            {entry["os"] for entry in matrix["include"]},
            {"windows-2022", "ubuntu-24.04"},
        )
        self.assertIn(
            "matrix.platform == 'linux'", job["env"]["EXPECTED_SPARK_ARTIFACT_ID"]
        )
        self.assertEqual(
            workflow["env"]["EXPECTED_SPARK_RUN_ID"], "${{ inputs.spark_run_id }}"
        )
        self.assertEqual(
            workflow["env"]["EXPECTED_ENDSTONE_SHA"], "${{ inputs.endstone_sha }}"
        )
        for step in job["steps"]:
            if "run" in step:
                self.assertNotIn("${{", step["run"])
        artifact = next(
            step for step in job["steps"] if step["name"] == "Upload scenario evidence"
        )
        self.assertIn("${{ matrix.profiler_mode }}", artifact["with"]["name"])
        self.assertIn("*-raw.sparkprofile", artifact["with"]["path"])

    def test_quality_exit_happens_after_inherited_cleanup(self):
        for quality, expected in (
            ("PASS", 0),
            ("DEGRADED", 0),
            ("FAIL", 1),
            ("UNVERIFIED", 1),
        ):
            validator = self.validator(".")
            validator.result["quality"] = {"status": quality}
            with mock.patch.object(
                FleetSparkValidation, "execute", return_value=0
            ) as inherited:
                self.assertEqual(validator.execute(), expected)
                inherited.assert_called_once()

    def test_default_cli_and_constructor_compatibility(self):
        with mock.patch.object(module, "CrossPlatformFleetSparkValidation") as cls:
            cls.return_value.execute.return_value = 0
            cls.return_value.result = {}
            with mock.patch(
                "sys.argv",
                [
                    "test",
                    "--platform",
                    "linux",
                    "--bot",
                    "bot",
                    "--count",
                    "1",
                    "--scenario",
                    "idle",
                ],
            ):
                self.assertEqual(module.main(), 0)
            self.assertEqual(cls.call_args.args[-2:], (30, "execution"))
        self.assertEqual(
            module.CrossPlatformFleetSparkValidation.__init__.__defaults__,
            ("execution",),
        )

    def test_provenance_env_validation(self):
        valid = {
            "EXPECTED_SPARK_SHA": "a" * 40,
            "EXPECTED_ENDSTONE_SHA": "b" * 40,
            "BOT_REF": "c" * 40,
            "EXPECTED_SPARK_RUN_ID": "123",
            "EXPECTED_SPARK_ARTIFACT_ID": "456",
        }
        with mock.patch.dict(os.environ, valid, clear=True):
            module.validate_provenance_env()
        for key in valid:
            with (
                mock.patch.dict(os.environ, valid | {key: "$(unsafe)"}, clear=True),
                self.assertRaises(ValueError),
            ):
                module.validate_provenance_env()


if __name__ == "__main__":
    unittest.main()
