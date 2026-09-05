from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from providers import artifact_provider


class SparkResearchArtifactProvenanceTest(unittest.TestCase):
    def test_exact_workflow_disambiguates_same_sha_runs(self) -> None:
        expected_sha = "a" * 40
        build_run = {
            "id": 10,
            "name": "Build",
            "head_branch": "exp/windows-stable-entry-hooks",
            "head_sha": expected_sha,
            "conclusion": "success",
        }
        no_shim_run = {
            "id": 11,
            "name": "Windows No-Shim Real Plugin Experiment",
            "head_branch": "exp/windows-stable-entry-hooks",
            "head_sha": expected_sha,
            "conclusion": "success",
        }
        no_shim_artifact = {
            "id": 21,
            "name": f"spark-windows-no-shim-{expected_sha}",
            "expired": False,
        }

        def fake_get(path: str):
            if "/artifacts" in path:
                self.assertIn("/runs/11/", path)
                return {"artifacts": [no_shim_artifact]}
            return {"workflow_runs": [build_run, no_shim_run]}

        with mock.patch.object(artifact_provider, "_get_json", side_effect=fake_get):
            run, artifact = artifact_provider.discover(
                "spark",
                "windows",
                expected_sha=expected_sha,
                expected_workflow="Windows No-Shim Real Plugin Experiment",
            )

        self.assertIs(run, no_shim_run)
        self.assertIs(artifact, no_shim_artifact)

    def test_artifact_prefix_disambiguates_payloads_in_one_run(self) -> None:
        expected_sha = "b" * 40
        run = {
            "id": 12,
            "name": "Windows No-Shim Real Plugin Experiment",
            "head_branch": "exp/windows-stable-entry-hooks",
            "head_sha": expected_sha,
            "conclusion": "success",
        }
        ordinary = {"id": 30, "name": "spark-windows-12", "expired": False}
        no_shim = {
            "id": 31,
            "name": f"spark-windows-no-shim-{expected_sha}",
            "expired": False,
        }

        def fake_get(path: str):
            if "/artifacts" in path:
                return {"artifacts": [ordinary, no_shim]}
            return {"workflow_runs": [run]}

        with mock.patch.object(artifact_provider, "_get_json", side_effect=fake_get):
            resolved_run, artifact = artifact_provider.discover(
                "spark",
                "windows",
                expected_sha=expected_sha,
                artifact_name_prefix="spark-windows-no-shim-",
            )

        self.assertIs(resolved_run, run)
        self.assertIs(artifact, no_shim)

    def test_resolve_artifacts_passes_research_constraints_from_environment(self) -> None:
        spark_sha = "c" * 40
        calls: list[tuple[str, str, dict[str, object]]] = []

        def fake_discover(component: str, platform: str, **kwargs: object):
            calls.append((component, platform, kwargs))
            return (
                {
                    "id": 40,
                    "name": "Windows No-Shim Real Plugin Experiment" if component == "spark" else "Build",
                    "head_branch": "feature" if component == "spark" else "develop",
                    "head_sha": kwargs.get("expected_sha") or "d" * 40,
                    "conclusion": "success",
                },
                {
                    "id": 41,
                    "name": "spark-windows-no-shim-test" if component == "spark" else "endstone-windows.zip",
                },
            )

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "EXPECTED_SPARK_SHA": spark_sha,
                "EXPECTED_SPARK_WORKFLOW": "Windows No-Shim Real Plugin Experiment",
                "EXPECTED_SPARK_ARTIFACT_PREFIX": "spark-windows-no-shim-",
                "EXPECTED_ENDSTONE_SHA": "",
            },
            clear=False,
        ), mock.patch.object(
            artifact_provider, "discover", side_effect=fake_discover
        ), mock.patch.object(
            artifact_provider,
            "_download_artifact",
            side_effect=lambda _repo, _artifact, destination: Path(destination) / "payload",
        ), mock.patch.object(artifact_provider, "save_metadata"):
            artifact_provider.resolve_artifacts(
                "windows", Path(tmp) / "downloads", Path(tmp) / "metadata.json"
            )

        self.assertEqual(calls[0], ("endstone", "windows", {"expected_sha": None}))
        self.assertEqual(
            calls[1],
            (
                "spark",
                "windows",
                {
                    "expected_sha": spark_sha,
                    "expected_workflow": "Windows No-Shim Real Plugin Experiment",
                    "artifact_name_prefix": "spark-windows-no-shim-",
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
