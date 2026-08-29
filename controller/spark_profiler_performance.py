#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
from typing import Any

from controller.python_profile_payload import fetch_viewer_payload, parse_sampler_data, profile_summary
from controller.spark_idle_performance import SparkIdlePerformance, _cpu_totals


PROFILE_COMMANDS = {
    "default": "spark profiler start --interval 4",
    "one-ms": "spark profiler start --interval 1",
    "all-thread": "spark profiler start --thread * --interval 4",
    "stress-all-thread": "spark profiler start --thread * --interval 1",
    "only-ticks-over": "spark profiler start --interval 4 --only-ticks-over 5",
    "allocation": "spark profiler start --alloc",
    "alloc-live-only": "spark profiler start --alloc --alloc-live-only",
}


class SparkProfilerPerformance(SparkIdlePerformance):
    def __init__(
        self,
        platform_name: str,
        bot_binary: pathlib.Path,
        profiler_mode: str,
        duration_seconds: int,
        bot_count: int,
    ) -> None:
        super().__init__(platform_name, bot_binary, "monitoring", duration_seconds, bot_count)
        self.profiler_mode = profiler_mode
        self.profile_path = self.root / f"spark-profiler-{profiler_mode}.sparkprofile"
        self.result.update(
            {
                "test_kind": "spark-profiler-performance",
                "benchmark_mode": profiler_mode,
                "duration_seconds": self.duration_seconds,
                "profile": None,
                "viewer_url": None,
            }
        )
        self._write_results()

    def configure_environment(self) -> None:
        # Prevent the configured background 10 ms profiler from contaminating the
        # explicit foreground session. Keep the normal allocation-rate metric and
        # automatic Python attribution lifecycle so these runs reflect foreground
        # profiler behavior under Spark's normal monitoring configuration.
        os.environ["SPARK_BACKGROUNDPROFILER"] = "false"
        os.environ["SPARK_ALLOCATIONRATEMETRICS"] = "true"
        os.environ["SPARK_PYTHON_ATTRIBUTION_MODE"] = "auto"

    def start_profile(self) -> int:
        assert self.server is not None
        command = PROFILE_COMMANDS[self.profiler_mode]
        start_index = self.server.command(command)
        output = self.server.wait_command_output(start_index, 8)
        text = "\n".join(output).lower()
        if "couldn't start" in text or "not available" in text or "isn't available" in text or "error" in text:
            raise RuntimeError("Profiler start failed: " + " | ".join(output[-30:]))
        return start_index

    def stop_profile(self, start_index: int) -> tuple[str, dict[str, object]]:
        assert self.server is not None
        stop_index = self.server.command("spark profiler stop")
        deadline = time.monotonic() + 120
        viewer_url = None
        while time.monotonic() < deadline:
            viewer_url = self._viewer_url(self.server.snapshot(), min(start_index, stop_index))
            if viewer_url:
                break
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while finalizing profiler")
            time.sleep(0.5)
        if not viewer_url:
            raise RuntimeError("Profiler produced no Spark viewer URL")
        raw = fetch_viewer_payload(viewer_url)
        if not raw:
            raise RuntimeError("Spark viewer profile payload is empty")
        self.profile_path.write_bytes(raw)
        parsed = parse_sampler_data(raw)
        summary = profile_summary(parsed)
        if float(summary.get("duration_seconds", 0.0)) <= 0.0 or int(summary.get("thread_count", 0)) <= 0:
            raise RuntimeError(f"Spark viewer payload is not a usable non-empty profile: {summary}")
        return viewer_url, summary

    def measure_active_profile(self) -> dict[str, Any]:
        assert self.server is not None
        time.sleep(5)
        tick_start = self.query_gametime()
        cpu_start = self.process_cpu_seconds()
        ctx_start = self.process_context_switches()
        faults_start = self.process_page_faults()
        system_start = _cpu_totals(__import__("psutil").cpu_times())
        wall_start = time.monotonic()
        rss: list[int] = []
        deadline = wall_start + self.duration_seconds
        while time.monotonic() < deadline:
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during active profiler window")
            rss.append(self.process_rss_bytes())
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        wall_end = time.monotonic()
        cpu_end = self.process_cpu_seconds()
        ctx_end = self.process_context_switches()
        faults_end = self.process_page_faults()
        psutil = __import__("psutil")
        system_end = _cpu_totals(psutil.cpu_times())
        tick_end = self.query_gametime()
        ticks = tick_end - tick_start
        if ticks <= 0:
            raise RuntimeError(f"Non-positive Bedrock gametime delta: {tick_start} -> {tick_end}")
        wall_seconds = wall_end - wall_start
        cpu_seconds = max(0.0, cpu_end - cpu_start)
        total_delta = max(0.0, system_end[0] - system_start[0])
        busy_delta = max(0.0, system_end[1] - system_start[1])
        return {
            "ticks": ticks,
            "wall_seconds": wall_seconds,
            "cpu_seconds": cpu_seconds,
            "process_cpu_percent_of_one_core": cpu_seconds / wall_seconds * 100.0,
            "cpu_ms_per_tick": cpu_seconds * 1000.0 / ticks,
            "observed_tps": ticks / wall_seconds,
            "system_cpu_percent": busy_delta / total_delta * 100.0 if total_delta else None,
            "rss_mean_bytes": int(sum(rss) / len(rss)) if rss else self.process_rss_bytes(),
            "rss_peak_bytes": max(rss) if rss else self.process_rss_bytes(),
            "context_switches": {
                "voluntary": max(0, ctx_end[0] - ctx_start[0]),
                "involuntary": max(0, ctx_end[1] - ctx_start[1]),
                "voluntary_per_second": max(0, ctx_end[0] - ctx_start[0]) / wall_seconds,
                "involuntary_per_second": max(0, ctx_end[1] - ctx_start[1]) / wall_seconds,
            },
            "page_faults": {
                "minor": max(0, faults_end[0] - faults_start[0]),
                "major": max(0, faults_end[1] - faults_start[1]),
            },
        }

    def execute(self) -> int:
        stage = "initialization"
        try:
            self.configure_environment()
            stage = "artifact-install"
            self.install_artifacts()
            stage = "server-bootstrap"
            self.bootstrap_offline_server()
            stage = "spark-sanity"
            self.run_basic_commands()
            stage = "fleet-connect"
            self.start_fleet()
            stage = "fleet-settle"
            time.sleep(20)
            stage = "profile-start"
            profile_start = self.start_profile()
            stage = "performance-window"
            self.result["performance"] = self.measure_active_profile()
            self._write_results()
            stage = "profile-stop"
            viewer_url, summary = self.stop_profile(profile_start)
            self.result["viewer_url"] = viewer_url
            self.result["profile"] = summary
            self._write_results()
            stage = "fleet-disconnect"
            self.stop_fleet()
            stage = "shutdown"
            self.shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self._write_results()
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "failed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"
            self._write_results()
            try:
                if self.server is not None and self.server.is_alive():
                    self.server.command("spark profiler stop")
                    time.sleep(2)
            except Exception:
                pass
            try:
                self.stop_fleet()
            except Exception:
                pass
            try:
                self.shutdown()
            except Exception:
                pass
            return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--mode", required=True, choices=sorted(PROFILE_COMMANDS))
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()
    validator = SparkProfilerPerformance(
        args.platform,
        pathlib.Path(args.bot),
        args.mode,
        max(30, args.duration_seconds),
        args.count,
    )
    code = validator.execute()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
