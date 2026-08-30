from __future__ import annotations

import os
import unittest
from unittest import mock

import controller.combined_pack_gamerule_fleet_exact_runner as exact


class _Server:
    def snapshot(self) -> list[str]:
        return [
            "[INFO] Starting Server",
            "[INFO] Version: 1.26.44.3",
        ]


class _Validator:
    def __init__(self) -> None:
        self.server = _Server()
        self.result = {"bds_version": "26.44"}
        self.metadata = {
            "components": {
                "spark": {
                    "sha": "a" * 40,
                    "run_id": 11,
                    "artifact": {"id": 12},
                },
                "endstone": {
                    "sha": "b" * 40,
                    "run_id": 21,
                    "artifact": {"id": 22},
                },
            }
        }
        self.checks: list[tuple[str, str, dict[str, object]]] = []

    def check(self, name: str, status: str, *args: object, **kwargs: object) -> None:
        self.checks.append((name, status, dict(kwargs)))


class CombinedPackExactRunnerTest(unittest.TestCase):
    def test_install_artifacts_requires_exact_component_identity(self) -> None:
        validator = _Validator()
        env = {
            "EXPECTED_SPARK_SHA": "a" * 40,
            "EXPECTED_ENDSTONE_SHA": "b" * 40,
            "EXPECTED_ENDSTONE_RUN_ID": "21",
            "EXPECTED_ENDSTONE_ARTIFACT_ID": "22",
            "EXPECTED_ENDSTONE_VERSION": "0.11.10.dev387",
        }
        with (
            mock.patch.object(exact, "_ORIGINAL_INSTALL_ARTIFACTS", lambda _self: None),
            mock.patch.object(exact, "validate_endstone_runtime_version", return_value="0.11.10.dev387"),
            mock.patch.dict(os.environ, env, clear=True),
        ):
            exact._install_exact_artifacts(validator)  # type: ignore[arg-type]

        name, status, fields = validator.checks[-1]
        self.assertEqual((name, status), ("exact-artifact-provenance", "PASS"))
        self.assertEqual(fields["spark_sha"], "a" * 40)
        self.assertEqual(fields["endstone_sha"], "b" * 40)
        self.assertEqual(fields["endstone_run_id"], 21)
        self.assertEqual(fields["endstone_artifact_id"], 22)

    def test_start_server_requires_protocol_and_full_runtime_version(self) -> None:
        validator = _Validator()
        with (
            mock.patch.object(exact, "_ORIGINAL_START_SERVER", lambda _self: None),
            mock.patch.dict(os.environ, {"EXPECTED_BDS_VERSION": "1.26.44.3"}, clear=True),
        ):
            exact._start_exact_server(validator)  # type: ignore[arg-type]

        name, status, fields = validator.checks[-1]
        self.assertEqual((name, status), ("exact-bds-version", "PASS"))
        self.assertEqual(fields["observed_protocol"], "26.44")
        self.assertEqual(fields["expected_full"], "1.26.44.3")

    def test_start_server_rejects_full_runtime_drift(self) -> None:
        validator = _Validator()
        validator.server.snapshot = lambda: ["Version: 1.26.45.0"]  # type: ignore[method-assign]
        with (
            mock.patch.object(exact, "_ORIGINAL_START_SERVER", lambda _self: None),
            mock.patch.dict(os.environ, {"EXPECTED_BDS_VERSION": "1.26.44.3"}, clear=True),
            self.assertRaisesRegex(RuntimeError, "full version mismatch"),
        ):
            exact._start_exact_server(validator)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
