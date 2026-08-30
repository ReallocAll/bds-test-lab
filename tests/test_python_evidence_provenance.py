from __future__ import annotations

import os
import unittest
from unittest import mock

from controller.python_evidence_provenance import (
    validate_bds_version,
    validate_component_provenance,
    validate_endstone_runtime_version,
)


class PythonEvidenceProvenanceTest(unittest.TestCase):
    def test_component_accepts_exact_sha_run_and_artifact(self) -> None:
        metadata = {
            "components": {
                "endstone": {
                    "sha": "a" * 40,
                    "run_id": 123,
                    "artifact": {"id": 456},
                }
            }
        }
        with mock.patch.dict(
            os.environ,
            {
                "EXPECTED_ENDSTONE_SHA": "a" * 40,
                "EXPECTED_ENDSTONE_RUN_ID": "123",
                "EXPECTED_ENDSTONE_ARTIFACT_ID": "456",
            },
            clear=True,
        ):
            observed = validate_component_provenance(metadata, "endstone")
        self.assertEqual(observed["run_id"], 123)

    def test_component_rejects_identity_drift(self) -> None:
        metadata = {
            "components": {
                "endstone": {
                    "sha": "a" * 40,
                    "run_id": 123,
                    "artifact": {"id": 456},
                }
            }
        }
        cases = (
            ({"EXPECTED_ENDSTONE_SHA": "b" * 40}, "SHA mismatch"),
            ({"EXPECTED_ENDSTONE_RUN_ID": "999"}, "run mismatch"),
            ({"EXPECTED_ENDSTONE_ARTIFACT_ID": "999"}, "artifact ID mismatch"),
        )
        for env, pattern in cases:
            with (
                self.subTest(pattern=pattern),
                mock.patch.dict(os.environ, env, clear=True),
                self.assertRaisesRegex(RuntimeError, pattern),
            ):
                validate_component_provenance(metadata, "endstone")

    def test_component_rejects_malformed_expected_id(self) -> None:
        metadata = {
            "components": {
                "spark": {"sha": "c" * 40, "run_id": 1, "artifact": {"id": 2}}
            }
        }
        with (
            mock.patch.dict(os.environ, {"EXPECTED_SPARK_RUN_ID": "not-an-id"}, clear=True),
            self.assertRaisesRegex(RuntimeError, "positive integer"),
        ):
            validate_component_provenance(metadata, "spark")

    def test_runtime_endstone_version_is_exact(self) -> None:
        with (
            mock.patch.dict(os.environ, {"EXPECTED_ENDSTONE_VERSION": "0.11.10.dev387"}, clear=True),
            mock.patch("importlib.metadata.version", return_value="0.11.10.dev387"),
        ):
            self.assertEqual(validate_endstone_runtime_version(), "0.11.10.dev387")
        with (
            mock.patch.dict(os.environ, {"EXPECTED_ENDSTONE_VERSION": "0.11.10.dev387"}, clear=True),
            mock.patch("importlib.metadata.version", return_value="0.11.10.dev388"),
            self.assertRaisesRegex(RuntimeError, "runtime version mismatch"),
        ):
            validate_endstone_runtime_version()

    def test_bds_version_is_exact(self) -> None:
        with mock.patch.dict(os.environ, {"EXPECTED_BDS_VERSION": "1.26.44.3"}, clear=True):
            self.assertEqual(validate_bds_version({"bds_version": "1.26.44.3"}), "1.26.44.3")
            with self.assertRaisesRegex(RuntimeError, "BDS version mismatch"):
                validate_bds_version({"bds_version": "1.26.45.0"})

    def test_unpinned_checks_preserve_legacy_behavior(self) -> None:
        metadata = {
            "components": {
                "spark": {"sha": "d" * 40, "run_id": 7, "artifact": {"id": 8}}
            }
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(validate_component_provenance(metadata, "spark")["sha"], "d" * 40)
            self.assertIsNone(validate_endstone_runtime_version())
            self.assertEqual(validate_bds_version({"bds_version": "anything"}), "anything")


if __name__ == "__main__":
    unittest.main()
