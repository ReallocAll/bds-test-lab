from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "spark51-post-reload-diagnostic.yml"
EXACT_WORKFLOW = ROOT / ".github" / "workflows" / "spark51-windows-bds-e2e.yml"
UPSTREAM_SHA = "6293f9d41672adc2b27ffa4e38eb9c444c6c0620"
SPARK_SHA = "6911c16ab7334410b13691a47895479a20d232de"
ARTIFACT_ID = "10056904202"
ARTIFACT_DIGEST = "sha256:2d6e7c2bd82b110ffc2f5d347624e42d0e96301507be2ee42aab9f75cb9e41f6"
SPARK_RUN_ID = "34228410211"


class Spark51PostReloadWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.data = yaml.safe_load(cls.text)
        cls.triggers = cls.data.get(True, cls.data.get("on", {}))
        cls.job = cls.data["jobs"]["windows-post-reload-diagnostic"]
        cls.steps = cls.job["steps"]

    def test_workflow_is_dispatch_only_single_attempt_and_shared_host(self) -> None:
        self.assertEqual(set(self.triggers), {"workflow_dispatch"})
        inputs = self.triggers["workflow_dispatch"]["inputs"]
        expected = {
            "upstream_lab_sha": UPSTREAM_SHA,
            "upstream_run_id": "34229468130",
            "upstream_run_attempt": "3",
            "spark_candidate_sha": SPARK_SHA,
            "spark_artifact_id": ARTIFACT_ID,
            "spark_artifact_digest": ARTIFACT_DIGEST,
        }
        for name, value in expected.items():
            self.assertTrue(inputs[name]["required"], name)
            self.assertEqual(inputs[name]["default"], value, name)
        self.assertTrue(inputs["diagnostic_lab_sha"]["required"])
        self.assertEqual(self.job["if"], "github.run_attempt == 1")
        self.assertEqual(self.job["timeout-minutes"], 30)
        self.assertEqual(self.data["concurrency"]["group"], "spark51-windows-bds-host")
        self.assertFalse(self.data["concurrency"]["cancel-in-progress"])

    def test_trusted_precheckout_validation_checks_fixed_provenance(self) -> None:
        validate = next(step for step in self.steps if step["name"] == "Validate trusted dispatch provenance before checkout")
        self.assertEqual(validate["env"]["GH_TOKEN"], "${{ secrets.REPO_PAT }}")
        script = validate["run"]
        for required in (
            "workflow_dispatch",
            "ReallocAll/bds-test-lab",
            "ReallocAll/spark",
            ".github/workflows/spark51-windows-bds-e2e.yml",
            UPSTREAM_SHA,
            SPARK_SHA,
            ARTIFACT_ID,
            ARTIFACT_DIGEST,
            SPARK_RUN_ID,
            "run_attempt",
            "head_repository.full_name",
            "fork",
            "actions/runs/$runId",
            "contents/.github/spark51-candidate.txt?ref=$upstreamSha",
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)
        self.assertNotIn("${{ inputs.", script)
        checkout = next(step for step in self.steps if step["name"] == "Checkout exact diagnostic lab revision")
        self.assertEqual(checkout["with"]["persist-credentials"], False)
        self.assertEqual(checkout["with"]["ref"], "${{ env.INPUT_DIAGNOSTIC_LAB_SHA }}")

    def test_artifact_token_is_narrow_and_result_evidence_is_unconditional(self) -> None:
        self.assertNotIn("GH_TOKEN", self.data.get("env", {}))
        names = [step.get("name") for step in self.steps]
        self.assertIn("Validate diagnostic result contract", names)
        self.assertIn("Upload bounded diagnostic evidence", names)
        validate = next(step for step in self.steps if step["name"] == "Validate diagnostic result contract")
        upload = next(step for step in self.steps if step["name"] == "Upload bounded diagnostic evidence")
        run = next(step for step in self.steps if step["name"] == "Run post-reload diagnostic stress")
        artifact = next(step for step in self.steps if step["name"] == "Require exact Spark Actions artifact")
        self.assertEqual(validate.get("if"), "always()")
        self.assertEqual(upload.get("if"), "always()")
        self.assertEqual(upload["uses"], "actions/upload-artifact@v7")
        self.assertEqual(upload["with"]["if-no-files-found"], "warn")
        self.assertEqual(artifact["env"]["GH_TOKEN"], "${{ secrets.REPO_PAT }}")
        self.assertEqual(run["env"]["GH_TOKEN"], "${{ secrets.REPO_PAT }}")
        for required in ("metadata.json", "fleet-spark-result.json", "failure-diagnostics.txt", "combined-health-capture-windows/*.json"):
            self.assertIn(required, upload["with"]["path"])

    def test_supervisor_has_two_suspended_contained_jobs_and_strict_verdict(self) -> None:
        run = next(step for step in self.steps if step["name"] == "Run post-reload diagnostic stress")
        script = run["run"]
        for required in (
            "Add-Type -TypeDefinition",
            "CreateJobObject",
            "KillOnJobClose",
            "BreakawayOk",
            "CreateSuspended",
            "AssignVerified",
            "ResumeThread",
            "controllerJob",
            "observerJob",
            "MemoryMappedFile",
            "EventWaitHandle",
            "1380L * Billion",
            "1480L * Billion",
            "1485L * Billion",
            "1495L * Billion",
            "1500L * Billion",
            "observer_death_confirmed",
            "controller_job_empty",
            "observer_job_empty",
            "forced_observer_termination",
            "GH_TOKEN",
            "REPO_PAT",
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)
        self.assertIn("StartChild(python, controllerArgs", script)
        self.assertIn("StartChild(python, observerArgs", script)

    def _run_embedded_supervisor(self, mode: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        if os.name != "nt":
            self.skipTest("embedded Windows supervisor oracle requires Windows")
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh is unavailable")
        fake_module = textwrap.dedent(
            r'''
            import json
            import mmap
            import os
            import struct
            import sys
            import time
            from pathlib import Path

            CONTROL_SIZE = 4096
            SUPERVISOR_STATUS = 12
            CONTROLLER_STATUS = 16
            OBSERVER_STATUS = 20
            FAILURE = 3
            DEATH_CONFIRMED = 4
            CONTROLLER_RUNNING = 2
            CONTROLLER_STOP_REQUEST = 3
            CONTROLLER_FAILURE = 4
            CONTROLLER_COMPLETE = 5
            OBSERVER_READY = 2
            OBSERVER_FAILURE = 3
            OBSERVER_STOPPED = 5

            def main():
                root = Path.cwd()
                control_path = Path(os.environ["SPARK51_CONTROL_PATH"])
                command_path = root / "observer-command.json"
                mode = os.environ.get("SPARK51_TEST_MODE", "success")
                with control_path.open("r+b") as output, mmap.mmap(output.fileno(), CONTROL_SIZE, access=mmap.ACCESS_WRITE) as view:
                    def read_int(offset):
                        return struct.unpack_from("<i", view, offset)[0]

                    def write_int(offset, value):
                        struct.pack_into("<i", view, offset, value)
                        view.flush()

                    if "--observer" in sys.argv:
                        if mode == "observer-crash":
                            write_int(OBSERVER_STATUS, OBSERVER_FAILURE)
                            return 7
                        write_int(OBSERVER_STATUS, OBSERVER_READY)
                        while True:
                            try:
                                command = json.loads(command_path.read_text(encoding="utf-8"))
                            except (FileNotFoundError, json.JSONDecodeError):
                                command = None
                            if isinstance(command, dict) and command.get("operation") == "stop":
                                write_int(OBSERVER_STATUS, OBSERVER_STOPPED)
                                return 0
                            if read_int(SUPERVISOR_STATUS) in (FAILURE, 6, 7):
                                return 8
                            time.sleep(0.01)

                    write_int(CONTROLLER_STATUS, CONTROLLER_RUNNING)
                    if mode == "controller-crash":
                        write_int(CONTROLLER_STATUS, CONTROLLER_FAILURE)
                        return 9
                    write_int(CONTROLLER_STATUS, CONTROLLER_STOP_REQUEST)
                    command_path.write_text(json.dumps({"sequence": 1, "operation": "stop"}), encoding="utf-8")
                    while True:
                        supervisor_status = read_int(SUPERVISOR_STATUS)
                        if supervisor_status == DEATH_CONFIRMED:
                            write_int(CONTROLLER_STATUS, CONTROLLER_COMPLETE)
                            return 0
                        if supervisor_status in (FAILURE, 6, 7):
                            return 10
                        time.sleep(0.01)

            if __name__ == "__main__":
                raise SystemExit(main())
            '''
        )
        command = textwrap.dedent(
            r'''
            $ErrorActionPreference = "Stop"
            $workflow = Get-Content -Raw -LiteralPath $env:SPARK51_WORKFLOW
            $match = [regex]::Match($workflow, '\$supervisorSource = @''(?<source>.*?)\r?\n\s*''@', [Text.RegularExpressions.RegexOptions]::Singleline)
            if (!$match.Success) { throw "embedded supervisor source was not found" }
            $source = $match.Groups['source'].Value -replace '(?m)^\s{10}', ''
            Add-Type -TypeDefinition $source -Language CSharp -ErrorAction Stop
            $exitCode = [Spark51WindowsContainmentSupervisor]::Run($env:SPARK51_PYTHON, $env:SPARK51_ROOT, "synthetic-child")
            Write-Output ("SUPERVISOR_EXIT=" + $exitCode)
            exit $exitCode
            '''
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "controller"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "spark51_post_reload_diagnostic.py").write_text(fake_module, encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "SPARK51_TEST_MODE": mode,
                    "SPARK51_WORKFLOW": str(WORKFLOW),
                    "SPARK51_PYTHON": sys.executable,
                    "SPARK51_ROOT": str(root),
                    "PYTHONPATH": str(root),
                }
            )
            environment.pop("SPARK51_TEST_FAIL_SECOND_START", None)
            if mode == "second-start-failure":
                environment["SPARK51_TEST_FAIL_SECOND_START"] = "1"
            completed = subprocess.run(
                [pwsh, "-NoProfile", "-Command", command],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            verdict_path = root / "supervisor-verdict.json"
            self.assertTrue(verdict_path.is_file(), completed.stdout + completed.stderr)
            return completed, json.loads(verdict_path.read_text(encoding="utf-8"))

    def test_embedded_supervisor_executable_success_oracle_with_short_children(self) -> None:
        completed, verdict = self._run_embedded_supervisor("success")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(verdict["status"], "PASS")
        self.assertEqual(verdict["controller_exit_code"], 0)
        self.assertEqual(verdict["observer_exit_code"], 0)
        self.assertTrue(verdict["controller_job_empty"])
        self.assertTrue(verdict["observer_job_empty"])
        self.assertTrue(verdict["observer_death_confirmed"])
        self.assertFalse(verdict["forced_observer_termination"])

    def test_embedded_supervisor_executable_failure_oracle_with_short_children(self) -> None:
        completed, verdict = self._run_embedded_supervisor("observer-crash")
        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(verdict["status"], "FAIL")
        self.assertTrue(verdict["controller_job_empty"])
        self.assertTrue(verdict["observer_job_empty"])
        self.assertFalse(verdict["observer_death_confirmed"])

    def test_embedded_supervisor_executable_partial_startup_oracle(self) -> None:
        started = time.monotonic()
        completed, verdict = self._run_embedded_supervisor("second-start-failure")
        self.assertLess(time.monotonic() - started, 10)
        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(verdict["status"], "FAIL")
        self.assertEqual(verdict["state"], "failed")
        self.assertTrue(verdict["controller_job_empty"])
        self.assertTrue(verdict["observer_job_empty"])
        self.assertTrue(verdict["controller_stopped"])
        self.assertTrue(verdict["observer_stopped"])
        self.assertIn("injected second StartChild failure", verdict["reason"])

    def test_supervised_profile_arms_before_command_and_repetition_deadline_is_published(self) -> None:
        source = (ROOT / "controller" / "spark51_post_reload_diagnostic.py").read_text(encoding="utf-8")
        profile = source[source.index("    def _post_reload_profile"):source.index("    def _validate_workload_success")]
        self.assertLess(profile.index("self._observer.arm"), profile.index("self._dispatch(POST_RELOAD_PROFILE_COMMAND"))
        self.assertIn("admit_repetition", source)
        self.assertIn("self._control.set_repetition", source)
        self.assertIn("shortened post-reload profile", source)

    def test_normal_exact_e2e_remains_diagnostic_free_and_uses_shared_host(self) -> None:
        exact = EXACT_WORKFLOW.read_text(encoding="utf-8")
        exact_data = yaml.safe_load(exact)
        self.assertIn("controller.spark51_windows_bds_runner", exact)
        self.assertNotIn("spark51_post_reload_diagnostic", exact)
        self.assertNotIn("post-reload-diagnostic", exact)
        self.assertIn("group: spark51-windows-bds-host", exact)
        self.assertIn("cancel-in-progress: false", exact)
        self.assertEqual(exact_data["concurrency"]["group"], "spark51-windows-bds-host")


if __name__ == "__main__":
    unittest.main()
