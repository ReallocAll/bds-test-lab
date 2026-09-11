from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from controller.run_test import IntegrationTest, extract_spark_linux

PLUGIN = "endstone_spark.so"
HELPER = ".spark-native/libspark_allocation_gateway_v1.so"


def package(path: Path, entries: list[tuple[str, bytes, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


class SparkArtifactInstallationTest(unittest.TestCase):
    def test_rejects_oversized_member_before_reading_contents(self) -> None:
        member = tarfile.TarInfo(PLUGIN)
        member.size = 256 * 1024 * 1024 + 1
        with tempfile.TemporaryDirectory() as temporary, mock.patch("controller.run_test.tarfile.open") as archive:
            archive.return_value.__enter__.return_value.__iter__.return_value = iter([member])
            with self.assertRaisesRegex(ValueError, "oversized"):
                extract_spark_linux(Path(temporary) / "package.tar.gz", Path(temporary) / "out")
            archive.return_value.__enter__.return_value.extractfile.assert_not_called()

    def test_installation_packages_and_legacy(self) -> None:
        for mode in ("package", "legacy", "windows", "wrong-plugin"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {}, clear=True):
                root = Path(temporary)
                fixture = IntegrationTest.__new__(IntegrationTest)
                fixture.root = root
                fixture.downloads = root / "downloads"
                fixture.server_dir = root / "server"
                fixture.metadata_path = root / "metadata.json"
                fixture.platform = "windows" if mode == "windows" else "linux"
                fixture.check = mock.Mock()
                spark = fixture.downloads / "spark" / "payload"
                endstone = fixture.downloads / "endstone" / "payload"
                spark.mkdir(parents=True)
                endstone.mkdir(parents=True)
                (endstone / "endstone-test.whl").write_bytes(b"wheel")
                if mode in {"package", "wrong-plugin"}:
                    package(spark / "spark-linux.tar.gz", [(PLUGIN, b"plugin", tarfile.REGTYPE)])
                else:
                    (spark / ("endstone_spark.dll" if mode == "windows" else PLUGIN)).write_bytes(b"plugin")
                if mode.startswith("wrong-"):
                    os.environ["EXPECTED_SPARK_PLUGIN_SHA256"] = "0" * 64
                with mock.patch("controller.run_test.resolve_artifacts", return_value={}), mock.patch("controller.run_test.run_checked"):
                    if mode.startswith("wrong-"):
                        with self.assertRaisesRegex(ValueError, "SHA256"):
                            fixture.install_artifacts()
                        self.assertFalse((fixture.server_dir / "plugins" / PLUGIN).exists())
                        continue
                    fixture.install_artifacts()
                evidence = json.loads(fixture.metadata_path.read_text())["components"]["spark"]["spark_installed_files"]
                self.assertEqual(len(evidence), 1)
                for item in evidence:
                    content = (fixture.server_dir / item["relative_path"]).read_bytes()
                    self.assertEqual(item["size"], len(content))
                    self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())
                if mode == "package":
                    self.assertFalse((fixture.server_dir / "plugins" / HELPER).exists())

    def test_rejects_invalid_packages_before_extracting(self) -> None:
        plugin = (PLUGIN, b"plugin", tarfile.REGTYPE)
        helper = (HELPER, b"helper", tarfile.REGTYPE)
        cases = [[], [plugin, helper], [plugin, plugin], [plugin, ("extra", b"", tarfile.REGTYPE)]]
        cases.extend([[(name, b"", kind)] for name, kind in (
            ("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
            (".spark-native/../escape", tarfile.REGTYPE), (PLUGIN, tarfile.SYMTYPE),
            (PLUGIN, tarfile.LNKTYPE), (PLUGIN, tarfile.DIRTYPE))])
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                package(root / "package.tar.gz", entries)
                with self.assertRaises(ValueError):
                    extract_spark_linux(root / "package.tar.gz", root / "out")
                self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
