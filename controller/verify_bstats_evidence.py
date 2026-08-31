#!/usr/bin/env python3
"""Fail-closed verification for opt-in bStats workflow artifacts."""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

from controller.bstats import (
    B_STATS_EVIDENCE_PATH,
    BStatsConfigError,
    inspect_bstats_config,
)


def verify_result(result_path: pathlib.Path) -> dict[str, Any]:
    result_path = pathlib.Path(result_path)
    if result_path.is_symlink() or not result_path.is_file():
        raise BStatsConfigError(f"bStats result is missing or symlinked: {result_path}")
    root = result_path.parent
    if root.is_symlink():
        raise BStatsConfigError(f"bStats result directory is a symlink: {root}")
    try:
        result_path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise BStatsConfigError(f"bStats result path escapes its directory: {result_path}") from exc
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BStatsConfigError(f"unable to read bStats result: {result_path}") from exc
    if not isinstance(result, dict):
        raise BStatsConfigError(f"bStats result is not a JSON object: {result_path}")
    expected = result.get("bstats_config")
    if not isinstance(expected, dict):
        raise BStatsConfigError(f"bStats metadata is missing from result: {result_path}")
    evidence_path = root / B_STATS_EVIDENCE_PATH
    try:
        evidence_path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise BStatsConfigError(f"bStats evidence path escapes its directory: {evidence_path}") from exc
    observed = inspect_bstats_config(evidence_path)
    if expected != observed:
        raise BStatsConfigError(f"bStats metadata does not match evidence: {result_path}")
    return observed


def verify_results(result_paths: list[pathlib.Path]) -> None:
    if not result_paths:
        raise BStatsConfigError("at least one bStats result is required")
    for result_path in result_paths:
        evidence = verify_result(result_path)
        print(
            f"Verified bStats disabled evidence for {result_path}: "
            f"{evidence['evidence_path']} sha256={evidence['sha256']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", dest="results", action="append", required=True)
    args = parser.parse_args()
    try:
        verify_results([pathlib.Path(result) for result in args.results])
    except BStatsConfigError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
