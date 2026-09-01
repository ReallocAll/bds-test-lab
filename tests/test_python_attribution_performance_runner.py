from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import threading
import time
import unittest
from collections.abc import Generator
from pathlib import Path
from unittest import mock

import yaml

from controller.python_attribution_performance_runner import (
    validate_bds_version,
    validate_component_provenance,
)
from controller.python_attribution_validation import EXPECTED_SOURCE
from controller.windows_evidence_matrix import PYTHON_MATRIX, resolve_matrix


def _load_hotspot_plugin_class() -> type:
    source_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "spark-python-hotspot-test"
        / "src"
        / "endstone_spark_python_hotspot_test"
        / "__init__.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    plugin_definition = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    plugin_definition.bases = [ast.Name(id="Plugin", ctx=ast.Load())]
    module = ast.Module(body=[plugin_definition], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Plugin": object,
        "PlayerMoveEvent": object,
        "event_handler": lambda function: function,
        "Generator": Generator,
        "Path": Path,
        "asyncio": asyncio,
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "threading": threading,
        "time": time,
    }
    exec(compile(module, str(source_path), "exec"), namespace)  # noqa: S102
    return namespace["HotspotPlugin"]


class PythonAttributionPerformanceRunnerTest(unittest.TestCase):
    def test_hotspot_plugin_async_tail_budget_scales_for_windows_sampling(self) -> None:
        hotspot_plugin = _load_hotspot_plugin_class()
        for iterations, expected_tail in ((100, 3000), (12000, 3000), (48000, 12000)):
            with self.subTest(iterations=iterations):
                plugin = object.__new__(hotspot_plugin)
                plugin.iterations = iterations
                calls: list[int] = []
                plugin.integer_hash_loop = lambda count, calls=calls: calls.append(count) or count

                asyncio.run(plugin._async_leaf(0))

                self.assertEqual(calls, [expected_tail])

    def test_hotspot_plugin_dual_workload_rotates_balanced_splits_and_order(self) -> None:
        hotspot_plugin = _load_hotspot_plugin_class()
        expected_a = {
            101: [60, 66, 71, 76, 81],
            1001: [600, 651, 701, 751, 801],
            12001: [7200, 7801, 8401, 9001, 9601],
            12000: [7200, 7800, 8400, 9000, 9600],
            48000: [28800, 31200, 33600, 36000, 38400],
        }
        for iterations, expected in expected_a.items():
            with self.subTest(iterations=iterations):
                plugin = object.__new__(hotspot_plugin)
                plugin.iterations = iterations
                plugin._dual_flip = False
                plugin._dual_split_index = 0
                calls: list[tuple[str, int]] = []
                plugin.hotspot_a = lambda count, calls=calls: calls.append(("a", count)) or count
                plugin.hotspot_b = lambda count, calls=calls: calls.append(("b", count)) or count

                for _ in range(5):
                    plugin.dual_hotspot()

                actual_a = [count for name, count in calls if name == "a"]
                actual_b = [count for name, count in calls if name == "b"]
                self.assertEqual(actual_a, expected)
                self.assertEqual(actual_b, [iterations - count for count in expected])
                self.assertEqual([a + b for (_, a), (_, b) in zip(calls[::2], calls[1::2])], [iterations] * 5)
                self.assertEqual(sum(actual_a), (iterations * 7 + 1) // 2)
                self.assertEqual(sum(actual_b), iterations * 5 - sum(actual_a))
                self.assertEqual(
                    [calls[index][0] + calls[index + 1][0] for index in range(0, 10, 2)],
                    ["ab", "ba", "ab", "ba", "ab"],
                )

    def test_python_dispatch_selectors_are_fail_closed_and_preserve_default_matrix(self) -> None:
        workflow = Path(__file__).parents[1] / ".github" / "workflows" / "python-attribution-bds-e2e.yml"
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        dispatch = data[True]["workflow_dispatch"]["inputs"]["target"]
        jobs = data["jobs"]
        profile = jobs["profile"]

        self.assertEqual(dispatch["type"], "choice")
        self.assertEqual(dispatch["options"], ["all", "windows-dual", "windows-fleet"])
        self.assertNotIn("if", profile)
        self.assertEqual(profile["strategy"]["matrix"], "${{ fromJSON(needs.resolve-target.outputs.matrix) }}")
        self.assertEqual(
            resolve_matrix("python", "push", None),
            {"include": list(PYTHON_MATRIX)},
        )
        dual = resolve_matrix("python", "workflow_dispatch", "windows-dual")["include"]
        fleet = resolve_matrix("python", "workflow_dispatch", "windows-fleet")["include"]
        self.assertEqual([(row["platform"], row["mode"]) for row in dual], [("windows", "dual")])
        self.assertEqual([(row["platform"], row["mode"]) for row in fleet], [("windows", "fleet")])
        self.assertNotIn(("windows", "single"), [(row["platform"], row["mode"]) for row in dual + fleet])
        resolver_run = jobs["resolve-target"]["steps"][1]["run"]
        self.assertIn("controller.windows_evidence_matrix", resolver_run)

        for invalid in ("windows", "windows-single", "bogus"):
            with self.subTest(target=invalid), self.assertRaisesRegex(ValueError, "target"):
                resolve_matrix("python", "workflow_dispatch", invalid)

    def test_hotspot_plugin_source_identity_uses_installed_module_name(self) -> None:
        self.assertEqual(EXPECTED_SOURCE, "spark_python_hotspot_test")
        self.assertNotEqual(EXPECTED_SOURCE, "spark-python-hotspot-test")

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
