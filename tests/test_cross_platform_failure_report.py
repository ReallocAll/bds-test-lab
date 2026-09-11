from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml


class RestrictedFailureReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_path = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/cross-platform-spark-scenarios.yml"
        )
        cls.workflow = yaml.safe_load(cls.workflow_path.read_text(encoding="utf-8"))
        cls.steps = {
            step["name"]: step
            for step in cls.workflow["jobs"]["player-scenario"]["steps"]
        }
        cls.writer = cls.steps["Write restricted failure report"]
        cls.uploader = cls.steps["Upload restricted failure report"]
        cls.script = (
            cls.writer["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        )

    def render(self, environment):
        with tempfile.TemporaryDirectory() as temporary:
            env = {"RUNNER_TEMP": temporary, **environment}
            with mock.patch.dict(os.environ, env, clear=True):
                exec(compile(self.script, str(self.workflow_path), "exec"), {})  # noqa: S102 - exercise checked-in workflow
            path = Path(temporary) / "restricted-failure-report.json"
            return json.loads(path.read_text(encoding="utf-8"))

    def test_fallback_guards_and_full_gate(self):
        self.assertEqual(
            self.writer["if"],
            "${{ always() && steps.verify-bstats.outcome != 'success' }}",
        )
        self.assertEqual(
            self.uploader["if"],
            "${{ always() && steps.verify-bstats.outcome != 'success' && steps.restricted-failure-report.outcome == 'success' }}",
        )
        self.assertEqual(
            self.steps["Upload scenario evidence"]["if"],
            "${{ always() && steps.verify-bstats.outcome == 'success' }}",
        )
        self.assertEqual(
            self.uploader["with"]["path"],
            "${{ runner.temp }}/restricted-failure-report.json",
        )
        self.assertTrue(
            self.uploader["with"]["name"].startswith("restricted-failure-status-")
        )
        self.assertNotIn("continue-on-error", self.writer)
        self.assertNotIn(
            "continue-on-error", self.steps["Install validation dependencies"]
        )
        self.assertNotIn(
            "continue-on-error", self.steps["Require exact Spark artifact"]
        )

    def test_early_failures_and_missing_bstats_produce_report_without_checkout(self):
        for failed in (
            "CHECKOUT_LAB",
            "SETUP_PYTHON",
            "VALIDATE_INPUTS",
            "INSTALL_DEPENDENCIES",
            "RESOLVE_SPARK",
            "RUN_SCENARIO",
        ):
            for gate in ("failure", "skipped", ""):
                with self.subTest(failed=failed, gate=gate):
                    report = self.render(
                        {f"OUTCOME_{failed}": "failure", "OUTCOME_VERIFY_BSTATS": gate}
                    )
                    self.assertEqual(report["step_outcomes"][failed.lower()], "failure")
                    self.assertFalse(report["bstats_verified"])
                    self.assertEqual(
                        report["bstats_verification"],
                        "failed" if gate == "failure" else "unavailable",
                    )

    def test_whitelist_redacts_unknown_secrets_and_invalid_input(self):
        secret = "SECRET_SENTINEL_untrusted_arbitrary_value"
        environment = {
            "GH_TOKEN": secret,
            "REPO_PAT": secret,
            "Authorization": secret,
            "UNKNOWN_CONTROLLER_RESULT": secret,
            "OUTCOME_UNKNOWN_STEP": secret,
            "GITHUB_RUN_ID": "1234",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SHA": "a" * 40,
            "CASE_PLATFORM": "linux",
            "CASE_SCENARIO": "chunk-walk",
            "CASE_COUNT": "5",
            "PROFILER_MODE": "allocation",
            "EXPECTED_SPARK_SHA": "b" * 40,
            "EXPECTED_ENDSTONE_SHA": "c" * 40,
            "EXPECTED_SPARK_RUN_ID": "456",
            "EXPECTED_SPARK_ARTIFACT_ID": "789",
            "OUTCOME_INSTALL_DEPENDENCIES": "failure",
            "OUTCOME_VERIFY_BSTATS": "skipped",
        }
        report = self.render(environment)
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(report["expected_spark_artifact_id"], "789")
        self.assertEqual(report["expected_spark_sha"], "b" * 40)
        self.assertEqual(report["platform"], "linux")
        self.assertEqual(
            set(report),
            {
                "report_kind",
                "github_run_id",
                "github_run_attempt",
                "lab_sha",
                "platform",
                "scenario",
                "player_count",
                "profiler_mode",
                "expected_spark_sha",
                "expected_endstone_sha",
                "expected_spark_run_id",
                "expected_spark_artifact_id",
                "step_outcomes",
                "bstats_verified",
                "bstats_verification",
            },
        )
        invalid = {key: secret for key in environment}
        redacted = self.render(invalid)
        self.assertNotIn(secret, json.dumps(redacted))
        for key in set(report) - {
            "report_kind",
            "step_outcomes",
            "bstats_verified",
            "bstats_verification",
        }:
            self.assertIsNone(redacted[key])
        self.assertEqual(set(redacted["step_outcomes"].values()), {"unavailable"})

    def test_fixed_step_outcomes_and_dependency_free_inline_generation(self):
        step_ids = {step.get("id") for step in self.steps.values()}
        for value in self.writer["env"].values():
            identifier = value.removeprefix("${{ steps.").removesuffix(".outcome }}")
            self.assertIn(identifier, step_ids)
        self.assertNotIn("${{", self.writer["run"])
        self.assertNotIn("controller.", self.script)
        self.assertNotIn("read_text", self.script)
        self.assertNotIn("read_bytes", self.script)
        self.assertNotIn("os.environ.items", self.script)
        full = self.steps["Upload scenario evidence"]["with"]["path"]
        self.assertIn("bstats-config.toml", full)
        self.assertIn("*-raw.sparkprofile", full)


if __name__ == "__main__":
    unittest.main()
