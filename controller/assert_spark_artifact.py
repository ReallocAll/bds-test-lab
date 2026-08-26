#!/usr/bin/env python3
from __future__ import annotations

import argparse

from providers.artifact_provider import discover


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()

    run, artifact = discover("spark", args.platform, expected_sha=args.expected_sha)
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
