#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib

# Importing the runner installs the real Endstone hotspot plugin deployment
# override before the benchmark class is instantiated.
from controller.python_attribution_performance_runner import PythonAttributionPerformance


class FocusedPythonAttributionPerformance(PythonAttributionPerformance):
    def __init__(self, platform_name: str, bot_binary: pathlib.Path, benchmark_mode: str, duration_seconds: int, bot_count: int) -> None:
        super().__init__(platform_name, bot_binary, benchmark_mode, max(180, duration_seconds), bot_count)
        # The standard benchmark intentionally enforces a long 180 s window for
        # statistical runs. This focused run exists only to obtain current-SHA
        # profile/event-volume evidence before making the first optimization.
        self.duration_seconds = max(60, duration_seconds)
        self.result["duration_seconds"] = self.duration_seconds
        self.result["focused_evidence"] = True
        self._write_results()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--mode", required=True, choices=["shadow", "full"])
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()
    validator = FocusedPythonAttributionPerformance(
        args.platform,
        pathlib.Path(args.bot),
        args.mode,
        args.duration_seconds,
        args.count,
    )
    code = validator.execute()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
