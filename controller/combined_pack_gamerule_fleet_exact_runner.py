from __future__ import annotations

import os

from controller.combined_pack_gamerule_fleet_validation import (
    CombinedPackGameruleFleetValidation,
    main,
)
from controller.python_evidence_provenance import (
    validate_bds_version,
    validate_component_provenance,
    validate_endstone_runtime_version,
)

_ORIGINAL_INSTALL_ARTIFACTS = CombinedPackGameruleFleetValidation.install_artifacts
_ORIGINAL_START_SERVER = CombinedPackGameruleFleetValidation.start_server


def _install_exact_artifacts(self: CombinedPackGameruleFleetValidation) -> None:
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


def _start_exact_server(self: CombinedPackGameruleFleetValidation) -> None:
    _ORIGINAL_START_SERVER(self)
    assert self.server is not None
    observed_protocol = validate_bds_version(self.result, self.server.snapshot())
    self.check(
        "exact-bds-version",
        "PASS",
        observed_protocol=observed_protocol,
        expected_protocol=os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION", "").strip() or None,
        expected_full=os.environ.get("EXPECTED_BDS_VERSION", "").strip() or None,
    )


CombinedPackGameruleFleetValidation.install_artifacts = _install_exact_artifacts
CombinedPackGameruleFleetValidation.start_server = _start_exact_server

if __name__ == "__main__":
    raise SystemExit(main())
