from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from providers import artifact_provider


class ArtifactProviderTest(unittest.TestCase):
    def test_exact_pins_validate_run_artifact_and_digest(self) -> None:
        run = {"id": 10, "conclusion": "success", "head_sha": "a" * 40,
               "repository": {"full_name": "ReallocAll/spark"}}
        artifact = {"id": 20, "name": "spark-linux", "expired": False,
                    "workflow_run": {"id": 10}, "digest": "sha256:" + "b" * 64}
        with mock.patch.object(artifact_provider, "_get_json", side_effect=[run, artifact]):
            self.assertEqual(artifact_provider.discover("spark", "linux", "a" * 40, 10, 20, "b" * 64), (run, artifact))
        for field, value in (("id", 21), ("name", "spark-windows"), ("expired", True),
                             ("workflow_run", {"id": 11}), ("digest", None), ("digest", "sha256:" + "c" * 64)):
            with self.subTest(field=field, value=value), mock.patch.object(
                artifact_provider, "_get_json", side_effect=[run, {**artifact, field: value}]
            ), self.assertRaises(artifact_provider.ArtifactResolutionError):
                artifact_provider.discover("spark", "linux", "a" * 40, 10, 20, "b" * 64)
        for field, value in (("id", 11), ("conclusion", "failure"), ("head_sha", "c" * 40),
                             ("repository", {"full_name": "other/spark"})):
            with self.subTest(field=field), mock.patch.object(
                artifact_provider, "_get_json", return_value={**run, field: value}
            ), self.assertRaises(artifact_provider.ArtifactResolutionError):
                artifact_provider.discover("spark", "linux", "a" * 40, 10, 20)

    def test_download_verifies_actual_zip_bytes_before_extracting(self) -> None:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("endstone_spark.dll", b"plugin")
        data = stream.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        for expected, succeeds in ((digest, True), ("0" * 64, False)):
            with self.subTest(succeeds=succeeds), tempfile.TemporaryDirectory() as temporary, mock.patch.object(
                artifact_provider, "_request", return_value=mock.sentinel.request
            ), mock.patch.object(artifact_provider.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(data)
                artifact = {"id": 20, "digest": "sha256:" + expected}
                if succeeds:
                    payload = artifact_provider._download_artifact("ReallocAll/spark", artifact, Path(temporary))
                    self.assertEqual((payload / "endstone_spark.dll").read_bytes(), b"plugin")
                    self.assertEqual(artifact["downloaded_sha256"], digest)
                else:
                    with self.assertRaises(artifact_provider.ArtifactResolutionError):
                        artifact_provider._download_artifact("ReallocAll/spark", artifact, Path(temporary))
                    self.assertFalse((Path(temporary) / "payload").exists())

    def test_pin_environment_and_explicit_precedence(self) -> None:
        environment = {"EXPECTED_SPARK_RUN_ID": "10", "EXPECTED_SPARK_ARTIFACT_ID": "20",
                       "EXPECTED_SPARK_ARTIFACT_DIGEST": "b" * 64}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, environment), mock.patch.object(
            artifact_provider, "discover", return_value=({"id": 10}, {"id": 20})
        ) as discover, mock.patch.object(artifact_provider, "_download_artifact", return_value=Path(temporary)), mock.patch.object(artifact_provider, "save_metadata"):
            artifact_provider.resolve_artifacts("linux", temporary)
            self.assertEqual(discover.call_args.kwargs["expected_run_id"], "10")
            self.assertEqual(discover.call_args.kwargs["expected_artifact_id"], "20")
            self.assertEqual(discover.call_args.kwargs["expected_artifact_digest"], "b" * 64)
            artifact_provider.resolve_artifacts("linux", temporary, spark_run_id=11, spark_artifact_id=21, spark_artifact_digest="c" * 64)
            self.assertEqual(discover.call_args.kwargs["expected_run_id"], 11)
            self.assertEqual(discover.call_args.kwargs["expected_artifact_id"], 21)
            self.assertEqual(discover.call_args.kwargs["expected_artifact_digest"], "c" * 64)

    def test_default_discovery_stays_on_configured_branch(self) -> None:
        run = {
            "id": 10,
            "head_branch": "develop",
            "head_sha": "a" * 40,
            "conclusion": "success",
        }
        artifact = {"id": 20, "name": "spark-linux", "expired": False}

        def fake_get(path: str):
            if "/artifacts" in path:
                return {"artifacts": [artifact]}
            self.assertIn("branch=develop", path)
            self.assertNotIn("head_sha=", path)
            return {"workflow_runs": [run]}

        with mock.patch.object(artifact_provider, "_get_json", side_effect=fake_get):
            resolved_run, resolved_artifact = artifact_provider.discover("spark", "linux")

        self.assertIs(resolved_run, run)
        self.assertIs(resolved_artifact, artifact)

    def test_exact_sha_discovery_accepts_successful_feature_branch_run(self) -> None:
        expected_sha = "b" * 40
        run = {
            "id": 11,
            "head_branch": "fix/windows-allocation-safe-hooks",
            "head_sha": expected_sha,
            "conclusion": "success",
        }
        artifact = {"id": 21, "name": "spark-windows", "expired": False}

        def fake_get(path: str):
            if "/artifacts" in path:
                return {"artifacts": [artifact]}
            self.assertIn(f"head_sha={expected_sha}", path)
            self.assertNotIn("branch=develop", path)
            return {"workflow_runs": [run]}

        with mock.patch.object(artifact_provider, "_get_json", side_effect=fake_get):
            resolved_run, resolved_artifact = artifact_provider.discover(
                "spark", "windows", expected_sha=expected_sha
            )

        self.assertIs(resolved_run, run)
        self.assertIs(resolved_artifact, artifact)

    def test_exact_sha_never_falls_back_to_stale_run(self) -> None:
        expected_sha = "c" * 40
        stale_run = {
            "id": 12,
            "head_branch": "develop",
            "head_sha": "d" * 40,
            "conclusion": "success",
        }

        with mock.patch.object(
            artifact_provider,
            "_get_json",
            return_value={"workflow_runs": [stale_run]},
        ), self.assertRaisesRegex(artifact_provider.ArtifactResolutionError, expected_sha):
            artifact_provider.discover("spark", "linux", expected_sha=expected_sha)

    def test_resolve_artifacts_uses_expected_spark_sha_from_environment(self) -> None:
        expected_sha = "e" * 40
        endstone_run = {
            "id": 30,
            "head_branch": "develop",
            "head_sha": "f" * 40,
            "conclusion": "success",
        }
        spark_run = {
            "id": 31,
            "head_branch": "feature",
            "head_sha": expected_sha,
            "conclusion": "success",
        }
        endstone_artifact = {"id": 40, "name": "endstone-linux.zip"}
        spark_artifact = {"id": 41, "name": "spark-linux"}
        calls: list[tuple[str, str, str | None]] = []

        def fake_discover(component: str, platform: str, expected_sha: str | None = None):
            calls.append((component, platform, expected_sha))
            if component == "spark":
                return spark_run, spark_artifact
            return endstone_run, endstone_artifact

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"EXPECTED_SPARK_SHA": expected_sha, "EXPECTED_ENDSTONE_SHA": ""},
            clear=False,
        ), mock.patch.object(
            artifact_provider, "discover", side_effect=fake_discover
        ), mock.patch.object(
            artifact_provider,
            "_download_artifact",
            side_effect=lambda _repo, _artifact, destination: Path(destination) / "payload",
        ), mock.patch.object(artifact_provider, "save_metadata"):
            result = artifact_provider.resolve_artifacts(
                "linux", Path(tmp) / "downloads", Path(tmp) / "metadata.json"
            )

        self.assertEqual(
            calls,
            [
                ("endstone", "linux", None),
                ("spark", "linux", expected_sha),
            ],
        )
        self.assertEqual(result["components"]["spark"]["sha"], expected_sha)

    def test_resolve_artifacts_pins_both_components_from_environment(self) -> None:
        spark_sha = "1" * 40
        endstone_sha = "2" * 40
        runs = {
            "endstone": {
                "id": 50,
                "head_branch": "develop",
                "head_sha": endstone_sha,
                "conclusion": "success",
            },
            "spark": {
                "id": 51,
                "head_branch": "feature",
                "head_sha": spark_sha,
                "conclusion": "success",
            },
        }
        artifacts = {
            "endstone": {"id": 60, "name": "endstone-linux.zip"},
            "spark": {"id": 61, "name": "spark-linux"},
        }
        calls: list[tuple[str, str, str | None]] = []

        def fake_discover(component: str, platform: str, expected_sha: str | None = None):
            calls.append((component, platform, expected_sha))
            return runs[component], artifacts[component]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"EXPECTED_SPARK_SHA": spark_sha, "EXPECTED_ENDSTONE_SHA": endstone_sha},
            clear=False,
        ), mock.patch.object(
            artifact_provider, "discover", side_effect=fake_discover
        ), mock.patch.object(
            artifact_provider,
            "_download_artifact",
            side_effect=lambda _repo, _artifact, destination: Path(destination) / "payload",
        ), mock.patch.object(artifact_provider, "save_metadata"):
            result = artifact_provider.resolve_artifacts(
                "linux", Path(tmp) / "downloads", Path(tmp) / "metadata.json"
            )

        self.assertEqual(
            calls,
            [
                ("endstone", "linux", endstone_sha),
                ("spark", "linux", spark_sha),
            ],
        )
        self.assertEqual(result["components"]["endstone"]["sha"], endstone_sha)
        self.assertEqual(result["components"]["spark"]["sha"], spark_sha)

    def test_explicit_component_shas_override_environment(self) -> None:
        spark_sha = "3" * 40
        endstone_sha = "4" * 40
        calls: list[tuple[str, str, str | None]] = []

        def fake_discover(component: str, platform: str, expected_sha: str | None = None):
            calls.append((component, platform, expected_sha))
            return (
                {
                    "id": 70,
                    "head_branch": "feature",
                    "head_sha": expected_sha,
                    "conclusion": "success",
                },
                {"id": 71, "name": f"{component}-linux"},
            )

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"EXPECTED_SPARK_SHA": "5" * 40, "EXPECTED_ENDSTONE_SHA": "6" * 40},
            clear=False,
        ), mock.patch.object(
            artifact_provider, "discover", side_effect=fake_discover
        ), mock.patch.object(
            artifact_provider,
            "_download_artifact",
            side_effect=lambda _repo, _artifact, destination: Path(destination) / "payload",
        ), mock.patch.object(artifact_provider, "save_metadata"):
            artifact_provider.resolve_artifacts(
                "linux",
                Path(tmp) / "downloads",
                Path(tmp) / "metadata.json",
                spark_sha=spark_sha,
                endstone_sha=endstone_sha,
            )

        self.assertEqual(
            calls,
            [
                ("endstone", "linux", endstone_sha),
                ("spark", "linux", spark_sha),
            ],
        )


if __name__ == "__main__":
    unittest.main()
