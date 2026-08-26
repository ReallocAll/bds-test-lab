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
            os.environ, {"EXPECTED_SPARK_SHA": expected_sha}, clear=False
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


if __name__ == "__main__":
    unittest.main()
