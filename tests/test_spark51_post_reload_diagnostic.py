from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import patch

from controller import spark51_post_reload_diagnostic as diagnostic


def _artifact(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": diagnostic.EXPECTED_SPARK_ARTIFACT_ID,
        "name": "spark-windows-34228410211",
        "expired": False,
        "digest": diagnostic.EXPECTED_SPARK_ARTIFACT_DIGEST,
        "workflow_run": {
            "id": diagnostic.EXPECTED_SPARK_ARTIFACT_RUN_ID,
            "head_sha": diagnostic.SPARK_CANDIDATE_SHA,
        },
    }
    value.update(overrides)
    return value


def _run(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": diagnostic.EXPECTED_SPARK_ARTIFACT_RUN_ID,
        "status": "completed",
        "conclusion": "success",
        "path": diagnostic.EXPECTED_SPARK_WORKFLOW_PATH,
        "head_sha": diagnostic.SPARK_CANDIDATE_SHA,
        "repository": {"full_name": diagnostic.EXPECTED_SPARK_REPOSITORY},
        "head_repository": {"full_name": diagnostic.EXPECTED_SPARK_REPOSITORY},
    }
    value.update(overrides)
    return value


def _mapping(lifetime: int = 1, application: int = 1, plugin: int = 1) -> dict[str, object]:
    return {
        "status": "available",
        "mapping_name": "Local\\spark51",
        "mapping_lifetime": lifetime,
        "schema_version": 2,
        "contexts": [
            {"context": "ApplicationTick", "status": "available", "transition_sequence": application, "session_generation": "g"},
            {"context": "PluginTick", "status": "available", "transition_sequence": plugin, "session_generation": "g"},
        ],
    }


class DeadlineAndControlTest(unittest.TestCase):
    def test_strict_aggregate_repetition_boundary_and_shared_publication(self) -> None:
        self.assertEqual(diagnostic.admit_repetition(0, 90.001), (90.0, 90.0))
        with self.assertRaises(diagnostic.DeadlineExceeded):
            diagnostic.admit_repetition(0, 90.0)
        with self.assertRaises(diagnostic.DeadlineExceeded):
            diagnostic.admit_repetition(1, 90.5)
        self.assertEqual(diagnostic.next_observer_tick(10.0, 10.5), 11.5)
        with self.assertRaises(diagnostic.DeadlineExceeded):
            diagnostic.next_observer_tick(10.0, 11.01)

    def test_fixed_shared_control_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = diagnostic.SharedControlBlock(Path(directory) / "control.bin", create=True)
            try:
                control.initialize(origin_ns=100, work_deadline_ns=200, hard_deadline_ns=300, emergency_ns=280, observer_zero_ns=285, controller_zero_ns=295)
                control.set_repetition(7, 190)
                control.set_observer_status(diagnostic.CONTROL_OBSERVER_FAILURE)
                self.assertEqual(control.deadlines()["work_deadline_ns"], 200)
                self.assertEqual(control.repetition_deadline_ns(), 190)
                self.assertEqual(control.statuses()["observer"], diagnostic.CONTROL_OBSERVER_FAILURE)
            finally:
                control.close()

    def test_no_catch_up_burst_and_legacy_observer_path_is_absent(self) -> None:
        source = Path(diagnostic.__file__).read_text(encoding="utf-8")
        self.assertNotIn("multiprocessing.Pipe", source)
        self.assertNotIn("_observer_process_main", source)
        self.assertNotIn("sample_once", source)
        self.assertNotIn("class PostReloadObserver", source)
        self.assertIn("run_autonomous_observer", source)


class ObserverClientCleanupTest(unittest.TestCase):
    def _client(self, directory: Path) -> tuple[diagnostic.SupervisorObserverClient, diagnostic.SharedControlBlock]:
        control = diagnostic.SharedControlBlock(directory / "control.bin", create=True)
        control.initialize(origin_ns=0, work_deadline_ns=100_000_000_000, hard_deadline_ns=200_000_000_000, emergency_ns=150_000_000_000, observer_zero_ns=160_000_000_000, controller_zero_ns=190_000_000_000)
        return diagnostic.SupervisorObserverClient(directory, directory / "control.bin", directory / "captures", directory / "bds.log"), control

    def test_cleanup_waits_for_supervisor_death_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client, control = self._client(root)
            try:
                (root / "observer-state.json").write_text(json.dumps({"status": "stopped", "process": {"alive": False, "forced_termination": False}}), encoding="utf-8")
                self.assertFalse(client.stop(timeout=0.05))
                control.set_observer_status(diagnostic.CONTROL_OBSERVER_STOPPED)
                control.set_supervisor_status(diagnostic.CONTROL_SUPERVISOR_OBSERVER_DEATH_CONFIRMED)
                self.assertTrue(client.stop(timeout=0.05))
            finally:
                control.close()

    def test_forced_observer_termination_never_passes_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client, control = self._client(root)
            try:
                (root / "observer-state.json").write_text(json.dumps({"status": "stopped", "process": {"alive": False, "forced_termination": True}}), encoding="utf-8")
                control.set_observer_status(diagnostic.CONTROL_OBSERVER_STOPPED)
                control.set_supervisor_status(diagnostic.CONTROL_SUPERVISOR_FORCED_OBSERVER_FAILURE)
                self.assertFalse(client.stop(timeout=0.05))
            finally:
                control.close()


class ExactArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        import providers.artifact_provider as provider

        self.provider = provider
        diagnostic._strict_spark_discover._original = lambda *_args: (_run(), _artifact())  # type: ignore[attr-defined]
        diagnostic._strict_download_artifact._original = lambda *_args: Path("unused")  # type: ignore[attr-defined]

    def test_large_exact_artifact_and_referenced_success_run_are_required(self) -> None:
        responses = {
            f"/repos/{diagnostic.EXPECTED_SPARK_REPOSITORY}/actions/artifacts/{diagnostic.EXPECTED_SPARK_ARTIFACT_ID}": _artifact(),
            f"/repos/{diagnostic.EXPECTED_SPARK_REPOSITORY}/actions/runs/{diagnostic.EXPECTED_SPARK_ARTIFACT_RUN_ID}": _run(),
        }
        with patch.object(self.provider, "_get_json", side_effect=responses.__getitem__):
            run, artifact = diagnostic._strict_spark_discover("spark", "windows", diagnostic.SPARK_CANDIDATE_SHA)
        self.assertEqual(run["id"], diagnostic.EXPECTED_SPARK_ARTIFACT_RUN_ID)
        self.assertEqual(artifact["id"], diagnostic.EXPECTED_SPARK_ARTIFACT_ID)

    def test_changed_id_digest_candidate_or_workflow_blocks_before_download(self) -> None:
        cases = (
            {"id": diagnostic.EXPECTED_SPARK_ARTIFACT_ID + 1},
            {"digest": "sha256:" + "0" * 64},
            {"workflow_run": {"id": diagnostic.EXPECTED_SPARK_ARTIFACT_RUN_ID, "head_sha": "0" * 40}},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                artifact = _artifact(**changed)
                responses = {
                    f"/repos/{diagnostic.EXPECTED_SPARK_REPOSITORY}/actions/artifacts/{diagnostic.EXPECTED_SPARK_ARTIFACT_ID}": artifact,
                    f"/repos/{diagnostic.EXPECTED_SPARK_REPOSITORY}/actions/runs/{diagnostic.EXPECTED_SPARK_ARTIFACT_RUN_ID}": _run(),
                }
                with patch.object(self.provider, "_get_json", side_effect=responses.__getitem__), self.assertRaises(RuntimeError):
                    diagnostic._strict_spark_discover("spark", "windows", diagnostic.SPARK_CANDIDATE_SHA)
        with self.assertRaises(RuntimeError):
            diagnostic._strict_spark_discover("spark", "windows", "0" * 40)

    def test_zip_digest_and_traversal_reject_without_extraction(self) -> None:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zipped:
            zipped.writestr("endstone_spark.dll", b"candidate")
        data = stream.getvalue()

        class Response:
            def __init__(self, payload: bytes) -> None:
                self.payload = io.BytesIO(payload)

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return self.payload.read(size)

        class Opener:
            def open(self, *_args: object, **_kwargs: object) -> Response:
                return Response(data)

        artifact = _artifact()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "spark"
            with patch.object(self.provider.urllib.request, "build_opener", return_value=Opener()), patch.object(self.provider, "_request", return_value=object()), self.assertRaises(RuntimeError):
                diagnostic._strict_download_artifact(diagnostic.EXPECTED_SPARK_REPOSITORY, artifact, destination)
            self.assertFalse((destination / "payload").exists())

            traversal = Path(directory) / "traversal.zip"
            with zipfile.ZipFile(traversal, "w") as zipped:
                zipped.writestr("../escape.dll", b"bad")
            with self.assertRaises(RuntimeError):
                diagnostic._strict_safe_extract(traversal, Path(directory) / "safe")

    def test_installed_dll_bytes_are_bound_to_verified_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "downloads" / "spark" / "payload"
            payload.mkdir(parents=True)
            dll = payload / "endstone_spark.dll"
            dll.write_bytes(b"verified")
            installed = root / "work" / "windows" / "bedrock_server" / "plugins"
            installed.mkdir(parents=True)
            (installed / "endstone_spark.dll").write_bytes(b"verified")
            digest = hashlib.sha256(b"verified").hexdigest()
            validation = SimpleNamespace(
                metadata={"components": {"spark": {"artifact": {"id": diagnostic.EXPECTED_SPARK_ARTIFACT_ID, "digest": diagnostic.EXPECTED_SPARK_ARTIFACT_DIGEST, "download_sha256": diagnostic.EXPECTED_SPARK_ARTIFACT_DIGEST, "payload_dll_sha256": digest, "payload_dll_relative_path": "payload/endstone_spark.dll"}}}},
                downloads=root / "downloads",
                server_dir=root / "work" / "windows" / "bedrock_server",
                root=root,
            )
            evidence = diagnostic._verify_installed_spark_payload(validation)
            self.assertEqual(evidence["installed_dll_sha256"], digest)
            (installed / "endstone_spark.dll").write_bytes(b"tampered")
            with self.assertRaises(RuntimeError):
                diagnostic._verify_installed_spark_payload(validation)


class ResultContractTest(unittest.TestCase):
    def _result(self) -> dict[str, object]:
        digest = "a" * 64
        bytes_evidence = {
            "artifact_id": diagnostic.EXPECTED_SPARK_ARTIFACT_ID,
            "artifact_digest": diagnostic.EXPECTED_SPARK_ARTIFACT_DIGEST,
            "download_sha256": diagnostic.EXPECTED_SPARK_ARTIFACT_DIGEST,
            "payload_dll_sha256": digest,
            "installed_dll_sha256": digest,
        }
        profiles = [
            {"kind": "cpu-baseline"},
            {"kind": "20-player-load"},
            {"kind": "allocation-4096"},
        ]
        for profile in profiles:
            profile.update(
                {
                    "spark_artifact_id": diagnostic.EXPECTED_SPARK_ARTIFACT_ID,
                    "spark_artifact_digest": diagnostic.EXPECTED_SPARK_ARTIFACT_DIGEST,
                    "spark_download_sha256": diagnostic.EXPECTED_SPARK_ARTIFACT_DIGEST,
                    "spark_payload_dll_sha256": digest,
                    "spark_installed_dll_sha256": digest,
                }
            )
        profiles.extend(
            {
                "kind": f"post-reload-{index}",
                "command": diagnostic.POST_RELOAD_PROFILE_COMMAND,
                "reload_index": index * 3,
                "spark_artifact_id": diagnostic.EXPECTED_SPARK_ARTIFACT_ID,
                "spark_artifact_digest": diagnostic.EXPECTED_SPARK_ARTIFACT_DIGEST,
                "spark_download_sha256": diagnostic.EXPECTED_SPARK_ARTIFACT_DIGEST,
                "spark_payload_dll_sha256": digest,
                "spark_installed_dll_sha256": digest,
            }
            for index in range(1, 11)
        )
        return {
            "status": "PASS",
            "state": "completed",
            "spark_sha": diagnostic.SPARK_CANDIDATE_SHA,
            "spark_byte_evidence": bytes_evidence,
            "artifact_provenance": {"spark": {"artifact_id": diagnostic.EXPECTED_SPARK_ARTIFACT_ID, "artifact_digest": diagnostic.EXPECTED_SPARK_ARTIFACT_DIGEST}},
            "deadline": {"hard_seconds": diagnostic.HARD_DEADLINE_SECONDS, "cleanup_reserve_seconds": diagnostic.SHUTDOWN_RESERVE_SECONDS},
            "reload_repetitions": 10,
            "reloads_per_repetition": 3,
            "total_reload_target": 30,
            "post_reload_profile_target": 10,
            "post_reload_diagnostic": {"status": "not-triggered", "process": {"alive": False, "forced_termination": False}},
            "profiles": profiles,
            "plugin_reload_cycles": [{"cycle": cycle, "reload_index": cycle, "transport": "stdin", "command_published": True} for cycle in range(1, 31)],
        }

    def test_missing_or_failed_supervisor_verdict_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path = root / "fleet-spark-result.json"
            result = self._result()
            result["supervisor_verdict"] = {"path": str(root / "supervisor-verdict.json")}
            result_path.write_text(json.dumps(result), encoding="utf-8")
            environment = {"EXPECTED_SPARK_SHA": diagnostic.SPARK_CANDIDATE_SHA, "SPARK51_SUPERVISOR_VERDICT": str(root / "supervisor-verdict.json")}
            with self.assertRaises((RuntimeError, TypeError)):
                diagnostic.validate_result_contract(result_path, environment)
            (root / "supervisor-verdict.json").write_text(json.dumps({"status": "FAIL", "state": "failed"}), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                diagnostic.validate_result_contract(result_path, environment)
            (root / "supervisor-verdict.json").write_text(json.dumps({"status": "PASS", "state": "complete", "controller_exit_code": 0, "observer_exit_code": 0, "controller_job_empty": True, "observer_job_empty": True, "observer_death_confirmed": True, "forced_observer_termination": False}), encoding="utf-8")
            diagnostic.validate_result_contract(result_path, environment)


if __name__ == "__main__":
    unittest.main()
