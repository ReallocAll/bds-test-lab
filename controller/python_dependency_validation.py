#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys

import controller.python_attribution_validation as base_validation
from controller.python_profile_payload import (
    Node,
    ProfilePayload,
    contains_python_chain,
    iter_leaf_paths,
    parse_sampler_data,
    python_nodes,
)
from controller.run_test import IntegrationTest, run_checked


DEPENDENCY_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "spark-python-dependency-test"
DEPENDENCY_MODULE = "endstone_spark_python_dependency_test"
DEPENDENCY_PLUGIN_SOURCE = "spark-python-dependency-test"
DEPENDENCY_INSTALLED_SOURCE = "spark_python_dependency_test"
DEPENDENCY_SCENARIO = "mixed-actions"
DEPENDENCY_TICK = "DependencyPlugin.dependency_tick"
CTYPES_CALLPROC = "_ctypes_callproc"
FFI_CALLS = ("ffi_call", "ffi_call_int", "ffi_call_unix64", "ffi_call_win64")
USLEEP_TARGETS = ("usleep", "__GI_usleep")
OBSERVER_THUNKS = (
    "spark::endstone_adapter::EndstonePythonAttribution::pyStartThunk",
    "spark::endstone_adapter::EndstonePythonAttribution::pyResumeThunk",
    "spark::endstone_adapter::EndstonePythonAttribution::pyThrowThunk",
    "spark::endstone_adapter::EndstonePythonAttribution::pyReturnThunk",
    "spark::endstone_adapter::EndstonePythonAttribution::pyYieldThunk",
    "spark::endstone_adapter::EndstonePythonAttribution::pyUnwindThunk",
    "spark::endstone_adapter::EndstonePythonAttribution::pyStartNativeCallback",
    "spark::endstone_adapter::EndstonePythonAttribution::pyResumeNativeCallback",
    "spark::endstone_adapter::EndstonePythonAttribution::pyThrowNativeCallback",
    "spark::endstone_adapter::EndstonePythonAttribution::pyReturnNativeCallback",
    "spark::endstone_adapter::EndstonePythonAttribution::pyYieldNativeCallback",
    "spark::endstone_adapter::EndstonePythonAttribution::pyUnwindNativeCallback",
    "spark::endstone_adapter::EndstonePythonAttribution::nativeEventCallback",
)

# Reuse the complete real-BDS lifecycle/viewer/raw validation while substituting
# only the dedicated plugin fixture and its expected source metadata.
base_validation.EXPECTED_MODULE = DEPENDENCY_MODULE
base_validation.EXPECTED_SOURCE = DEPENDENCY_INSTALLED_SOURCE


