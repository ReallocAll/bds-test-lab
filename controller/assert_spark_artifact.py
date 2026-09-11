#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os

from providers.artifact_provider import discover


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-run-id", default=os.environ.get("EXPECTED_SPARK_RUN_ID") or None)
    parser.add_argument("--expected-artifact-id", default=os.environ.get("EXPECTED_SPARK_ARTIFACT_ID") or None)
    parser.add_argument("--expected-artifact-digest", default=os.environ.get("EXPECTED_SPARK_ARTIFACT_DIGEST") or None)
    args = parser.parse_args()

    run, artifact = discover(
        "spark", args.platform, expected_sha=args.expected_sha,
        expected_run_id=args.expected_run_id, expected_artifact_id=args.expected_artifact_id,
        expected_artifact_digest=args.expected_artifact_digest,
    )
    actual = str(run.get("head_sha") or "")
    if actual != args.expected_sha:
        raise SystemExit(
            f"Resolved Spark artifact for {args.platform} is {actual}, expected {args.expected_sha}; "
            "release validation must not use stale artifacts"
        )
    print(
        f"Spark artifact preflight PASS: platform={args.platform} sha={actual} "
        f"run={run.get('id')} artifact={artifact.get('name')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
