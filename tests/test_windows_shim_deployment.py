from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from controller.run_test import IntegrationTest


class WindowsShimDeploymentTest(unittest.TestCase):
    def test_windows_artifact_deploys_plugin_and_allocation_shim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            endstone = downloads / "endstone" / "payload"
            spark = downloads / "spark" / "payload"
            endstone.mkdir(parents=True)
            spark.mkdir(parents=True)
            wheel = endstone / "endstone-test-cp313-cp313-win_amd64.whl"
            plugin = spark / "endstone_spark.dll"
            shim = spark / "spark_allocation_shim.dll"
            wheel.write_bytes(b"wheel")
            plugin.write_bytes(b"plugin")
            shim.write_bytes(b"shim")

            fixture = IntegrationTest.__new__(IntegrationTest)
            fixture.platform = "windows"
            fixture.root = root
            fixture.downloads = downloads
            fixture.metadata_path = root / "metadata.json"
            fixture.server_dir = root / "server"
            fixture.metadata = {}
            checks: list[str] = []
            fixture.check = lambda name, *_args, **_kwargs: checks.append(name)

            with mock.patch("controller.run_test.resolve_artifacts", return_value={}), mock.patch(
                "controller.run_test.run_checked"
            ):
                fixture.install_artifacts()

            plugin_dir = fixture.server_dir / "plugins"
            self.assertEqual((plugin_dir / "endstone_spark.dll").read_bytes(), b"plugin")
            self.assertEqual((plugin_dir / "spark_allocation_shim.dll").read_bytes(), b"shim")
            self.assertIn("spark-plugin-deployed", checks)
            self.assertIn("spark-allocation-shim-deployed", checks)


if __name__ == "__main__":
    unittest.main()
