from __future__ import annotations

import os
import unittest
from unittest import mock

from controller.python_attribution_performance_runner import (
    validate_bds_version,
    validate_component_provenance,
)


class PythonAttributionPerformanceRunnerTest(unittest.TestCase):
    def test_exact_component_provenance_accepts_matching_endstone_identity(self) -> None:
        metadata = {
            "components": {
                "endstone": {
                    "sha": "a" * 40,
                    "run_id": 123,
                    "artifact": {"id": 456, "name": "endstone-linux.zip"},
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
            clear=False,
        ):
            observed = validate_component_provenance(metadata, "endstone")
        self.assertEqual(observed["run_id"], 123)

    def test_exact_component_provenance_rejects_sha_run_or_artifact_drift(self) -> None:
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
            (
                {
                    "EXPECTED_ENDSTONE_SHA": "a" * 40,
                    "EXPECTED_ENDSTONE_RUN_ID": "999",
                },
                "run mismatch",
            ),
            (
                {
                    "EXPECTED_ENDSTONE_SHA": "a" * 40,
                    "EXPECTED_ENDSTONE_RUN_ID": "123",
                    "EXPECTED_ENDSTONE_ARTIFACT_ID": "999",
                },
                "artifact ID mismatch",
            ),
        )
        for env, pattern in cases:
            with (
                self.subTest(pattern=pattern),
                mock.patch.dict(os.environ, env, clear=True),
                self.assertRaisesRegex(RuntimeError, pattern),
            ):
                validate_component_provenance(metadata, "endstone")

    def test_component_provenance_rejects_malformed_expected_id(self) -> None:
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

    def test_bds_version_guard_accepts_exact_protocol_and_runtime(self) -> None:
        env = {
            "EXPECTED_BDS_VERSION": "1.26.44.3",
            "EXPECTED_BDS_PROTOCOL_VERSION": "26.44",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                validate_bds_version({"bds_version": "26.44"}, ["[INFO] Version: 1.26.44.3"]),
                "26.44",
            )

    def test_bds_version_guard_rejects_protocol_runtime_or_missing_evidence(self) -> None:
        env = {
            "EXPECTED_BDS_VERSION": "1.26.44.3",
            "EXPECTED_BDS_PROTOCOL_VERSION": "26.44",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "protocol version mismatch"):
                validate_bds_version({"bds_version": "26.45"}, ["Version: 1.26.44.3"])
            with self.assertRaisesRegex(RuntimeError, "full version mismatch"):
                validate_bds_version({"bds_version": "26.44"}, ["Version: 1.26.45.0"])
            with self.assertRaisesRegex(RuntimeError, "full-version runtime evidence is required"):
                validate_bds_version({"bds_version": "26.44"})

    def test_unpinned_component_and_bds_keep_legacy_behavior(self) -> None:
        metadata = {
            "components": {
                "spark": {"sha": "d" * 40, "run_id": 7, "artifact": {"id": 8}}
            }
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(validate_component_provenance(metadata, "spark")["sha"], "d" * 40)
            self.assertEqual(validate_bds_version({"bds_version": "anything"}), "anything")


if __name__ == "__main__":
    unittest.main()
