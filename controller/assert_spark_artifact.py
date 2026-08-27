#!/usr/bin/env python3
from __future__ import annotations

import argparse

from providers.artifact_provider import discover


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--component", choices=["spark", "endstone"], default="spark")
    args = parser.parse_args()

    run, artifact = discover(args.component, args.platform, expected_sha=args.expected_sha)
    actual = str(run.get("head_sha") or "")
    label = "Spark" if args.component == "spark" else "Endstone"
    if actual != args.expected_sha:
        raise SystemExit(
            f"Resolved {label} artifact for {args.platform} is {actual}, expected {args.expected_sha}; "
            "benchmark/release validation must not use stale artifacts"
        )
    print(
        f"{label} artifact preflight PASS: platform={args.platform} sha={actual} "
        f"run={run.get('id')} artifact={artifact.get('name')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
