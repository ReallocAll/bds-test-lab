#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib

import controller.python_attribution_validation as base_validation
from controller.python_profile_payload import contains_python_chain, parse_sampler_data, python_nodes


DEPENDENCY_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "spark-python-dependency-test"
DEPENDENCY_MODULE = "endstone_spark_python_dependency_test"
DEPENDENCY_PLUGIN_SOURCE = "spark-python-dependency-test"

# Reuse the complete real-BDS lifecycle/viewer/raw validation while substituting
# only the dedicated plugin fixture and its expected source metadata.
base_validation.PLUGIN_SOURCE = DEPENDENCY_SOURCE
base_validation.EXPECTED_MODULE = DEPENDENCY_MODULE
base_validation.EXPECTED_SOURCE = DEPENDENCY_PLUGIN_SOURCE


class PythonDependencyValidation(base_validation.PythonAttributionValidation):
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
