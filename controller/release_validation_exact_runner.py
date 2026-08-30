from __future__ import annotations

import os

from controller.python_evidence_provenance import validate_bds_version
from controller.release_validation import SparkReleaseValidation, main


def _verify_exact_bds_version(self: SparkReleaseValidation) -> None:
    assert self.server is not None
    observed_protocol = validate_bds_version(self.result, self.server.snapshot())
    self.check(
        "bds-exact-version",
        "PASS",
        observed_protocol=observed_protocol,
        expected_protocol=os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION", "").strip() or None,
        expected_full=os.environ.get("EXPECTED_BDS_VERSION", "").strip() or None,
    )


SparkReleaseValidation.verify_bds_version = _verify_exact_bds_version

if __name__ == "__main__":
    raise SystemExit(main())
