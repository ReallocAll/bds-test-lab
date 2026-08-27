#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import pathlib

from controller.bot_validation import patch_server_properties
from controller.fleet_spark_validation import set_server_property
from controller.profiler_overhead_validation import BOT_COUNT, WORLD_NAME, ProfilerOverheadValidation


class ResilientProfilerOverheadValidation(ProfilerOverheadValidation):
    """Keep benchmark isolation progressing even when a BDS transition refuses graceful shutdown."""

    def _stop_server(self) -> None:
        if self.server is None:
            return
        graceful = self.server.graceful_stop(20)
        if not graceful:
            self.server.force_kill_tree()
            self.check(
                "benchmark-transition-forced-stop",
                "WARN",
                "BDS did not exit within 20s after stop; force-killed after the measurement/profile had already finalized",
            )
        self.server.close()
        self.server = None

    def bootstrap_world(self) -> None:
        # Provision/configure the benchmark world without Spark. This keeps a Spark
        # shutdown anomaly from being mistaken for a benchmark harness bootstrap failure.
        self.set_spark_enabled(False)
        self.start_server_mode(False)
        self._stop_server()

        properties = self.server_dir / "server.properties"
        patch_server_properties(properties)
        set_server_property(properties, "max-players", "30")
        set_server_property(properties, "level-name", WORLD_NAME)
        set_server_property(properties, "allow-cheats", "true")
        set_server_property(properties, "player-idle-timeout", "0")
        set_server_property(properties, "view-distance", "12")
        set_server_property(properties, "tick-distance", "6")

        self.start_server_mode(False)
        assert self.server is not None
        for command in (
            "gamerule domobspawning false",
            "gamerule dodaylightcycle false",
            "gamerule mobgriefing false",
            "gamerule domobloot false",
            "difficulty normal",
            "time set night",
        ):
            self.command_check("bootstrap-" + command.split()[0] + "-" + str(len(self.result["checks"])), command)
        self._stop_server()
        self.check("benchmark-world-bootstrap", "PASS", "fixed world and gamerules prepared with Spark disabled")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--window-seconds", type=int, default=int(os.environ.get("BENCH_WINDOW_SECONDS", "20")))
    parser.add_argument("--repeats", type=int, default=int(os.environ.get("BENCH_REPEATS", "3")))
    args = parser.parse_args()
    validator = ResilientProfilerOverheadValidation(
        args.platform,
        pathlib.Path(args.bot),
        args.window_seconds,
        args.repeats,
    )
    return validator.execute_benchmark()


if __name__ == "__main__":
    raise SystemExit(main())
