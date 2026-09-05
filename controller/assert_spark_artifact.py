#!/usr/bin/env python3
from __future__ import annotations

import argparse

from providers.artifact_provider import discover


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-workflow", default=None)
    parser.add_argument("--artifact-name-prefix", default=None)
    args = parser.parse_args()

    run, artifact = discover(
        "spark",
        args.platform,
        expected_sha=args.expected_sha,
        expected_workflow=args.expected_workflow,
        artifact_name_prefix=args.artifact_name_prefix,
    )
    actual = str(run.get("head_sha") or "")
    if actual != args.expected_sha:
        raise SystemExit(
            f"Resolved Spark artifact for {args.platform} is {actual}, expected {args.expected_sha}; "
            "release validation must not use stale artifacts"
        )
    if args.expected_workflow and str(run.get("name") or "") != args.expected_workflow:
        raise SystemExit(
            f"Resolved Spark workflow is {run.get('name')!r}, expected {args.expected_workflow!r}"
        )
    artifact_name = str(artifact.get("name") or "")
    if args.artifact_name_prefix and not artifact_name.startswith(args.artifact_name_prefix):
        raise SystemExit(
            f"Resolved Spark artifact is {artifact_name!r}, expected prefix {args.artifact_name_prefix!r}"
        )
    print(
        f"Spark artifact preflight PASS: platform={args.platform} sha={actual} "
        f"workflow={run.get('name')} run={run.get('id')} artifact={artifact_name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
