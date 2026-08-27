#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import time
import traceback

from controller.run_test import IntegrationTest, locate_one, run_checked
from providers.artifact_provider import _download_artifact, _metadata, discover, save_metadata

ENDSTONE_RUN_ID = 32992839821
ENDSTONE_SHA = "c76c814289ee3be8a7236389b6bdeb5728b154e4"
ENDSTONE_ARTIFACT = {
    "id": 9616071559,
    "name": "endstone-0.11.10.dev387-windows-x86_64.zip",
    "size_in_bytes": 25671796,
    "expires_at": "2026-11-24T17:13:20Z",
}


def install_pinned_artifacts(test: IntegrationTest) -> None:
    expected_spark_sha = os.environ.get("EXPECTED_SPARK_SHA", "").strip()
    if len(expected_spark_sha) != 40:
        raise RuntimeError("EXPECTED_SPARK_SHA must be an exact 40-character SHA")

    endstone_run = {
        "id": ENDSTONE_RUN_ID,
        "head_branch": "develop",
        "head_sha": ENDSTONE_SHA,
        "html_url": f"https://github.com/EndstoneMC/endstone/actions/runs/{ENDSTONE_RUN_ID}",
        "name": "Build",
        "event": "push",
        "created_at": "2026-08-26T17:13:20Z",
    }
    metadata: dict[str, object] = {"platform": "windows", "components": {}}
    components = metadata["components"]
    assert isinstance(components, dict)

    endstone_info = _metadata("endstone", "EndstoneMC/endstone", endstone_run, ENDSTONE_ARTIFACT)
    components["endstone"] = endstone_info
    save_metadata(metadata, test.metadata_path)
    endstone_payload = _download_artifact("EndstoneMC/endstone", ENDSTONE_ARTIFACT, test.downloads / "endstone")
    endstone_info["payload_dir"] = str(endstone_payload)
    save_metadata(metadata, test.metadata_path)
    print(
        f"[artifact] endstone: {ENDSTONE_SHA} run={ENDSTONE_RUN_ID} "
        f"artifact={ENDSTONE_ARTIFACT['name']}",
        flush=True,
    )

    spark_run, spark_artifact = discover("spark", "windows", expected_sha=expected_spark_sha)
    spark_info = _metadata("spark", "ReallocAll/spark", spark_run, spark_artifact)
    components["spark"] = spark_info
    save_metadata(metadata, test.metadata_path)
    spark_payload = _download_artifact("ReallocAll/spark", spark_artifact, test.downloads / "spark")
    spark_info["payload_dir"] = str(spark_payload)
    save_metadata(metadata, test.metadata_path)
    print(
        f"[artifact] spark: {spark_info['sha']} run={spark_info['run_id']} "
        f"artifact={spark_info['artifact']['name']}",
        flush=True,
    )

    test.metadata = metadata
    test.check("artifact-discovery", "PASS", f"Endstone {ENDSTONE_SHA}; Spark {expected_spark_sha}")

    wheel = locate_one(test.downloads / "endstone" / "payload", ["endstone-*-cp313-cp313-*.whl", "endstone-*.whl"])
    test.check("endstone-wheel-located", "PASS", str(wheel.relative_to(test.root)))
    run_checked(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall", str(wheel)],
        timeout=300,
    )

    spark_root = test.downloads / "spark" / "payload"
    spark_binary = locate_one(spark_root, ["endstone_spark.dll"])
    plugin_dir = test.server_dir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    target = plugin_dir / spark_binary.name
    shutil.copy2(spark_binary, target)
    test.check("spark-plugin-deployed", "PASS", str(target.relative_to(test.root)))
    allocation_shim = locate_one(spark_root, ["spark_allocation_shim.dll"])
    shim_target = plugin_dir / allocation_shim.name
    shutil.copy2(allocation_shim, shim_target)
    test.check("spark-allocation-shim-deployed", "PASS", str(shim_target.relative_to(test.root)))


def spark_fully_enabled(lines: list[str]) -> bool:
    return any(
        "spark" in line.lower()
        and "enabled" in line.lower()
        and "enabling" not in line.lower()
        for line in lines
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=12)
    args = parser.parse_args()
    if args.cycles < 1:
        raise SystemExit("cycles must be positive")

    test = IntegrationTest("windows")
    output = pathlib.Path("bootstrap-stress-results.json")
    result: dict[str, object] = {
        "platform": "windows",
        "cycles_requested": args.cycles,
        "cycles_completed": 0,
        "status": "running",
        "endstone_sha": ENDSTONE_SHA,
        "spark_sha": os.environ.get("EXPECTED_SPARK_SHA", "").strip(),
        "cycles": [],
    }

    try:
        install_pinned_artifacts(test)
        for cycle in range(1, args.cycles + 1):
            started = time.monotonic()
            test.start_server()
            assert test.server is not None
            test.server.wait_for(spark_fully_enabled, 30, "completed Spark enable")
            enabled = time.monotonic()
            if cycle == 1 and test.result.get("bds_version") != "1.26.44.3":
                raise RuntimeError(f"unexpected BDS version: {test.result.get('bds_version')!r}")
            stop_started = time.monotonic()
            graceful = test.server.graceful_stop(60)
            stop_finished = time.monotonic()
            lines = test.server.snapshot()
            if not graceful:
                test.server.force_kill_tree()
                raise RuntimeError(f"cycle {cycle}: BDS did not stop gracefully within 60s")
            test.server.close()
            test.server = None
            leftovers = test.residual_processes()
            if leftovers:
                raise RuntimeError(f"cycle {cycle}: residual BDS process: {' | '.join(leftovers[:5])}")

            cycle_result = {
                "cycle": cycle,
                "startup_seconds": round(enabled - started, 3),
                "shutdown_seconds": round(stop_finished - stop_started, 3),
                "server_stop_requested": any("server stop requested" in line.lower() for line in lines),
                "spark_disabled": any("[spark] disabling spark" in line.lower() for line in lines),
                "quit_correctly": any("quit correctly" in line.lower() for line in lines),
            }
            cycles = result["cycles"]
            assert isinstance(cycles, list)
            cycles.append(cycle_result)
            result["cycles_completed"] = cycle
            output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
            print(json.dumps(cycle_result, sort_keys=True), flush=True)
            time.sleep(0.5)

        result["status"] = "PASS"
        output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        return 0
    except Exception as exc:
        result["status"] = "FAIL"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        try:
            if test.server is not None and test.server.is_alive():
                test.server.force_kill_tree()
                test.server.close()
                test.server = None
        finally:
            output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        return 1
    finally:
        test.split_logs()


if __name__ == "__main__":
    raise SystemExit(main())
