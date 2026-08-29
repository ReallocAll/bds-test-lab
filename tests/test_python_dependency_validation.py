from __future__ import annotations

import unittest
from pathlib import Path

from controller.python_dependency_validation import (
    DEPENDENCY_SCENARIO,
    OBSERVER_THUNKS,
    validate_user_ctypes_branches,
)
from controller.python_profile_payload import Node, ProfilePayload, ThreadTree


MODULE = "[Python] endstone_spark_python_dependency_test"


def profile_with_branches(*branches: list[tuple[str, str]]) -> ProfilePayload:
    nodes: list[Node] = []
    roots: list[int] = []
    for branch in branches:
        previous: int | None = None
        for method_name, class_name in branch:
            node = Node(method_name=method_name, class_name=class_name)
            index = len(nodes)
            nodes.append(node)
            if previous is None:
                roots.append(index)
            else:
                nodes[previous].children_refs.append(index)
            previous = index
    return ProfilePayload(threads=[ThreadTree(name="main", nodes=nodes, children_refs=roots)])


USER_BRANCH = [
    ("DependencyPlugin.dependency_tick", MODULE),
    ("_ctypes_callproc", ""),
    ("ffi_call_unix64", ""),
    ("usleep", ""),
]


class PythonDependencyBridgeOracleTest(unittest.TestCase):
    def test_valid_user_branch(self) -> None:
        report = validate_user_ctypes_branches(profile_with_branches(USER_BRANCH))

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["ctypes_node_count"], 1)
        self.assertEqual(report["ffi_node_count"], 1)
        self.assertEqual(report["valid_branch_occurrence_count"], 2)
        self.assertEqual(report["failures"], [])

    def test_extra_observer_associated_bridge_branch_fails(self) -> None:
        observer_branch = [
            (OBSERVER_THUNKS[-1], ""),
            ("_ctypes_callproc", ""),
            ("ffi_call_unix64", ""),
            ("usleep", ""),
        ]

        report = validate_user_ctypes_branches(profile_with_branches(USER_BRANCH, observer_branch))

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any(OBSERVER_THUNKS[-1] in failure["path"] for failure in report["failures"]))

    def test_missing_usleep_target_fails(self) -> None:
        missing_target = USER_BRANCH[:-1] + [("nanosleep", "")]

        report = validate_user_ctypes_branches(profile_with_branches(missing_target))

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("usleep" in failure["missing"] for failure in report["failures"]))

    def test_semantic_workflow_uses_mixed_actions_metadata(self) -> None:
        workflow = Path(__file__).parents[1] / ".github" / "workflows" / "python-native-bridge-semantics.yml"

        self.assertEqual(DEPENDENCY_SCENARIO, "mixed-actions")
        self.assertIn("BDS_TEST_BOT_SCENARIO_FILE: bot-src/scenarios/mixed-actions.json", workflow.read_text())
        self.assertIn("python-attribution-bots-linux-1-mixed-actions.log", workflow.read_text())


if __name__ == "__main__":
    unittest.main()
