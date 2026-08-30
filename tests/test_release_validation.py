from __future__ import annotations

import os
import unittest
from unittest import mock

from controller.release_validation import (
    _optional_positive_int_env,
    _required_env,
    validate_component_identity,
    validate_exact_version,
)


class ReleaseValidationProvenanceTest(unittest.TestCase):
    def test_validate_component_identity_accepts_exact_endstone(self) -> None:
        metadata = {
            "components": {
                "endstone": {
                    "sha": "a" * 40,
                    "run_id": 123,
                    "artifact": {"id": 456},
                }
            }
        }
        observed = validate_component_identity(
            metadata,
            "endstone",
            expected_sha="a" * 40,
            expected_run_id=123,
            expected_artifact_id=456,
        )
        self.assertEqual(observed["run_id"], 123)

    def test_validate_component_identity_rejects_any_identity_drift(self) -> None:
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
            ({"expected_sha": "b" * 40}, "SHA mismatch"),
            ({"expected_sha": "a" * 40, "expected_run_id": 999}, "run mismatch"),
            (
                {"expected_sha": "a" * 40, "expected_run_id": 123, "expected_artifact_id": 999},
                "artifact ID mismatch",
            ),
        )
        for kwargs, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(RuntimeError, pattern):
                validate_component_identity(metadata, "endstone", **kwargs)

    def test_validate_component_identity_requires_component_metadata(self) -> None:
        with self.assertRaisesRegex(TypeError, "missing component"):
            validate_component_identity({}, "spark", expected_sha="c" * 40)

    def test_validate_exact_version_is_strict(self) -> None:
        self.assertEqual(validate_exact_version("1.26.44.3", "1.26.44.3", "BDS"), "1.26.44.3")
        with self.assertRaisesRegex(RuntimeError, "BDS version mismatch"):
            validate_exact_version("1.26.45.0", "1.26.44.3", "BDS")
        with self.assertRaisesRegex(RuntimeError, "BDS version mismatch"):
            validate_exact_version(None, "1.26.44.3", "BDS")

    def test_required_env_and_positive_id_guards(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            RuntimeError, "EXPECTED_SPARK_SHA is required"
        ):
            _required_env("EXPECTED_SPARK_SHA")
        with mock.patch.dict(os.environ, {"EXPECTED_SPARK_SHA": "abc"}, clear=True):
            self.assertEqual(_required_env("EXPECTED_SPARK_SHA"), "abc")
        with mock.patch.dict(os.environ, {"EXPECTED_ENDSTONE_RUN_ID": "123"}, clear=True):
            self.assertEqual(_optional_positive_int_env("EXPECTED_ENDSTONE_RUN_ID"), 123)
        with mock.patch.dict(os.environ, {"EXPECTED_ENDSTONE_RUN_ID": "0"}, clear=True), self.assertRaisesRegex(
            RuntimeError, "positive integer"
        ):
            _optional_positive_int_env("EXPECTED_ENDSTONE_RUN_ID")


if __name__ == "__main__":
    unittest.main()
