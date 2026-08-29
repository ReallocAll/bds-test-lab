#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
from typing import Any


def describe(values: list[float]) -> dict[str, float]:
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "mean": mean,
        "median": statistics.median(values),
        "stddev": sd,
        "cv_percent": (sd / mean * 100.0) if mean else 0.0,
        "min": min(values),
        "max": max(values),
    }


def paired_delta(
    metrics: dict[tuple[int, str, str], dict[str, Any]],
    mode: str,
    field: str,
    left: str,
    right: str,
    repetitions: int,
) -> dict[str, Any] | None:
    values: list[float] = []
    for rep in range(1, repetitions + 1):
        left_value = metrics.get((rep, mode, left), {}).get(field)
        right_value = metrics.get((rep, mode, right), {}).get(field)
        if left_value is None or right_value is None:
            continue
        values.append(float(right_value) - float(left_value))
    if len(values) != repetitions:
        return None
    return {"values": values, **describe(values)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("evidence"))
    parser.add_argument("--parent-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--modes", nargs="+", default=["off", "shadow", "full"])
    parser.add_argument("--output", type=pathlib.Path, default=None)
    args = parser.parse_args()

    root: pathlib.Path = args.root
    output = args.output or root / "paired-summary.json"
    status_path = root / "case-status.tsv"
    if not status_path.is_file():
        raise SystemExit("missing case-status.tsv")

    problems: list[str] = []
    rows: list[tuple[int, str, str, str, str, int, str]] = []
    for line in status_path.read_text(encoding="utf-8").splitlines()[1:]:
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            problems.append(f"malformed status row: {line!r}")
            continue
        rep, mode, target, sha, label, exit_code, status = fields
        rows.append((int(rep), mode, target, sha, label, int(exit_code), status))

    expected = {
        (rep, mode, target)
        for rep in range(1, args.repetitions + 1)
        for mode in args.modes
        for target in ("parent", "candidate")
    }
    actual = {(row[0], row[1], row[2]) for row in rows}
    if actual != expected:
        problems.append(f"case set mismatch missing={sorted(expected-actual)} extra={sorted(actual-expected)}")

    expected_sha = {"parent": args.parent_sha, "candidate": args.candidate_sha}
    observed_spark: dict[str, set[str]] = {"parent": set(), "candidate": set()}
    endstone_shas: set[str] = set()
    metrics: dict[tuple[int, str, str], dict[str, Any]] = {}

    for rep, mode, target, sha, label, exit_code, status in rows:
        case = root / label
        result_path = case / "python-attribution-performance.json"
        meta_path = case / "metadata.json"
        profile_path = case / "python-attribution-performance.sparkprofile"
        if exit_code != 0 or status != "PASS":
            problems.append(f"{label}: exit={exit_code} status={status}")
            continue
        if sha != expected_sha.get(target):
            problems.append(f"{label}: status SHA {sha} != expected {expected_sha.get(target)}")
        if not result_path.is_file() or not meta_path.is_file():
            problems.append(f"{label}: missing result or metadata")
            continue

        data = json.loads(result_path.read_text(encoding="utf-8"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        components = meta.get("components", {})
        spark_sha = str((components.get("spark") or {}).get("sha") or "")
        endstone_sha = str((components.get("endstone") or {}).get("sha") or "")
        observed_spark[target].add(spark_sha)
        endstone_shas.add(endstone_sha)
        if spark_sha != expected_sha[target]:
            problems.append(f"{label}: observed Spark SHA {spark_sha} != expected {expected_sha[target]}")

        perf = data.get("performance") or {}
        tick = perf.get("tick_statistics") or {}
        profile = perf.get("profile_summary")
        viewer = perf.get("viewer_url")
        if mode == "full":
            if not viewer:
                problems.append(f"{label}: full mode produced no viewer URL")
            if not profile_path.is_file() or profile_path.stat().st_size == 0:
                problems.append(f"{label}: full mode profile is missing or empty")
            if not profile:
                problems.append(f"{label}: full mode profile summary missing")

        metrics[(rep, mode, target)] = {
            "cpu": float(perf["process_cpu_percent_of_one_core"]),
            "cpu_ms_per_tick": float(perf["cpu_ms_per_tick"]),
            "mspt_mean": float(tick["mspt_mean"]),
            "mspt_p95": float(tick["mspt_p95"]),
            "rss_mean_bytes": float(perf["rss_mean_bytes"]),
            "viewer_url": viewer,
            "profile_summary": profile,
            "shadow": perf.get("shadow_only_diagnostics"),
        }

    for target, expected_value in expected_sha.items():
        if observed_spark[target] != {expected_value}:
            problems.append(f"{target} Spark SHA set mismatch: {sorted(observed_spark[target])}")
    if len(endstone_shas) != 1:
        problems.append(f"Endstone SHA drift across paired run: {sorted(endstone_shas)}")

    summary: dict[str, Any] = {
        "parent_sha": args.parent_sha,
        "candidate_sha": args.candidate_sha,
        "repetitions": args.repetitions,
        "modes": args.modes,
        "endstone_shas": sorted(endstone_shas),
        "metrics": {},
        "viewers": [],
        "shadow_diagnostics": {},
    }

    scalar_fields = ("cpu", "cpu_ms_per_tick", "mspt_mean", "mspt_p95", "rss_mean_bytes")
    for mode in args.modes:
        mode_summary: dict[str, Any] = {}
        for target in ("parent", "candidate"):
            target_summary: dict[str, Any] = {}
            for field in scalar_fields:
                values = [
                    float(metrics[(rep, mode, target)][field])
                    for rep in range(1, args.repetitions + 1)
                    if (rep, mode, target) in metrics
                ]
                if len(values) == args.repetitions:
                    target_summary[field] = describe(values)
            mode_summary[target] = target_summary

        deltas: dict[str, Any] = {}
        for field in scalar_fields:
            delta = paired_delta(metrics, mode, field, "parent", "candidate", args.repetitions)
            if delta is not None:
                deltas[f"candidate_minus_parent_{field}"] = delta
        mode_summary["paired_deltas"] = deltas
        summary["metrics"][mode] = mode_summary

        for rep in range(1, args.repetitions + 1):
            for target in ("parent", "candidate"):
                item = metrics.get((rep, mode, target))
                if item and item.get("viewer_url"):
                    summary["viewers"].append(
                        {
                            "rep": rep,
                            "mode": mode,
                            "target": target,
                            "sha": expected_sha[target],
                            "url": item["viewer_url"],
                            "profile_summary": item.get("profile_summary"),
                        }
                    )

    # Use the unaffected attribution-off mode as a per-repetition control for
    # environmental drift. Negative values mean the candidate reduced the
    # incremental Python-attribution overhead relative to the parent.
    for measured_mode in ("shadow", "full"):
        if "off" not in args.modes or measured_mode not in args.modes:
            continue
        for field in ("cpu", "cpu_ms_per_tick", "mspt_mean"):
            did: list[float] = []
            for rep in range(1, args.repetitions + 1):
                po = metrics.get((rep, "off", "parent"))
                pm = metrics.get((rep, measured_mode, "parent"))
                co = metrics.get((rep, "off", "candidate"))
                cm = metrics.get((rep, measured_mode, "candidate"))
                if po and pm and co and cm:
                    did.append((float(cm[field]) - float(co[field])) - (float(pm[field]) - float(po[field])))
            if len(did) == args.repetitions:
                summary.setdefault("difference_in_differences", {}).setdefault(measured_mode, {})[field] = {
                    "values": did,
                    **describe(did),
                }

    for target in ("parent", "candidate"):
        diagnostics: dict[str, list[float]] = {
            "pushes_per_second": [],
            "pops_per_second": [],
            "snapshot_attempts_delta": [],
            "snapshot_failures_delta": [],
            "attributed_samples_delta": [],
            "native_only_samples_delta": [],
            "cache_hits_delta": [],
            "cache_misses_delta": [],
            "code_objects": [],
        }
        for rep in range(1, args.repetitions + 1):
            shadow = metrics.get((rep, "shadow", target), {}).get("shadow")
            if not isinstance(shadow, dict):
                continue
            for key in diagnostics:
                value = shadow.get(key)
                if value is not None:
                    diagnostics[key].append(float(value))
        summary["shadow_diagnostics"][target] = {
            key: describe(values)
            for key, values in diagnostics.items()
            if len(values) == args.repetitions
        }

    output.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    if problems:
        print("Evidence validation failed:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
