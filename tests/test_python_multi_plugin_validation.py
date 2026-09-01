from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from controller.python_multi_plugin_validation import (
    PLUGIN_SPECS,
    PythonMultiPluginValidation,
    _required_bds_version,
    _validate_exact_bds_evidence,
    validate_multi_plugin_profile,
)
from controller.python_profile_payload import Node, ProfilePayload, ThreadTree


def valid_profile() -> ProfilePayload:
    nodes: list[Node] = []
    roots: list[int] = []
    for spec in PLUGIN_SPECS:
        base = len(nodes)
        class_name = f"[Python] {spec.module}"
        nodes.extend(
            [
                Node(
                    class_name=class_name,
                    method_name=spec.chain[0],
                    line_number=10,
                    method_desc=f"{spec.module}:10",
                    times=[20.0],
                    children_refs=[base + 1],
                ),
                Node(
                    class_name=class_name,
                    method_name=spec.chain[1],
                    line_number=11,
                    method_desc=f"{spec.module}:11",
                    times=[15.0],
                    children_refs=[base + 2],
                ),
                Node(
                    class_name=class_name,
                    method_name=spec.chain[2],
                    line_number=12,
                    method_desc=f"{spec.module}:12",
                    times=[10.0],
                ),
            ]
        )
        roots.append(base)
    nodes.append(Node(method_name="bedrock_server!tick", times=[10.0]))
    roots.append(len(nodes) - 1)
    diagnostics = {
        "Python attribution backend": '"PEP669"',
        "Python function attribution enabled": "true",
        "Python PY_START events": "300",
        "Python shadow snapshot attempts": "280",
        "Python attributed samples": "120",
        "Python monitoring callback failures": "0",
        "Python shadow snapshot failures": "0",
        "Python shadow overflows": "0",
        "Python native boundary misses": "0",
        "Python thread mismatches": "0",
        "Python unknown code IDs": "0",
    }
    return ProfilePayload(
        start_time_ms=0,
        end_time_ms=60_000,
        sampler_mode=0,
        extra_metadata=diagnostics,
        sources={spec.source_id: spec.source_id for spec in PLUGIN_SPECS},
        class_sources={f"[Python] {spec.module}": spec.source_id for spec in PLUGIN_SPECS},
        threads=[ThreadTree(name="Server thread", nodes=nodes, times=[100.0], children_refs=roots)],
    )


