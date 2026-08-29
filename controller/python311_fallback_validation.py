#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import zipfile

from controller import run_test
from controller.python_attribution_validation import PLUGIN_SOURCE, PythonAttributionValidation
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


def _repack_wheel_payload(payload: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    # GitHub Actions stores an uploaded wheel artifact as a ZIP whose root is the
    # wheel payload itself (endstone/, endstone.libs/, *.dist-info/) rather than
    # as a ZIP containing the original .whl file. Re-wrap those exact files in a
    # wheel container so pip can install the exact cp311 build artifact.
    dist_info = payload / "endstone-0.11.10.dev387.dist-info"
    if not (dist_info / "WHEEL").is_file() or not (dist_info / "METADATA").is_file():
        raise RuntimeError(f"exact cp311 artifact payload is not a wheel layout: {payload}")
    destination.unlink(missing_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(payload).as_posix())
    return destination


class Python311FallbackValidation(PythonAttributionValidation):
    def install_artifacts(self) -> None:
        self.metadata = _resolve_with_cp311(
            self.platform,
            self.downloads,
            self.metadata_path,
            spark_sha=None,
        )
        self.check("artifact-discovery", "PASS")

        endstone_root = self.downloads / "endstone" / "payload"
        wheel = _repack_wheel_payload(endstone_root, self.root / ENDSTONE_ARTIFACT_NAME)
        self.check(
            "endstone-wheel-located",
            "PASS",
            str(wheel.relative_to(self.root)),
            source_artifact_id=ENDSTONE_ARTIFACT_ID,
            reconstructed_container=True,
        )
        run_test.run_checked(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--force-reinstall",
                str(wheel),
            ],
            timeout=300,
        )

        spark_root = self.downloads / "spark" / "payload"
        spark_binary = run_test.locate_one(spark_root, ["endstone_spark.so"])
        self.server_dir.mkdir(parents=True, exist_ok=True)
        plugin_dir = self.server_dir / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        spark_target = plugin_dir / spark_binary.name
        shutil.copy2(spark_binary, spark_target)
        self.check("spark-plugin-deployed", "PASS", str(spark_target.relative_to(self.root)))

        wheel_dir = self.root / "hotspot-wheel"
        shutil.rmtree(wheel_dir, ignore_errors=True)
        wheel_dir.mkdir(parents=True, exist_ok=True)
        run_test.run_checked(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--disable-pip-version-check",
                "--no-deps",
                "--wheel-dir",
                str(wheel_dir),
                str(PLUGIN_SOURCE),
            ],
            timeout=180,
        )
        hotspot_wheels = sorted(wheel_dir.glob("endstone_spark_python_hotspot_test-*.whl"))
        if len(hotspot_wheels) != 1:
            raise RuntimeError(f"Expected one hotspot plugin wheel, got: {hotspot_wheels}")
        hotspot_target = plugin_dir / hotspot_wheels[0].name
        shutil.copy2(hotspot_wheels[0], hotspot_target)
        self.check("python-hotspot-plugin-installed", "PASS", str(hotspot_target.relative_to(self.root)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-seconds", type=int, default=60)
    args = parser.parse_args()
    validator = Python311FallbackValidation(
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
