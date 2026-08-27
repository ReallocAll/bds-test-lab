#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import pathlib
import time
from typing import Any

from controller.bot_validation import patch_server_properties
from controller.fleet_spark_validation import set_server_property
from controller.profiler_overhead_validation import (
    BOT_COUNT,
    TIME_VALUE_RE,
    WORLD_NAME,
    ProfilerOverheadValidation,
    median,
    pct,
)


class ResilientProfilerOverheadValidation(ProfilerOverheadValidation):
    """Keep benchmark isolation progressing while preserving measurement validity."""

    def __init__(self, platform_name: str, bot_binary: pathlib.Path, window_seconds: int, repeats: int) -> None:
        self._last_fleet_stop_at: float | None = None
        super().__init__(platform_name, bot_binary, window_seconds, repeats)
        self.result["measurement_method"].update(
            {
                "tick_alignment": (
                    "gametime is captured immediately when the matching BDS command response appears; "
                    "the generic 1s command-output stability wait is intentionally bypassed"
                ),
                "profile_metrics": "Spark TPS/MSPT/CPU is captured before profiler stop while profiling is still active",
            }
        )
        self._write_results()

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

    def query_gametime(self) -> int:
        """Return the tick value as soon as BDS emits the matching command response.

        ServerProcess.wait_command_output intentionally waits for one second of stable
        output. That is desirable for general commands but biases a timed benchmark by
        making the starting tick sample about one second older than wall_start.
        """
        assert self.server is not None
        start_index = self.server.command("time query gametime")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            lines = self.server.snapshot()[start_index:]
            for line in reversed(lines):
                match = TIME_VALUE_RE.search(line)
                if match:
                    return int(match.group(1))
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while querying gametime")
            time.sleep(0.01)
        raise RuntimeError(
            "Unable to parse /time query gametime output within 5s: "
            + " | ".join(self.server.snapshot()[start_index:][-20:])
        )

    def _discard_failed_fleet(self) -> None:
        if self.bot is None:
            return
        try:
            self.bot.terminate(5)
        except Exception:
            pass
        self.bot = None
        if self.server is not None and self.server.is_alive():
            try:
                self.wait_player_count(0, timeout=10)
            except Exception:
                pass
        self._last_fleet_stop_at = time.monotonic()

    def start_load(self, scenario: str) -> None:
        self.scenario = scenario
        self.bot_log = self.root / f"profiler-overhead-{self.platform}-{scenario}.log"

        # BDS/RakNet can temporarily throttle rapid reconnect bursts from the same
        # loopback address. Preserve OFF/ON isolation while avoiding a false Spark
        # regression caused by immediately reconnecting 20 clients after a prior fleet.
        cooldown = 30.0 if self.platform == "windows" else 10.0
        if self._last_fleet_stop_at is not None:
            remaining = cooldown - (time.monotonic() - self._last_fleet_stop_at)
            if remaining > 0:
                time.sleep(remaining)

        for attempt in range(2):
            try:
                self.start_fleet()
                time.sleep(15)
                self.wait_player_count(BOT_COUNT, timeout=15)
                return
            except RuntimeError as exc:
                text = str(exc)
                retryable = "Bot exited with code" in text or "dial raknet" in text or "fleet_online" in text
                if attempt != 0 or not retryable:
                    raise
                self._discard_failed_fleet()
                retry_delay = 30.0 if self.platform == "windows" else 10.0
                self.check(
                    "fleet-connect-retry",
                    "WARN",
                    f"initial 20-bot reconnect failed ({text[:400]}); retrying after network cooldown",
                    retry_delay_seconds=retry_delay,
                )
                time.sleep(retry_delay)

    def stop_load(self) -> None:
        if self.bot is None:
            return
        try:
            self.stop_fleet()
        finally:
            self.bot = None
            self._last_fleet_stop_at = time.monotonic()

    def measure_phase(
        self,
        name: str,
        *,
        spark_enabled: bool,
        load: str,
        profiler: str | None = None,
        settle_seconds: int = 10,
    ) -> dict[str, Any]:
        assert self.server is not None
        if settle_seconds:
            time.sleep(settle_seconds)

        profile_start = None
        if profiler:
            command = "spark profiler start"
            if profiler == "allocation":
                command += " --alloc"
            profile_start = self.server.command(command)
            output = self.server.wait_command_output(profile_start, 5)
            joined = "\n".join(output).lower()
            if "couldn't start" in joined or "isn't available" in joined or "not available" in joined:
                raise RuntimeError(f"Unable to start {profiler} profiler: " + " | ".join(output[-30:]))
            time.sleep(5)

        windows: list[dict[str, Any]] = []
        viewer_url: str | None = None
        metrics_during: dict[str, Any] | None = None
        try:
            for _ in range(self.repeats):
                windows.append(self.measure_once())
            if spark_enabled:
                # Capture the rolling MSPT/CPU window before profiler stop/upload. This
                # makes the secondary metrics describe the profiled interval itself.
                metrics_during = self.spark_metrics()
        finally:
            if profiler and self.server is not None and self.server.is_alive():
                stop_at = self.server.command("spark profiler stop")
                deadline = time.monotonic() + 75
                while time.monotonic() < deadline:
                    viewer_url = self._viewer_url(self.server.snapshot(), min(profile_start or stop_at, stop_at))
                    if viewer_url:
                        break
                    time.sleep(0.5)
                if not viewer_url:
                    raise RuntimeError(f"{profiler} profiler produced no viewer URL")

        cpu_values = [float(w["cpu_ms_per_tick"]) for w in windows]
        wall_values = [float(w["wall_ms_per_tick"]) for w in windows]
        tick_rates = [float(w["tick_rate"]) for w in windows]
        phase: dict[str, Any] = {
            "name": name,
            "spark_enabled": spark_enabled,
            "load": load,
            "profiler": profiler or "off",
            "viewer_url": viewer_url,
            "windows": windows,
            "cpu_ms_per_tick": {
                "median": median(cpu_values),
                "min": min(cpu_values),
                "max": max(cpu_values),
                "p95": pct(cpu_values, 0.95),
            },
            "wall_ms_per_tick": {
                "median": median(wall_values),
                "min": min(wall_values),
                "max": max(wall_values),
            },
            "tick_rate": {
                "median": median(tick_rates),
                "min": min(tick_rates),
                "max": max(tick_rates),
            },
            "spark_metrics": metrics_during,
        }
        self.result["phases"].append(phase)
        if viewer_url:
            self.result["profiles"].append({"phase": name, "kind": profiler, "url": viewer_url})
        self._write_results()
        self.check(
            "benchmark-" + name,
            "PASS",
            f"{name}: median CPU {phase['cpu_ms_per_tick']['median']:.4f} ms/tick, "
            f"wall {phase['wall_ms_per_tick']['median']:.4f} ms/tick",
            viewer_url=viewer_url,
        )
        return phase


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
