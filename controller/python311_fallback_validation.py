#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import shutil

from controller import run_test
from controller.python_attribution_validation import PythonAttributionValidation
from providers import artifact_provider

ENDSTONE_RUN_ID = 32992839821
ENDSTONE_SHA = "c76c814289ee3be8a7236389b6bdeb5728b154e4"
ENDSTONE_ARTIFACT_ID = 9616014885
ENDSTONE_ARTIFACT_NAME = "endstone-0.11.10.dev387-cp311-cp311-manylinux_2_31_x86_64.whl"

_original_resolve = run_test.resolve_artifacts


def _resolve_with_cp311(
    platform_name: str,
    output_dir: pathlib.Path | str = "downloads",
    metadata_path: pathlib.Path | str = "metadata.json",
    spark_sha: str | None = None,
):
    if platform_name != "linux":
        raise ValueError("Python 3.11 fallback E2E currently targets Linux")
    result = _original_resolve(platform_name, output_dir, metadata_path, spark_sha)
    root = pathlib.Path(output_dir) / "endstone"
    shutil.rmtree(root, ignore_errors=True)
    run = artifact_provider._get_json(f"/repos/EndstoneMC/endstone/actions/runs/{ENDSTONE_RUN_ID}")
    if run.get("head_sha") != ENDSTONE_SHA or run.get("conclusion") != "success":
        raise RuntimeError(f"unexpected Endstone run state: {run.get('head_sha')} / {run.get('conclusion')}")
    artifacts = artifact_provider._get_json(
        f"/repos/EndstoneMC/endstone/actions/runs/{ENDSTONE_RUN_ID}/artifacts?per_page=100"
    ).get("artifacts") or []
    artifact = next(
        (
            item
            for item in artifacts
            if int(item.get("id", 0)) == ENDSTONE_ARTIFACT_ID and item.get("name") == ENDSTONE_ARTIFACT_NAME
        ),
        None,
    )
    if artifact is None or artifact.get("expired"):
        raise RuntimeError("exact Endstone cp311 artifact is missing or expired")
    payload = artifact_provider._download_artifact("EndstoneMC/endstone", artifact, root)
    result["components"]["endstone"] = artifact_provider._metadata(
        "endstone", "EndstoneMC/endstone", run, artifact
    )
    result["components"]["endstone"]["payload_dir"] = str(payload)
    artifact_provider.save_metadata(result, metadata_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-seconds", type=int, default=60)
    args = parser.parse_args()
    run_test.resolve_artifacts = _resolve_with_cp311
    validator = PythonAttributionValidation(
        "linux",
        None,
        0,
        "idle",
        "off",
        args.profile_seconds,
    )
    code = validator.execute()
    validator.result["expected_endstone_sha"] = ENDSTONE_SHA
    validator.result["expected_endstone_run_id"] = ENDSTONE_RUN_ID
    validator.result["expected_endstone_artifact_id"] = ENDSTONE_ARTIFACT_ID
    validator.result["expected_endstone_artifact_name"] = ENDSTONE_ARTIFACT_NAME
    validator._write_results()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
