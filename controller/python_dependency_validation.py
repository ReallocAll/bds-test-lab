#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys

import controller.python_attribution_validation as base_validation
from controller.python_profile_payload import contains_python_chain, parse_sampler_data, python_nodes
from controller.run_test import IntegrationTest, run_checked


DEPENDENCY_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "spark-python-dependency-test"
DEPENDENCY_MODULE = "endstone_spark_python_dependency_test"
DEPENDENCY_PLUGIN_SOURCE = "spark-python-dependency-test"
DEPENDENCY_INSTALLED_SOURCE = "spark_python_dependency_test"
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
    return value == name or value.startswith(name + "(")


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
            raise RuntimeError(f"Spark PEP 669 observer callbacks leaked into visible profile tree: {observer_nodes}")
        self.check("python-observer-thunks-filtered", "PASS", observer_nodes=0)

        ctypes_nodes = [node.method_name for node in nodes if matches_function(node.method_name, "_ctypes_callproc")]
        ffi_nodes = [
            node.method_name
            for node in nodes
            if any(
                matches_function(node.method_name, name)
                for name in ("ffi_call", "ffi_call_int", "ffi_call_unix64", "ffi_call_win64")
            )
        ]
        if not ctypes_nodes or not ffi_nodes:
            raise RuntimeError(
                "normal user ctypes/libffi path was not retained in the real profile: "
                f"ctypes={ctypes_nodes}, ffi={ffi_nodes}"
            )
        self.check(
            "python-user-ctypes-retained",
            "PASS",
            ctypes_nodes=len(ctypes_nodes),
            ffi_nodes=len(ffi_nodes),
            ctypes_methods=sorted(set(ctypes_nodes)),
            ffi_methods=sorted(set(ffi_nodes)),
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
        "chunk-walk",
        "dependency",
        args.profile_seconds,
    )
    return validation.execute()


if __name__ == "__main__":
    raise SystemExit(main())
