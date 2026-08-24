#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def collect(root: pathlib.Path) -> dict[str, dict[str, Any]]:
    platforms: dict[str, dict[str, Any]] = {}
    for result_path in root.rglob("test-results.json"):
        try:
            result = load(result_path)
        except Exception:
            continue
        platform = str(result.get("platform", ""))
        if platform not in {"linux", "windows"}:
            continue
        metadata_path = result_path.parent / "metadata.json"
        metadata: dict[str, Any] = {}
        if metadata_path.exists():
            try:
                metadata = load(metadata_path)
            except Exception:
                metadata = {}
        platforms[platform] = {"result": result, "metadata": metadata}
    return platforms


def component(platforms: dict[str, dict[str, Any]], name: str) -> dict[str, Any] | None:
    records: dict[str, dict[str, Any]] = {}
    for platform, bundle in platforms.items():
        info = ((bundle.get("metadata") or {}).get("components") or {}).get(name)
        if not info:
            continue
        artifact = info.get("artifact") or {}
        records[platform] = {
            "sha": info.get("sha"),
            "run_id": info.get("run_id"),
            "run_url": info.get("run_url"),
            "workflow": info.get("workflow"),
            "artifact": artifact.get("name"),
            "artifact_id": artifact.get("id"),
        }
    if not records:
        return None
    shas = {record.get("sha") for record in records.values() if record.get("sha")}
    run_ids = {record.get("run_id") for record in records.values() if record.get("run_id")}
    run_urls = {record.get("run_url") for record in records.values() if record.get("run_url")}
    return {
        "sha": next(iter(shas)) if len(shas) == 1 else sorted(shas),
        "run_id": next(iter(run_ids)) if len(run_ids) == 1 else sorted(run_ids),
        "run_url": next(iter(run_urls)) if len(run_urls) == 1 else None,
        "platforms": records,
    }


def platform_record(bundle: dict[str, Any] | None) -> dict[str, Any]:
    if not bundle:
        return {
            "status": "FAIL",
            "failed_stage": "job-output-missing",
            "error_summary": "No integration result was produced",
            "profiles": [],
        }
    result = bundle.get("result") or {}
    return {
        "status": result.get("status", "FAIL"),
        "failed_stage": result.get("failed_stage"),
        "error_summary": result.get("error_summary"),
        "bds_version": result.get("bds_version"),
        "shutdown_status": result.get("shutdown_status"),
        "checks": result.get("checks", []),
        "profiles": result.get("profiles", []),
        "recovery": result.get("recovery"),
        "soak": result.get("soak"),
        "execution_profile_viewer_url": result.get("execution_profile_viewer_url"),
        "allocation_profile_viewer_url": result.get("allocation_profile_viewer_url"),
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Latest BDS integration test",
        "",
        f"- Lab commit: `{report['lab_commit']}`",
        f"- Lab Actions: [{report['lab_run_id']}]({report['lab_run_url']})",
        f"- State: **{report['state']}**",
        f"- Spark SHA: `{report.get('spark_sha') or ''}`",
        f"- Endstone SHA: `{report.get('endstone_sha') or ''}`",
        f"- Completed: `{report['completed_at']}`",
        "",
        "## Platforms",
        "",
        "| Platform | Result | BDS | Shutdown | Crash replay | Soak | Execution | Allocation | Recovery |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for platform in ("windows", "linux"):
        item = report["platforms"][platform]
        profiles = {p.get("kind"): p for p in item.get("profiles", []) if isinstance(p, dict)}
        def link(kind: str) -> str:
            url = (profiles.get(kind) or {}).get("url")
            return f"[viewer]({url})" if url else ""
        recovery = item.get("recovery") or {}
        soak = item.get("soak") or {}
        lines.append(
            f"| {platform.capitalize()} | **{item.get('status')}** | `{item.get('bds_version') or ''}` | "
            f"`{item.get('shutdown_status') or ''}` | `{recovery.get('crash_replay') or ''}` | "
            f"`{soak.get('duration_minutes') or ''}m` | {link('execution')} | {link('allocation')} | {link('crash-recovery')} |"
        )
        if item.get("error_summary"):
            lines.extend(["", f"**{platform.capitalize()} error:** `{item['error_summary']}`"])
        if soak.get("samples"):
            lines.extend([
                "",
                f"- {platform} soak RSS: start `{soak.get('rss_start_bytes')}`, end `{soak.get('rss_end_bytes')}`, peak `{soak.get('rss_peak_bytes')}`",
                f"- {platform} soak threads: start `{soak.get('threads_start')}`, end `{soak.get('threads_end')}`, peak `{soak.get('threads_peak')}`",
            ])
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab-commit", required=True)
    parser.add_argument("--lab-run-id", required=True)
    parser.add_argument("--lab-run-url", required=True)
    parser.add_argument("--input-root", default="collected")
    parser.add_argument("--output-dir", default="reports")
    args = parser.parse_args()

    collected = collect(pathlib.Path(args.input_root))
    components = {
        "spark": component(collected, "spark"),
        "endstone": component(collected, "endstone"),
    }
    platforms = {
        "windows": platform_record(collected.get("windows")),
        "linux": platform_record(collected.get("linux")),
    }
    state = "PASS" if all(item.get("status") == "PASS" for item in platforms.values()) else "FAIL"
    report = {
        "schema_version": 2,
        "state": state,
        "lab_commit": args.lab_commit,
        "lab_run_id": args.lab_run_id,
        "lab_run_url": args.lab_run_url,
        "spark_sha": (components["spark"] or {}).get("sha"),
        "endstone_sha": (components["endstone"] or {}).get("sha"),
        "components": components,
        "platforms": platforms,
        "completed_at": now_iso(),
    }
    for item in report["platforms"].values():
        item["actions_url"] = args.lab_run_url

    output = pathlib.Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "latest.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    (output / "latest.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if state == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
