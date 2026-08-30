from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from providers import artifact_provider


class ArtifactProviderTest(unittest.TestCase):
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
