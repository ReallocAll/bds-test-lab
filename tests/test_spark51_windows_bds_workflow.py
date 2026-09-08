from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "spark51-windows-bds-e2e.yml"
EXPECTED_SPARK_SHA = "6974323d5345d987d4ea7cc47067e20dc864384e"
EXPECTED_ENDSTONE_SHA = "46eff9f125f52eac76472d84339ead8fbf51fcd2"
SPARK_DIGEST = "sha256:" + "a" * 64
ENDSTONE_DIGEST = "sha256:" + "b" * 64


def _metadata_component(
    repository: str,
    sha: str,
    run_id: int,
    artifact_id: int,
    artifact_name: str,
    digest: str,
) -> dict[str, Any]:
    return {
        "component": repository.rsplit("/", 1)[-1],
        "repository": repository,
        "sha": sha,
        "run_id": run_id,
        "run_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        "artifact": {"id": artifact_id, "name": artifact_name, "digest": digest},
    }


def _valid_fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    spark = {
        "repository": "ReallocAll/spark",
        "sha": EXPECTED_SPARK_SHA,
        "run_id": 111,
        "run_url": "https://github.com/ReallocAll/spark/actions/runs/111",
        "artifact_id": 222,
        "artifact_name": "spark-windows",
        "artifact_digest": SPARK_DIGEST,
    }
    endstone = {
        "repository": "EndstoneMC/endstone",
        "sha": EXPECTED_ENDSTONE_SHA,
        "run_id": 33567087207,
        "run_url": "https://github.com/EndstoneMC/endstone/actions/runs/33567087207",
        "artifact_id": 9824065779,
        "artifact_name": "endstone-windows-cp313",
        "artifact_digest": ENDSTONE_DIGEST,
    }
    command = "spark profiler start --timeout 30"
    result: dict[str, Any] = {
        "status": "PASS",
        "state": "completed",
        "spark_sha": EXPECTED_SPARK_SHA,
        "endstone_sha": EXPECTED_ENDSTONE_SHA,
        "endstone_version": "0.11.11.dev396",
        "bds_full_version": "1.26.45.1",
        "lab_run_id": 424242,
        "artifact_provenance": {"spark": dict(spark), "endstone": dict(endstone)},
        "profiles": [],
        "plugin_reload_cycles": [],
        "shutdown_lifecycle_events": [
            {
                "phase_name": "bootstrap-provisioning",
                "wrapper_outcome": "exited",
                "wrapper_return_code": 0,
                "process_tree_verification": "clean",
                "forced": False,
                "acknowledgement_evidence": {"observed": True},
            },
            {
                "phase_name": "candidate-final-shutdown",
                "wrapper_outcome": "exited",
                "wrapper_return_code": 0,
                "process_tree_verification": "clean",
                "forced": False,
                "acknowledgement_evidence": {"observed": True},
            },
        ],
        "shutdown_evidence": {"graceful": True},
    }
    profile_data = (
        ("cpu-baseline", 0, 0, None, command, "cpu-baseline"),
        ("20-player-load", 20, 0, None, command, "20-player-load"),
        ("allocation-4096", 20, 0, 4096, f"{command} --alloc --interval 4096", "allocation-4096"),
        ("post-reload-cycle-3", 20, 3, None, command, "post-reload-cycle-3"),
    )
    for kind, players, reload_cycle, interval, profile_command, slug in profile_data:
        result["profiles"].append(
            {
                "kind": kind,
                "viewer_url": f"https://spark.lucko.me/{slug}",
                "player_count": players,
                "reload_cycle": reload_cycle,
                "allocation_interval": interval,
                "command": profile_command,
                "lab_run_id": 424242,
                "spark_sha": EXPECTED_SPARK_SHA,
                "endstone_sha": EXPECTED_ENDSTONE_SHA,
                "endstone_version": "0.11.11.dev396",
                "bds_full_version": "1.26.45.1",
                "spark_artifact_id": spark["artifact_id"],
                "spark_artifact_run_id": spark["run_id"],
                "spark_artifact_digest": spark["artifact_digest"],
                "endstone_artifact_id": endstone["artifact_id"],
                "endstone_artifact_run_id": endstone["run_id"],
                "endstone_artifact_digest": endstone["artifact_digest"],
            }
        )
    for cycle in range(1, 4):
        disable = "[01:49:22 INFO] [Spark] Disabling spark v0.5.3"
        enable = "[01:49:24 INFO] [Spark] Enabling spark v0.5.3"
        complete = "Reload complete."
        result["plugin_reload_cycles"].append(
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
                "reload_completion": complete,
                "spark_enabled": True,
                "spark_disable_evidence": disable,
                "spark_enable_evidence": enable,
                "reload_evidence_order": ["spark-disable", "spark-enable", "reload-complete"],
                "reload_evidence_lines": [disable, enable, complete],
                "bds_pid": 4242,
                "bds_create_time": 100.0,
                "before_bds_pid": 4242,
                "before_bds_create_time": 100.0,
                "same_bds_identity": True,
                "player_count": 20,
            }
        )
    metadata = {
        "platform": "windows",
        "components": {
            "spark": _metadata_component(
                "ReallocAll/spark", EXPECTED_SPARK_SHA, 111, 222, "spark-windows", SPARK_DIGEST
            ),
            "endstone": _metadata_component(
                "EndstoneMC/endstone",
                EXPECTED_ENDSTONE_SHA,
                33567087207,
                9824065779,
                "endstone-windows-cp313",
                ENDSTONE_DIGEST,
            ),
        },
    }
    env = {
        "EXPECTED_SPARK_SHA": EXPECTED_SPARK_SHA,
        "EXPECTED_ENDSTONE_SHA": EXPECTED_ENDSTONE_SHA,
        "EXPECTED_ENDSTONE_RUN_ID": "33567087207",
        "EXPECTED_ENDSTONE_ARTIFACT_ID": "9824065779",
        "EXPECTED_ENDSTONE_VERSION": "0.11.11.dev396",
        "EXPECTED_BDS_VERSION": "1.26.45.1",
        "LAB_RUN_ID": "424242",
    }
    return result, metadata, env


