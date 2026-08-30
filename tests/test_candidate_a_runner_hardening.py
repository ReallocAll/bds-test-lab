from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from controller import candidate_a_blocked_benchmark as base
from controller import candidate_a_blocked_hardening as hardening


class FakeProcess:
    def __init__(self, *, executable: str | None = None, cmdline: list[str] | None = None) -> None:
        self.pid = 4242
        self._executable = executable or sys.executable
        self._cmdline = cmdline or [sys.executable, "-m", "endstone", "--server-path", "bedrock_server"]

    def exe(self) -> str:
        return self._executable

    def cmdline(self) -> list[str]:
        return list(self._cmdline)

    def create_time(self) -> float:
        return 1234.5


def exact_case_result(treatment: str) -> dict[str, object]:
    expected_spark = base.BASELINE_SHA if treatment.endswith("-B") else base.CANDIDATE_SHA
    return {
        "status": "PASS",
        "state": "completed",
        "performance": {
            "counter_windows": {
                "warmup": {
                    "configured_seconds": base.WARMUP_SECONDS,
                    "observed_seconds": float(base.WARMUP_SECONDS),
                },
                "measurement": {
                    "configured_seconds": base.MEASUREMENT_SECONDS,
                    "observed_seconds": float(base.MEASUREMENT_SECONDS),
                },
            }
        },
        "artifact_metadata": {
            "components": {
                "spark": {"sha": expected_spark},
                "endstone": {"sha": base.ENDSTONE_SHA},
            }
        },
        "protocol": {
            "endstone_artifact": {
                "sha": base.ENDSTONE_SHA,
                "run_id": 32992839821,
                "artifact": {"id": 9616075557, "name": "endstone-linux"},
            }
        },
    }


def write_exact_cases(block_dir: Path, block_index: int = 1) -> None:
    for treatment in base.block_schedule(block_index):
        case_dir = block_dir / treatment
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "candidate-a-blocked-result.json").write_text(
            json.dumps(exact_case_result(treatment)), encoding="utf-8"
        )


