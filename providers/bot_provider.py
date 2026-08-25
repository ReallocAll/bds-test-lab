#!/usr/bin/env python3
"""Resolve a successful bds-test-bot build artifact for BDS E2E validation."""

from __future__ import annotations

import json
import pathlib
import urllib.parse
from typing import Any

from providers.artifact_provider import (
    ArtifactResolutionError,
    _download_artifact,
    _get_json,
)

BOT_REPO = "ReallocAll/bds-test-bot"
BOT_WORKFLOW = "build.yml"
ARTIFACTS = {
    "linux": ("bds-test-bot-linux-amd64", "bds-test-bot"),
    "windows": ("bds-test-bot-windows-amd64", "bds-test-bot.exe"),
}


def _select_artifact(run: dict[str, Any], artifact_name: str) -> dict[str, Any] | None:
    data = _get_json(f"/repos/{BOT_REPO}/actions/runs/{run['id']}/artifacts?per_page=100")
    for artifact in data.get("artifacts") or []:
        if artifact.get("name") == artifact_name and not artifact.get("expired"):
            return artifact
    return None


def resolve_bot(
    platform_name: str,
    ref: str,
    output_dir: pathlib.Path | str = "downloads/bot",
    metadata_path: pathlib.Path | str = "bot-metadata.json",
    expected_sha: str | None = None,
) -> dict[str, Any]:
    if platform_name not in ARTIFACTS:
        raise ValueError(f"Unsupported platform: {platform_name}")
    if not ref:
        raise ValueError("Bot ref must not be empty")

    artifact_name, binary_name = ARTIFACTS[platform_name]
    query = urllib.parse.urlencode({"branch": ref, "status": "success", "per_page": 100})
    runs = _get_json(
        f"/repos/{BOT_REPO}/actions/workflows/{BOT_WORKFLOW}/runs?{query}"
    ).get("workflow_runs") or []

    candidates = [
        run
        for run in runs
        if run.get("head_branch") == ref
        and run.get("conclusion") == "success"
        and (expected_sha is None or run.get("head_sha") == expected_sha)
    ]
    # Prefer push runs because their checkout is the exact branch head rather than
    # GitHub's synthetic pull-request merge ref. Newest run wins within each class.
    candidates.sort(
        key=lambda run: (run.get("event") == "push", int(run.get("id") or 0)),
        reverse=True,
    )

    for run in candidates:
        artifact = _select_artifact(run, artifact_name)
        if artifact is None:
            continue
        payload = _download_artifact(BOT_REPO, artifact, pathlib.Path(output_dir))
        matches = sorted(path for path in payload.rglob(binary_name) if path.is_file())
        if not matches:
            raise ArtifactResolutionError(
                f"Artifact {artifact_name} from run {run['id']} did not contain {binary_name}"
            )
        binary = matches[0]
        result = {
            "repository": BOT_REPO,
            "ref": ref,
            "sha": run.get("head_sha"),
            "run_id": run.get("id"),
            "run_url": run.get("html_url"),
            "workflow": run.get("name"),
            "event": run.get("event"),
            "artifact": {
                "id": artifact.get("id"),
                "name": artifact.get("name"),
                "size_in_bytes": artifact.get("size_in_bytes"),
                "expires_at": artifact.get("expires_at"),
            },
            "binary": str(binary),
        }
        pathlib.Path(metadata_path).write_text(
            json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(
            f"[bot-artifact] sha={result['sha']} run={result['run_id']} "
            f"artifact={artifact_name}"
        )
        return result

    sha_note = f" sha={expected_sha}" if expected_sha else ""
    raise ArtifactResolutionError(
        f"No successful {BOT_REPO} {BOT_WORKFLOW} run for ref={ref}{sha_note} "
        f"with artifact {artifact_name}"
    )
