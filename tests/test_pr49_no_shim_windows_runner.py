from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from controller import pr49_no_shim_windows_runner as runner


class Pr49NoShimWindowsRunnerTest(unittest.TestCase):
    def test_validate_no_shim_run_requires_exact_workflow_and_sha(self) -> None:
        sha = "a" * 40
        run = {
            "id": 123,
            "head_sha": sha,
            "conclusion": "success",
            "name": runner.NO_SHIM_WORKFLOW,
        }
        runner._validate_no_shim_run(run, expected_sha=sha, expected_run_id=123)

        bad = dict(run, name="Build")
        with self.assertRaisesRegex(RuntimeError, "workflow mismatch"):
            runner._validate_no_shim_run(bad, expected_sha=sha, expected_run_id=123)

    def test_select_no_shim_artifact_requires_exact_id_and_name(self) -> None:
        sha = "b" * 40
        artifact = {
            "id": 456,
            "name": f"spark-windows-no-shim-{sha}",
            "expired": False,
        }
        selected = runner._select_no_shim_artifact(
            [artifact], expected_sha=sha, expected_artifact_id=456
        )
        self.assertIs(selected, artifact)

        wrong_name = dict(artifact, name="spark-windows-build")
        with self.assertRaisesRegex(RuntimeError, "artifact name mismatch"):
            runner._select_no_shim_artifact(
                [wrong_name], expected_sha=sha, expected_artifact_id=456
            )

    def test_assert_no_shim_payload_accepts_verified_single_dll_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "endstone_spark.dll").write_bytes(b"dll")
            (root / "endstone_spark-dependents.txt").write_text(
                "KERNEL32.dll\nVCRUNTIME140.dll\n",
                encoding="utf-8",
            )
            (root / "no-shim-targets.txt").write_text(
                "endstone_spark\nprofiler_export_test\n",
                encoding="utf-8",
            )
            runner._assert_no_shim_payload(root)

    def test_assert_no_shim_payload_rejects_shim_file_or_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "endstone_spark-dependents.txt").write_text("KERNEL32.dll\n", encoding="utf-8")
            (root / "no-shim-targets.txt").write_text("endstone_spark\n", encoding="utf-8")
            (root / "spark_allocation_shim.dll").write_bytes(b"shim")
            with self.assertRaisesRegex(RuntimeError, "unexpectedly contains"):
                runner._assert_no_shim_payload(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "endstone_spark-dependents.txt").write_text(
                "spark_allocation_shim.dll\n",
                encoding="utf-8",
            )
            (root / "no-shim-targets.txt").write_text("endstone_spark\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "dependency evidence"):
                runner._assert_no_shim_payload(root)

    def test_resolve_exact_no_shim_spark_pins_run_and_artifact(self) -> None:
        sha = "c" * 40
        run = {
            "id": 789,
            "head_sha": sha,
            "conclusion": "success",
            "name": runner.NO_SHIM_WORKFLOW,
        }
        artifact = {
            "id": 987,
            "name": f"spark-windows-no-shim-{sha}",
            "expired": False,
        }

        def fake_get(path: str):
            if path.endswith("/artifacts?per_page=100"):
                return {"artifacts": [artifact]}
            return run

        with mock.patch.dict(
            os.environ,
            {
                "EXPECTED_SPARK_SHA": sha,
                "EXPECTED_SPARK_RUN_ID": "789",
                "EXPECTED_SPARK_ARTIFACT_ID": "987",
            },
            clear=False,
        ), mock.patch.object(runner.artifact_provider, "_get_json", side_effect=fake_get):
            observed_run, observed_artifact = runner._resolve_exact_no_shim_spark()

        self.assertIs(observed_run, run)
        self.assertIs(observed_artifact, artifact)

    def test_live_only_mode_requires_retained_start_and_info_semantics(self) -> None:
        runner._validate_live_only_start_output(
            [
                "Retained Allocation Profiler is now running! (async)",
                "The result will contain only sampled allocations still live when profiling stops.",
            ]
        )
        runner._validate_live_only_info_output(
            [
                "Retained Allocation Profiler is already running!",
                "So far it has profiled for 5s (12 tracked sampled allocations still live process-wide, 64 KiB estimated).",
                "Process-wide tracked lifecycle: 8 freed, 12 still live (64 KiB).",
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "retained mode"):
            runner._validate_live_only_start_output(["Allocation Profiler is now running!"])
        with self.assertRaisesRegex(RuntimeError, "retained semantics"):
            runner._validate_live_only_info_output(["Allocation Profiler is already running!"])


    def test_live_only_post_stop_accepts_idle_or_resumed_background(self) -> None:
        self.assertTrue(runner._validate_live_only_post_stop_output(["The profiler isn't running."]))
        self.assertTrue(
            runner._validate_live_only_post_stop_output(
                [
                    "Profiler is already running!",
                    "It was started automatically when spark enabled and has been running in the background for 0s.",
                ]
            )
        )
        self.assertFalse(
            runner._validate_live_only_post_stop_output(["Results are still being finalized, please wait..."])
        )
        with self.assertRaisesRegex(RuntimeError, "retained session"):
            runner._validate_live_only_post_stop_output(["Retained Allocation Profiler is already running!"])
        with self.assertRaisesRegex(RuntimeError, "unexpected profiler state"):
            runner._validate_live_only_post_stop_output(["Profiler is already running!"])


if __name__ == "__main__":
    unittest.main()