class PythonMultiPluginValidationTest(unittest.TestCase):
    def test_multi_plugin_controller_requires_canonical_disabled_bstats(self) -> None:
        self.assertTrue(PythonMultiPluginValidation.disable_bstats)

    def test_multi_plugin_controller_requires_exact_spark_sha(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(Path, "cwd", return_value=Path(temp)),
            self.assertRaisesRegex(ValueError, "full 40-character commit SHA"),
        ):
            PythonMultiPluginValidation("not-an-exact-sha")

    def test_multi_plugin_controller_requires_exact_bds_version(self) -> None:
        self.assertEqual(_required_bds_version("1.26.44.3"), ("1.26.44.3", "26.44"))
        with self.assertRaisesRegex(ValueError, "EXPECTED_BDS_VERSION"):
            _required_bds_version("26.44")
        with self.assertRaisesRegex(RuntimeError, "protocol version mismatch"):
            _validate_exact_bds_evidence(
                {"bds_version": "25.0"},
                ["Version: 1.26.44.3"],
                "1.26.44.3",
                "26.44",
            )

    def test_two_distinct_identities_and_chains_pass(self) -> None:
        report = validate_multi_plugin_profile(valid_profile())

        self.assertTrue(report["assertions"]["both_identities"])
        self.assertTrue(report["assertions"]["both_source_chains"])
        self.assertTrue(report["assertions"]["distinct_sources"])
        self.assertEqual(set(report["identities"]), {spec.name for spec in PLUGIN_SPECS})

    def test_normalized_source_names_are_accepted_without_merging_plugins(self) -> None:
        profile = valid_profile()
        profile.sources = {
            spec.source_id.replace("-", "_"): spec.source_id.replace("-", "_") for spec in PLUGIN_SPECS
        }
        profile.class_sources = {
            f"[Python] {spec.module}": spec.source_id.replace("-", "_") for spec in PLUGIN_SPECS
        }

        report = validate_multi_plugin_profile(profile)

        self.assertTrue(report["assertions"]["distinct_sources"])
        self.assertEqual(
            {item["observed_class_source"] for item in report["identities"].values()},
            {spec.source_id.replace("-", "_") for spec in PLUGIN_SPECS},
        )

    def test_missing_plugin_identity_is_rejected(self) -> None:
        profile = valid_profile()
        spec = PLUGIN_SPECS[1]
        profile.threads[0].nodes = profile.threads[0].nodes[:3]
        profile.threads[0].children_refs = [0, 3]
        profile.class_sources.pop(f"[Python] {spec.module}")
        profile.sources.pop(spec.source_id)

        with self.assertRaisesRegex(RuntimeError, "profile contains no nodes for plugin-b"):
            validate_multi_plugin_profile(profile)

    def test_cross_attribution_is_rejected(self) -> None:
        profile = valid_profile()
        first, second = PLUGIN_SPECS
        profile.class_sources[f"[Python] {first.module}"] = second.source_id

        with self.assertRaisesRegex(RuntimeError, "plugin-a class source mismatch"):
            validate_multi_plugin_profile(profile)

    def test_cross_plugin_method_chain_is_rejected(self) -> None:
        profile = valid_profile()
        for node in profile.threads[0].nodes[:3]:
            node.method_name = node.method_name.replace("PluginA", "PluginB")
        for node in profile.threads[0].nodes[3:6]:
            node.method_name = node.method_name.replace("PluginB", "PluginA")

        with self.assertRaisesRegex(RuntimeError, "missing plugin-a Python chain"):
            validate_multi_plugin_profile(profile)

    def test_ambiguous_source_identity_is_rejected(self) -> None:
        profile = valid_profile()
        spec = PLUGIN_SPECS[0]
        profile.sources[f"{spec.source_id}-copy"] = spec.source_id

        with self.assertRaisesRegex(RuntimeError, "plugin-a source identity is ambiguous"):
            validate_multi_plugin_profile(profile)

    def test_observer_frame_leak_is_rejected(self) -> None:
        profile = valid_profile()
        profile.threads[0].nodes.append(Node(method_name="pyStartThunk", times=[1.0]))

        with self.assertRaisesRegex(RuntimeError, "observer frames leaked"):
            validate_multi_plugin_profile(profile)

    def test_callback_failure_is_rejected(self) -> None:
        profile = valid_profile()
        profile.extra_metadata["Python shadow overflows"] = "1"

        with self.assertRaisesRegex(RuntimeError, "callback/shadow failures"):
            validate_multi_plugin_profile(profile)

    def test_native_only_attribution_is_rejected(self) -> None:
        profile = valid_profile()
        profile.extra_metadata["Python attribution backend"] = '"native-only"'
        profile.extra_metadata["Python function attribution enabled"] = "false"

        with self.assertRaisesRegex(RuntimeError, "attribution is not active"):
            validate_multi_plugin_profile(profile)


class PythonMultiPluginWorkflowTest(unittest.TestCase):
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "python-attribution-multi-plugin.yml"

    def test_workflow_is_linux_exact_sha_and_fail_closed(self) -> None:
        text = self.workflow.read_text(encoding="utf-8")
        self.assertIn("spark_sha:", text)
        self.assertIn("bds_version:", text)
        self.assertIn("endstone_sha:", text)
        self.assertIn("runs-on: ubuntu-24.04", text)
        self.assertIn("--spark-sha \"$EXPECTED_SPARK_SHA\"", text)
        self.assertIn("--bds-version \"$EXPECTED_BDS_VERSION\"", text)
        self.assertIn("EXPECTED_BDS_PROTOCOL_VERSION", text)
        self.assertIn("if: always()", text)
        self.assertIn("verify_bstats_evidence", text)
        self.assertIn("actions/upload-artifact@v7", text)
        controller = (self.workflow.parents[2] / "controller" / "python_multi_plugin_validation.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("spark-python-attribution-plugin-a", controller)
        self.assertIn("spark-python-attribution-plugin-b", controller)


if __name__ == "__main__":
    unittest.main()