def canonical_plugin_key(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def matches_function(value: str, name: str) -> bool:
    return value == name or value.startswith(name + "(") or value.startswith(name + "@")


def _is_dependency_tick(node: Node) -> bool:
    return node.class_name.startswith("[Python] ") and matches_function(node.method_name, DEPENDENCY_TICK)


def _is_native_function(node: Node, names: tuple[str, ...]) -> bool:
    return not node.class_name.startswith("[Python] ") and any(
        matches_function(node.method_name, name) for name in names
    )


def _bridge_category(node: Node) -> str | None:
    if _is_native_function(node, (CTYPES_CALLPROC,)):
        return "ctypes"
    if _is_native_function(node, FFI_CALLS):
        return "libffi"
    if _is_native_function(node, USLEEP_TARGETS):
        return "usleep"
    if _is_dependency_tick(node):
        return "dependency_tick"
    return None


def _bridge_path_failure(path: tuple[Node, ...], bridge_node: Node) -> dict[str, object]:
    methods = [getattr(node, "method_name", "") for node in path]
    bridge_index = next((index for index, node in enumerate(path) if node is bridge_node), -1)
    category = _bridge_category(bridge_node)
    missing: list[str] = []
    cursor = -1
    if category not in {"ctypes", "libffi"} or bridge_index < 0:
        missing.append("ctypes/libffi bridge node")
    else:
        tick_index = next(
            (
                index
                for index, node in enumerate(path)
                if index < bridge_index and _is_dependency_tick(node)
            ),
            None,
        )
        if tick_index is None:
            missing.append("dependency_tick")
        else:
            cursor = tick_index
    if category == "libffi" and bridge_index >= 0:
        found = next(
            (
                index
                for index, node in enumerate(path)
                if cursor < index < bridge_index and _is_native_function(node, (CTYPES_CALLPROC,))
            ),
            None,
        )
        if found is None:
            missing.append("ctypes")
        else:
            cursor = found

    if category in {"ctypes", "libffi"} and bridge_index >= 0:
        cursor = bridge_index
    if category == "ctypes":
        ffi_index = next(
            (
                index
                for index, node in enumerate(path)
                if index > cursor and _is_native_function(node, FFI_CALLS)
            ),
            None,
        )
        if ffi_index is None:
            missing.append("libffi")
        else:
            cursor = ffi_index
    first_usleep = next(
        (
            index
            for index, node in enumerate(path)
            if _is_native_function(node, USLEEP_TARGETS)
        ),
        None,
    )
    if first_usleep is not None and bridge_index > first_usleep:
        missing.append("bridge before usleep")
    usleep_index = next(
        (
            index
            for index, node in enumerate(path)
            if index > cursor and _is_native_function(node, USLEEP_TARGETS)
        ),
        None,
    )
    if usleep_index is None:
        missing.append("usleep")
    return {"path": methods, "missing": missing}


def validate_user_ctypes_branches(profile: ProfilePayload) -> dict[str, object]:
    """Validate every retained ctypes/libffi node against the user fixture branch."""

    paths = list(iter_leaf_paths(profile))
    occurrences: dict[int, list[tuple[str, tuple[Node, ...]]]] = {}
    bridge_nodes: list[tuple[str, Node]] = []
    ctypes_methods: list[str] = []
    ffi_methods: list[str] = []
    usleep_methods: list[str] = []
    for thread in profile.threads:
        for node in thread.nodes:
            category = _bridge_category(node)
            if category == "ctypes":
                ctypes_methods.append(node.method_name)
            elif category == "libffi":
                ffi_methods.append(node.method_name)
            elif category == "usleep":
                usleep_methods.append(node.method_name)
            if category in {"ctypes", "libffi"}:
                bridge_nodes.append((thread.name, node))
    for thread_name, path in paths:
        for node in path:
            if _bridge_category(node) in {"ctypes", "libffi"}:
                occurrences.setdefault(id(node), []).append((thread_name, path))

    failures: list[dict[str, object]] = []
    valid_occurrences = 0
    for thread_name, node in bridge_nodes:
        node_occurrences = occurrences.get(id(node), [])
        if not node_occurrences:
            failures.append(
                {
                    "thread": thread_name,
                    "node": node.method_name,
                    "path": [],
                    "missing": ["reachable profile-tree ancestry"],
                }
            )
            continue
        for occurrence_thread, path in node_occurrences:
            failure = _bridge_path_failure(path, node)
            if failure["missing"]:
                failure["thread"] = occurrence_thread
                failure["node"] = node.method_name
                failures.append(failure)
            else:
                valid_occurrences += 1

    if not ctypes_methods:
        failures.append({"missing": ["ctypes"], "path": []})
    if not ffi_methods:
        failures.append({"missing": ["libffi"], "path": []})
    return {
        "status": "PASS" if not failures else "FAIL",
        "ctypes_node_count": len(ctypes_methods),
        "ffi_node_count": len(ffi_methods),
        "bridge_node_count": len(bridge_nodes),
        "bridge_occurrence_count": sum(len(occurrences.get(id(node), [])) for _thread, node in bridge_nodes),
        "valid_branch_occurrence_count": valid_occurrences,
        "ctypes_methods": sorted(set(ctypes_methods)),
        "ffi_methods": sorted(set(ffi_methods)),
        "usleep_methods": sorted(set(usleep_methods)),
        "failures": failures,
    }


class PythonDependencyValidation(base_validation.PythonAttributionValidation):
    def install_artifacts(self) -> None:
        # The base attribution installer deliberately expects the hotspot fixture's
        # wheel name. This validation uses a separate real dependency plugin, so
        # install the common Endstone/Spark artifacts and deploy this wheel by its
        # own exact package prefix instead of weakening the main harness checks.
        IntegrationTest.install_artifacts(self)
        wheel_dir = self.root / "dependency-wheel"
        shutil.rmtree(wheel_dir, ignore_errors=True)
        wheel_dir.mkdir(parents=True, exist_ok=True)
        run_checked(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--disable-pip-version-check",
                "--no-deps",
                "--wheel-dir",
                str(wheel_dir),
                str(DEPENDENCY_SOURCE),
            ],
            timeout=180,
        )
        wheels = sorted(wheel_dir.glob("endstone_spark_python_dependency_test-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected one dependency plugin wheel, got: {wheels}")
        plugin_dir = self.server_dir / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        target = plugin_dir / wheels[0].name
        shutil.copy2(wheels[0], target)
        self.check("python-dependency-plugin-installed", "PASS", str(target.relative_to(self.root)))

    def wait_plugin(self) -> None:
        assert self.server is not None
        self.server.wait_for(
            lambda lines: any("spark python dependency test enabled" in line.lower() for line in lines),
            30,
            "Python dependency plugin enable",
        )
        self.check("python-dependency-plugin-enabled", "PASS")

    def validate_profile(self, url: str) -> dict[str, object]:
        summary = super().validate_profile(url)
        profile = parse_sampler_data(self.raw_profile_path.read_bytes())
        expected_chain = ["DependencyPlugin.dependency_tick", "SpecifierSet.contains"]
        if not contains_python_chain(profile, expected_chain):
            raise RuntimeError("real profile did not preserve plugin -> packaging dependency caller relationship")
        self.check("python-dependency-chain", "PASS", chain=" -> ".join(expected_chain))

        packaging_nodes = [
            node
            for _thread, node in python_nodes(profile)
            if node.class_name == "[Python] packaging.specifiers" and node.method_name == "SpecifierSet.contains"
        ]
        if not packaging_nodes:
            raise RuntimeError("real profile contains no packaging.specifiers dependency nodes")
        external = int(profile.extra_metadata.get("Python external code objects", "0"))
        if external <= 0:
            raise RuntimeError(f"real profile did not classify dependency code as external: {external}")
        self.check(
            "python-external-dependency",
            "PASS",
            module="packaging.specifiers",
            method="SpecifierSet.contains",
            external_code_objects=external,
            dependency_nodes=len(packaging_nodes),
        )

        canonical_expected = canonical_plugin_key(DEPENDENCY_PLUGIN_SOURCE)
        installed_sources = [
            key
            for key, display_name in profile.sources.items()
            if canonical_plugin_key(key) == canonical_expected or canonical_plugin_key(display_name) == canonical_expected
        ]
        if len(installed_sources) != 1:
            raise RuntimeError(
                f"expected exactly one installed source matching {DEPENDENCY_PLUGIN_SOURCE!r}, got {installed_sources}; "
                f"all sources={profile.sources}"
            )
        installed_source = installed_sources[0]
        if installed_source == DEPENDENCY_PLUGIN_SOURCE:
            raise RuntimeError(
                "dependency fixture no longer exposes the hyphen/underscore identity mismatch needed by this regression"
            )
        dependency_classes = [
            (class_name, source_id)
            for class_name, source_id in profile.class_sources.items()
            if class_name == f"[Python] {DEPENDENCY_MODULE}"
        ]
        if not dependency_classes:
            raise RuntimeError("profile contains no class-source mapping for the dependency plugin")
        bad_sources = [(class_name, source_id) for class_name, source_id in dependency_classes if source_id != installed_source]
        if bad_sources:
            raise RuntimeError(
                f"Python plugin classes were not reconciled to installed source {installed_source!r}: {bad_sources}"
            )
        if DEPENDENCY_PLUGIN_SOURCE in profile.class_sources.values():
            raise RuntimeError(
                f"raw attribution source {DEPENDENCY_PLUGIN_SOURCE!r} leaked into class_sources beside installed plugin source"
            )
        self.check(
            "python-plugin-canonical-identity",
            "PASS",
            raw_attribution_source=DEPENDENCY_PLUGIN_SOURCE,
            installed_source=installed_source,
            matching_installed_sources=len(installed_sources),
            mapped_classes=len(dependency_classes),
        )

        nodes = [node for thread in profile.threads for node in thread.nodes]
        observer_nodes = [
            node.method_name
            for node in nodes
            if any(matches_function(node.method_name, thunk) for thunk in OBSERVER_THUNKS)
        ]
        if observer_nodes:
            detail = f"Spark PEP 669 observer callbacks leaked into visible profile tree: {observer_nodes}"
            self.check(
                "python-observer-thunks-filtered",
                "FAIL",
                detail,
                observer_nodes=sorted(set(observer_nodes)),
            )
            raise RuntimeError(detail)
        self.check("python-observer-thunks-filtered", "PASS", observer_nodes=0)

        bridge_oracle = validate_user_ctypes_branches(profile)
        if bridge_oracle["status"] != "PASS":
            detail = "invalid user ctypes/libffi branch: " + repr(bridge_oracle["failures"])
            self.check("python-user-ctypes-retained", "FAIL", detail, branch_oracle=bridge_oracle)
            raise RuntimeError(detail)
        self.check(
            "python-user-ctypes-retained",
            "PASS",
            ctypes_nodes=bridge_oracle["ctypes_node_count"],
            ffi_nodes=bridge_oracle["ffi_node_count"],
            ctypes_methods=bridge_oracle["ctypes_methods"],
            ffi_methods=bridge_oracle["ffi_methods"],
            branch_oracle=bridge_oracle,
        )
        return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", type=pathlib.Path, required=True)
    parser.add_argument("--profile-seconds", type=int, default=60)
    args = parser.parse_args()
    validation = PythonDependencyValidation(
        "linux",
        args.bot,
        1,
        DEPENDENCY_SCENARIO,
        "dependency",
        args.profile_seconds,
    )
    return validation.execute()


if __name__ == "__main__":
    raise SystemExit(main())