class Spark51WindowsBdsWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.data = yaml.safe_load(cls.text)
        steps = cls.data["jobs"]["windows-bds-e2e"]["steps"]
        run = next(step["run"] for step in steps if step.get("name") == "Independently validate fleet result contract")
        start = "# BEGIN SPARK51_RESULT_VALIDATOR\n"
        end = "\n# END SPARK51_RESULT_VALIDATOR"
        cls.validator_source = run.split(start, 1)[1].split(end, 1)[0]
        ast.parse(cls.validator_source)
        namespace: dict[str, Any] = {"__name__": "spark51_embedded_validator"}
        exec(compile(cls.validator_source, str(WORKFLOW), "exec"), namespace)
        cls.validate_result_contract = staticmethod(namespace["validate_result_contract"])

    def _validate_fixture(self, result: dict[str, Any], metadata: dict[str, Any], env: dict[str, str]) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result_path = root / "fleet-spark-result.json"
            metadata_path = root / "metadata.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            self.validate_result_contract(result_path, metadata_path, env)

    def test_yaml_has_dispatch_and_safe_same_repository_pull_request(self) -> None:
        triggers = self.data[True]
        self.assertIn("workflow_dispatch", triggers)
        self.assertIn("pull_request", triggers)
        self.assertIn(".github/spark51-candidate.txt", triggers["pull_request"]["paths"])
        self.assertIn("github.event_name == 'workflow_dispatch'", self.text)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", self.text)

    def test_candidate_and_provenance_are_exactly_pinned(self) -> None:
        self.assertIn('.github/spark51-candidate.txt', self.text)
        self.assertIn(EXPECTED_SPARK_SHA, self.text)
        self.assertIn(f'EXPECTED_ENDSTONE_SHA: "{EXPECTED_ENDSTONE_SHA}"', self.text)
        self.assertIn('EXPECTED_ENDSTONE_RUN_ID: "33567087207"', self.text)
        self.assertIn('EXPECTED_ENDSTONE_ARTIFACT_ID: "9824065779"', self.text)
        self.assertIn('EXPECTED_ENDSTONE_VERSION: "0.11.11.dev396"', self.text)
        self.assertIn('EXPECTED_BDS_VERSION: "1.26.45.1"', self.text)
        self.assertIn('BOT_REF: "f175e4a6551d1973628b5ca926a497e9962c1302"', self.text)

    def test_uses_exact_artifact_runner_and_never_compiles_spark(self) -> None:
        self.assertIn("controller.assert_spark_artifact", self.text)
        self.assertIn("--expected-sha $env:EXPECTED_SPARK_SHA", self.text)
        self.assertIn("controller.spark51_windows_bds_runner", self.text)
        self.assertIn("--platform windows", self.text)
        self.assertIn("--bot dist/bds-test-bot.exe", self.text)
        self.assertNotIn("spark-src", self.text)
        self.assertNotIn("cmake --build", self.text)
        self.assertNotIn("cmake -S", self.text)
        self.assertNotIn("clang", self.text.lower())

    def test_result_validator_covers_profiles_reloads_provenance_and_lifecycle(self) -> None:
        for required in (
            "fleet-spark-result.json",
            'result.get("status") == "PASS"',
            'result.get("state") == "completed"',
            '"cpu-baseline"',
            '"20-player-load"',
            '"allocation-4096"',
            '"post-reload-cycle-3"',
            'len(set(urls)) == 4',
            'len(reloads) == 3',
            'record.get("transport") == "stdin"',
            'record.get("command_published") is True',
            'record.get("dispatch_acknowledged") is False',
            'record.get("command_acknowledged") is False',
            'record.get("spark_enabled") is True',
            'record.get("same_bds_identity") is True',
            'record.get("player_count") == 20',
            'events[-1].get("phase_name") == "candidate-final-shutdown"',
            'event.get("process_tree_verification") == "clean"',
            'event.get("forced") is False',
            'acknowledgement.get("observed") is True',
            'artifact_digest',
            'reload_evidence_lines',
            'reload_completion',
            'sha256:[0-9a-f]{64}',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.text)

    def test_embedded_validator_accepts_minimal_valid_fixture(self) -> None:
        self._validate_fixture(*_valid_fixture())

    def test_embedded_validator_accepts_legacy_endstone_reload_evidence(self) -> None:
        result, metadata, env = _valid_fixture()
        for record in result["plugin_reload_cycles"]:
            record["spark_disable_evidence"] = "[Endstone] Disabling spark"
            record["spark_enable_evidence"] = "[Endstone] Enabling spark"
            record["reload_evidence_lines"] = [
                record["spark_disable_evidence"],
                record["spark_enable_evidence"],
                record["reload_completion"],
            ]
        self._validate_fixture(result, metadata, env)

    def test_embedded_validator_rejects_mutated_profile_command(self) -> None:
        result, metadata, env = _valid_fixture()
        result["profiles"][2]["command"] = "spark profiler start --timeout 30"
        with self.assertRaises(SystemExit):
            self._validate_fixture(result, metadata, env)

    def test_embedded_validator_rejects_missing_reversed_and_fake_reload_evidence(self) -> None:
        mutations = {}

        result, metadata, env = _valid_fixture()
        result["plugin_reload_cycles"][0].pop("spark_enable_evidence")
        mutations["missing"] = (result, metadata, env)

        result, metadata, env = _valid_fixture()
        record = result["plugin_reload_cycles"][1]
        record["reload_evidence_lines"] = [record["spark_enable_evidence"], record["spark_disable_evidence"], record["reload_completion"]]
        mutations["reversed"] = (result, metadata, env)

        result, metadata, env = _valid_fixture()
        result["plugin_reload_cycles"][2]["spark_disable_evidence"] = "Disabling spark failed"
        mutations["fake"] = (result, metadata, env)

        for name, mutation in mutations.items():
            with self.subTest(mutation=name), self.assertRaises(SystemExit):
                self._validate_fixture(*mutation)

    def test_embedded_validator_rejects_file_transport_or_dispatch_ack_claim(self) -> None:
        for field, value in (
            ("transport", "file-trigger"),
            ("command_published", False),
            ("dispatch_acknowledged", True),
            ("command_acknowledged", True),
        ):
            result, metadata, env = _valid_fixture()
            result["plugin_reload_cycles"][0][field] = value
            with self.subTest(field=field), self.assertRaises(SystemExit):
                self._validate_fixture(result, metadata, env)

    def test_embedded_validator_rejects_non_endstone_reload_evidence(self) -> None:
        for field, line_index, evidence in (
            ("spark_disable_evidence", 0, "[Worker] Disabling spark"),
            ("spark_enable_evidence", 1, "[Worker] Enabling spark"),
        ):
            result, metadata, env = _valid_fixture()
            record = result["plugin_reload_cycles"][0]
            record[field] = evidence
            record["reload_evidence_lines"][line_index] = evidence
            with self.subTest(field=field), self.assertRaises(SystemExit):
                self._validate_fixture(result, metadata, env)

    def test_embedded_validator_rejects_malformed_and_mismatched_digests(self) -> None:
        result, metadata, env = _valid_fixture()
        metadata["components"]["spark"]["artifact"]["digest"] = "sha256:" + "A" * 64
        with self.assertRaises(SystemExit):
            self._validate_fixture(result, metadata, env)

        result, metadata, env = _valid_fixture()
        metadata["components"]["spark"]["artifact"]["digest"] = "sha256:" + "c" * 64
        with self.assertRaises(SystemExit):
            self._validate_fixture(result, metadata, env)

    def test_fixture_summary_and_evidence_upload_are_always_present(self) -> None:
        self.assertIn("fixtures/endstone-ci-lifecycle-control", self.text)
        self.assertIn("name: Add exact-candidate summary", self.text)
        self.assertIn("name: Upload exact-candidate evidence", self.text)
        self.assertGreaterEqual(self.text.count("if: always()"), 3)
        self.assertIn("actions/upload-artifact@v7", self.text)
        self.assertIn("fleet-spark-result.json", self.text)
        self.assertIn("metadata.json", self.text)
        self.assertIn("work/windows/bedrock_server/crash_reports/**/*.txt", self.text)
        self.assertIn("crash_reports/**/*.txt", self.text)
        self.assertNotIn("            work/windows/bedrock_server/crash_reports/**\n", self.text)
        self.assertNotIn("            crash_reports/**\n", self.text)
        self.assertIn("if-no-files-found: warn", self.text)
        self.assertIn("persist-credentials: false", self.text)


if __name__ == "__main__":
    unittest.main()
