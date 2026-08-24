#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_platform_results(root: pathlib.Path) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for path in root.rglob("test-results.json"):
        try:
            data = load_json(path)
        except Exception:
            continue
        platform = data.get("platform")
        if platform in {"linux", "windows"}:
            results[str(platform)] = data
    return results


def find_metadata(root: pathlib.Path) -> dict[str, dict[str, Any]]:
    component_metadata: dict[str, dict[str, Any]] = {}
    for path in root.rglob("metadata.json"):
        try:
            data = load_json(path)
        except Exception:
            continue
        for component, info in (data.get("components") or {}).items():
            if component in {"endstone", "spark"} and component not in component_metadata:
                component_metadata[component] = info
    return component_metadata


def platform_record(platform: str, data: dict[str, Any] | None) -> dict[str, Any]:
    if data is None:
        return {
            "status": "FAIL",
            "failed_stage": "job-output-missing",
            "error_summary": f"No test-results.json was produced for {platform}",
            "execution_profile_viewer_url": None,
            "allocation_profile_viewer_url": None,
            "shutdown_status": "unknown",
            "bds_version": None,
        }
    return {
        "status": data.get("status", "FAIL"),
        "failed_stage": data.get("failed_stage"),
        "error_summary": data.get("error_summary"),
        "execution_profile_viewer_url": data.get("execution_profile_viewer_url"),
        "allocation_profile_viewer_url": data.get("allocation_profile_viewer_url"),
        "shutdown_status": data.get("shutdown_status"),
        "bds_version": data.get("bds_version"),
        "checks": data.get("checks", []),
    }


def component_record(info: dict[str, Any] | None) -> dict[str, Any] | None:
    if not info:
        return None
    artifact = info.get("artifact") or {}
    return {
        "sha": info.get("sha"),
        "run_id": info.get("run_id"),
        "run_url": info.get("run_url"),
        "artifact": artifact.get("name"),
        "artifact_id": artifact.get("id"),
        "workflow": info.get("workflow"),
    }


def build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Latest BDS integration test",
        "",
        f"- Lab commit: `{report['lab_commit']}`",
        f"- State: **{report['state']}**",
        f"- Completed: `{report['completed_at']}`",
        "",
        "## Versions",
        "",
        "| Component | SHA | Actions | Artifact |",
        "|---|---|---|---|",
    ]
    for component in ("endstone", "spark"):
        info = report["components"].get(component)
        if info:
            action = f"[{info.get('run_id')}]({info.get('run_url')})" if info.get("run_url") else str(info.get("run_id") or "")
            lines.append(f"| {component.capitalize()} | `{info.get('sha') or ''}` | {action} | `{info.get('artifact') or ''}` |")
        else:
            lines.append(f"| {component.capitalize()} |  |  |  |")
    lines += [
        "",
        "## Platforms",
        "",
        "| Platform | Result | BDS | Failed stage | Shutdown | Execution profile | Allocation profile |",
        "|---|---|---|---|---|---|---|",
    ]
    for platform in ("windows", "linux"):
        item = report["platforms"][platform]
        execution = item.get("execution_profile_viewer_url") or ""
        allocation = item.get("allocation_profile_viewer_url") or ""
        if execution:
            execution = f"[viewer]({execution})"
        if allocation:
            allocation = f"[viewer]({allocation})"
        lines.append(f"| {platform.capitalize()} | **{item.get('status')}** | `{item.get('bds_version') or ''}` | `{item.get('failed_stage') or ''}` | `{item.get('shutdown_status') or ''}` | {execution} | {allocation} |")
        if item.get("error_summary"):
            lines += ["", f"**{platform.capitalize()} error:** `{item['error_summary']}`"]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab-commit", required=True)
    parser.add_argument("--input-root", default="collected")
    parser.add_argument("--output-dir", default="reports")
    args = parser.parse_args()

    root = pathlib.Path(args.input_root)
    platform_results = find_platform_results(root)
    metadata = find_metadata(root)
    report = {
        "schema_version": 1,
        "state": "completed",
        "lab_commit": args.lab_commit,
        "components": {
            "endstone": component_record(metadata.get("endstone")),
            "spark": component_record(metadata.get("spark")),
        },
        "platforms": {
            "windows": platform_record("windows", platform_results.get("windows")),
            "linux": platform_record("linux", platform_results.get("linux")),
        },
        "completed_at": now_iso(),
    }
    versions = {value.get("bds_version") for value in report["platforms"].values() if value.get("bds_version")}
    report["bds_version"] = next(iter(versions)) if len(versions) == 1 else sorted(versions)

    out_dir = pathlib.Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "latest.md").write_text(build_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
