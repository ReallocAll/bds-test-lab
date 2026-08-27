#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import time
import traceback

from controller.run_test import IntegrationTest


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
        "cycles": [],
    }

    try:
        test.install_artifacts()
        for cycle in range(1, args.cycles + 1):
            started = time.monotonic()
            test.start_server()
            enabled = time.monotonic()
            assert test.server is not None
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
            result["cycles"].append(cycle_result)  # type: ignore[union-attr]
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
