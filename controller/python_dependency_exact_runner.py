from __future__ import annotations

import controller.python_attribution_exact_runner  # noqa: F401
from controller.python_dependency_validation import PythonDependencyValidation, main
from controller.python_evidence_provenance import (
    validate_component_provenance,
    validate_endstone_runtime_version,
)

_ORIGINAL_INSTALL_ARTIFACTS = PythonDependencyValidation.install_artifacts


def _install_exact_artifacts(self: PythonDependencyValidation) -> None:
    _ORIGINAL_INSTALL_ARTIFACTS(self)
    spark = validate_component_provenance(self.metadata, "spark")
    endstone = validate_component_provenance(self.metadata, "endstone")
    runtime_version = validate_endstone_runtime_version()
    self.check(
        "exact-artifact-provenance",
        "PASS",
        spark_sha=spark.get("sha"),
        spark_run_id=spark.get("run_id"),
        endstone_sha=endstone.get("sha"),
        endstone_run_id=endstone.get("run_id"),
        endstone_artifact_id=(endstone.get("artifact") or {}).get("id"),
        endstone_runtime_version=runtime_version,
    )


PythonDependencyValidation.install_artifacts = _install_exact_artifacts

if __name__ == "__main__":
    raise SystemExit(main())