class CandidateARunnerHardeningTest(unittest.TestCase):
    def test_registered_stationary_scenario_sha_is_exact(self) -> None:
        scenario = Path(__file__).parents[1] / "scenarios" / "candidate-a-stationary.json"
        observed = hashlib.sha256(scenario.read_bytes()).hexdigest()
        self.assertEqual(observed, hardening.ACTUAL_SCENARIO_SHA256)
        self.assertEqual(base.BOT_SCENARIO_SHA256, observed)

    def test_endstone_root_requires_expected_python_module_process(self) -> None:
        identity = hardening.validate_endstone_root_process(FakeProcess())
        self.assertEqual(identity["validated_python_module"], "endstone")
        self.assertEqual(identity["pid"], 4242)

        with self.assertRaises(hardening.RunnerStateError):
            hardening.validate_endstone_root_process(
                FakeProcess(executable="/tmp/not-the-runner-python")
            )
        with self.assertRaises(hardening.RunnerStateError):
            hardening.validate_endstone_root_process(
                FakeProcess(cmdline=[sys.executable, "-m", "http.server"])
            )

    def test_bot_affinity_wraps_fleet_popen_with_psutil(self) -> None:
        case = object.__new__(hardening.HardenedCandidateABlockedCase)
        case.bot = SimpleNamespace(process=SimpleNamespace(pid=7777))
        case._load_cpus = [0, 1, 2]
        case.affinity = {}
        case._verify_all_affinity = mock.Mock()
        case.check = mock.Mock()
        case._write_results = mock.Mock()
        process = mock.Mock()
        process.pid = 7777

        with mock.patch.object(hardening.psutil, "Process", return_value=process) as constructor:
            case._set_bot_affinity()

        constructor.assert_called_once_with(7777)
        process.cpu_affinity.assert_called_once_with([0, 1, 2])
        case._verify_all_affinity.assert_called_once_with()

    def test_record_affinity_sample_queries_psutil_bot_process(self) -> None:
        case = object.__new__(hardening.HardenedCandidateABlockedCase)
        case.measurement_pid = 100
        case._controlled_cpu = 3
        case._available_cpus = [0, 1, 2, 3]
        case.bot = SimpleNamespace(process=SimpleNamespace(pid=200))
        case.affinity = {}
        case._affinity_samples = []
        case._affinity_phase = "warmup"
        case.result = {}

        bds_process = mock.Mock()
        bds_process.pid = 100
        bds_process.cpu_affinity.return_value = [3]
        bot_process = mock.Mock()
        bot_process.pid = 200
        bot_process.cpu_affinity.return_value = [0, 1, 2]

        def process_for_pid(pid: int) -> mock.Mock:
            if pid == 100:
                return bds_process
            if pid == 200:
                return bot_process
            raise AssertionError(f"unexpected pid {pid}")

        validated = {
            "controlled_cpu": 3,
            "bds_affinity": [3],
            "load_generator_affinity": [0, 1, 2],
            "available_cpus": [0, 1, 2, 3],
            "verified": True,
        }
        with (
            mock.patch.object(hardening.psutil, "Process", side_effect=process_for_pid),
            mock.patch.object(base, "_sched_affinity", return_value=[0, 1, 2]),
            mock.patch.object(base, "validate_affinity_snapshot", return_value=validated) as validator,
        ):
            case._record_affinity_sample(
                bds_tids={"100": [3]},
                load_tids={"200": [0, 1, 2]},
                controller_tids={"300": [0, 1, 2]},
            )

        validator.assert_called_once()
        self.assertEqual(case.affinity["load_generator_pid"], 200)
        self.assertEqual(case.affinity["load_generator_affinity"], [0, 1, 2])
        self.assertEqual(case.affinity["verification_count"], 1)

    def test_controller_affinity_restore_covers_original_and_new_tids(self) -> None:
        process_mask = [0, 1, 2, 3]
        snapshot = {
            "pid": os.getpid(),
            "process_affinity": process_mask,
            "tid_affinities": {"101": [0, 1, 2, 3], "102": [0, 1]},
        }
        current = {101: [0, 1], 103: [1]}

        def get_affinity(tid: int) -> list[int]:
            return list(current[tid])

        def set_affinity(tid: int, cpus: list[int]) -> None:
            current[tid] = list(cpus)

        with (
            mock.patch.object(base, "_linux_task_ids", return_value=[101, 103]),
            mock.patch.object(base, "_sched_affinity", side_effect=get_affinity),
            mock.patch.object(base, "_set_sched_affinity", side_effect=set_affinity),
        ):
            restored = hardening.restore_controller_affinity_state(snapshot)

        self.assertTrue(restored["restored"])
        self.assertEqual(current[101], [0, 1, 2, 3])
        self.assertEqual(current[103], process_mask)
        self.assertEqual(restored["new_tids_restored_to_process_mask"], [103])
        self.assertEqual(restored["exited_tids"], [102])

    def test_case_restoration_runs_even_when_case_launch_raises(self) -> None:
        topology = {"allowed_cpus": [0, 1, 2, 3], "cpu_count": 4}
        snapshot = {"pid": os.getpid(), "process_affinity": [0, 1, 2, 3], "tid_affinities": {"1": [0, 1, 2, 3]}}
        previous = hardening._EXPECTED_TOPOLOGY
        hardening._EXPECTED_TOPOLOGY = topology
        try:
            with (
                mock.patch.object(hardening, "runner_topology", return_value=topology),
                mock.patch.object(hardening, "capture_controller_affinity_state", return_value=snapshot),
                mock.patch.object(
                    hardening,
                    "restore_controller_affinity_state",
                    return_value={"restored": True},
                ) as restore,
                mock.patch.object(hardening, "_BASE_RUN_CASE", side_effect=RuntimeError("launch failed")),
                self.assertRaisesRegex(RuntimeError, "launch failed"),
            ):
                hardening.hardened_run_case(case_dir=Path("unused"))
            restore.assert_called_once_with(snapshot)
        finally:
            hardening._EXPECTED_TOPOLOGY = previous

    def test_topology_drift_is_rejected(self) -> None:
        expected = {"allowed_cpus": [0, 1, 2, 3], "cpu_count": 4}
        with (
            mock.patch.object(
                hardening,
                "runner_topology",
                return_value={"allowed_cpus": [0, 1, 2], "cpu_count": 4},
            ),
            self.assertRaises(hardening.RunnerStateError),
        ):
            hardening.require_topology(expected, phase="between-cases")

    def test_upload_gate_requires_exact_artifacts_and_completed_windows_for_all_four_cases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            block_dir = Path(temp) / "block-01"
            write_exact_cases(block_dir)
            gate = hardening.evaluate_upload_gate(block_dir, base.block_schedule(1))
            self.assertTrue(gate["safe"])

            treatment = base.block_schedule(1)[-1]
            path = block_dir / treatment / "candidate-a-blocked-result.json"
            result = json.loads(path.read_text(encoding="utf-8"))
            result["artifact_metadata"]["components"]["endstone"]["sha"] = "0" * 40
            path.write_text(json.dumps(result), encoding="utf-8")
            gate = hardening.evaluate_upload_gate(block_dir, base.block_schedule(1))
            self.assertFalse(gate["safe"])
            self.assertTrue(any(not check["safe"] for check in gate["checks"]))

        with tempfile.TemporaryDirectory() as temp:
            block_dir = Path(temp) / "block-01"
            write_exact_cases(block_dir)
            treatment = base.block_schedule(1)[0]
            path = block_dir / treatment / "candidate-a-blocked-result.json"
            result = json.loads(path.read_text(encoding="utf-8"))
            result["status"] = "FAIL"
            result["state"] = "failed"
            result["failed_stage"] = "bots-connect"
            path.write_text(json.dumps(result), encoding="utf-8")
            gate = hardening.evaluate_upload_gate(block_dir, base.block_schedule(1))
            self.assertFalse(gate["safe"])
            self.assertIn("did not complete successfully", gate["checks"][0]["reason"])

    def test_block_rewrites_manifest_and_only_creates_safe_upload_marker(self) -> None:
        topology = {"allowed_cpus": [0, 1, 2, 3], "cpu_count": 4}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            block_dir = root / "block-01"

            def fake_block(**_kwargs: object) -> int:
                block_dir.mkdir(parents=True, exist_ok=True)
                write_exact_cases(block_dir)
                (block_dir / "candidate-a-blocked-block.json").write_text(
                    json.dumps({"status": "PASS"}), encoding="utf-8"
                )
                return 0

            with (
                mock.patch.object(hardening, "runner_topology", return_value=topology),
                mock.patch.object(hardening, "_BASE_RUN_BLOCK", side_effect=fake_block),
            ):
                code = hardening.hardened_run_block(evidence_root=root, block_index=1)
            self.assertEqual(code, 0)
            self.assertTrue((root / hardening.UPLOAD_GATE_NAME).is_file())
            manifest = json.loads(
                (block_dir / "candidate-a-blocked-block.json").read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["runner_topology_contract"]["stable"])
            self.assertTrue(manifest["artifact_upload_gate"]["eligible"])
            self.assertEqual(manifest["artifact_upload_gate"]["block_exit_code"], 0)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            block_dir = root / "block-01"

            def fake_bad_block(**_kwargs: object) -> int:
                block_dir.mkdir(parents=True, exist_ok=True)
                write_exact_cases(block_dir)
                treatment = base.block_schedule(1)[0]
                path = block_dir / treatment / "candidate-a-blocked-result.json"
                result = json.loads(path.read_text(encoding="utf-8"))
                result["artifact_metadata"]["components"]["spark"]["sha"] = "0" * 40
                path.write_text(json.dumps(result), encoding="utf-8")
                return 1

            with (
                mock.patch.object(hardening, "runner_topology", return_value=topology),
                mock.patch.object(hardening, "_BASE_RUN_BLOCK", side_effect=fake_bad_block),
            ):
                code = hardening.hardened_run_block(evidence_root=root, block_index=1)
            self.assertEqual(code, 1)
            self.assertFalse((root / hardening.UPLOAD_GATE_NAME).exists())

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            block_dir = root / "block-01"

            def fake_failed_block_with_exact_artifacts(**_kwargs: object) -> int:
                block_dir.mkdir(parents=True, exist_ok=True)
                write_exact_cases(block_dir)
                return 1

            with (
                mock.patch.object(hardening, "runner_topology", return_value=topology),
                mock.patch.object(
                    hardening,
                    "_BASE_RUN_BLOCK",
                    side_effect=fake_failed_block_with_exact_artifacts,
                ),
            ):
                code = hardening.hardened_run_block(evidence_root=root, block_index=1)
            self.assertEqual(code, 1)
            self.assertFalse((root / hardening.UPLOAD_GATE_NAME).exists())
            manifest = json.loads(
                (block_dir / "candidate-a-blocked-block.json").read_text(encoding="utf-8")
            )
            self.assertFalse(manifest["artifact_upload_gate"]["eligible"])
            self.assertEqual(manifest["artifact_upload_gate"]["block_exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
