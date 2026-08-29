#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import statistics
import sys
import time
from typing import Any

import psutil

from controller.python_attribution_validation import PythonAttributionValidation
from controller.python_profile_payload import fetch_viewer_payload, parse_sampler_data, profile_summary
from controller.run_test import write_json


TIME_VALUE_RE = re.compile(r"(?:game\s*time|time\s+is|time:)\D*(-?\d+)", re.IGNORECASE)
BENCH_RE = re.compile(
    r"Python attribution benchmark: pushes=(\d+) pops=(\d+).*?snapshot_attempts=(\d+) "
    r"snapshot_failures=(\d+) attributed_samples=(\d+) native_only_samples=(\d+) .*?"
    r"cache_hits=(\d+) cache_misses=(\d+) code_objects=(\d+)"
)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    part = index - lower
    return ordered[lower] * (1.0 - part) + ordered[upper] * part


class PythonAttributionPerformance(PythonAttributionValidation):
    def __init__(
        self,
        platform_name: str,
        bot_binary: pathlib.Path,
        benchmark_mode: str,
        duration_seconds: int,
        bot_count: int,
    ) -> None:
        hotspot_mode = "stress" if benchmark_mode == "stress" else "fleet"
        super().__init__(platform_name, bot_binary, bot_count, "chunk-walk", hotspot_mode, max(60, duration_seconds))
        self.benchmark_mode = benchmark_mode
        self.duration_seconds = max(180, duration_seconds)
        self.tick_metrics_path = self.root / "python-attribution-tick-metrics.json"
        self.performance_path = self.root / "python-attribution-performance.json"
        self.profile_path = self.root / "python-attribution-performance.sparkprofile"
        self.measure_start_ns = 0
        self.measure_end_ns = 0
        self.result.update(
            {
                "test_kind": "spark-python-attribution-performance",
                "benchmark_mode": benchmark_mode,
                "duration_seconds": self.duration_seconds,
                "bot_count": bot_count,
                "performance": None,
            }
        )
        self._write_results()

    def _write_results(self) -> None:
        super()._write_results()
        if hasattr(self, "performance_path"):
            write_json(self.performance_path, self.result)

    def _bedrock_processes(self) -> list[psutil.Process]:
        assert self.server is not None and self.server.process is not None
        root = psutil.Process(self.server.process.pid)
        processes = [root]
        try:
            processes.extend(root.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        matches: list[psutil.Process] = []
        for process in processes:
            try:
                name = process.name().lower()
                command = " ".join(process.cmdline()).lower()
                if "bedrock_server" in name or "bedrock_server" in command:
                    matches.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return matches or processes

    def process_cpu_seconds(self) -> float:
        total = 0.0
        for process in self._bedrock_processes():
            try:
                current = process.cpu_times()
                total += float(current.user) + float(current.system)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total

    def process_rss_bytes(self) -> int:
        total = 0
        for process in self._bedrock_processes():
            try:
                total += int(process.memory_info().rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total

    def query_gametime(self) -> int:
        assert self.server is not None
        start = self.server.command("time query gametime")
        output = self.server.wait_command_output(start, 5)
        for line in reversed(output):
            match = TIME_VALUE_RE.search(line)
            if match:
                return int(match.group(1))
            numbers = re.findall(r"-?\d+", line)
            if numbers:
                return int(numbers[-1])
        raise RuntimeError("Unable to parse gametime: " + " | ".join(output[-20:]))

    def start_profiler_if_needed(self) -> int | None:
        if self.benchmark_mode not in {"native", "full", "stress"}:
            return None
        assert self.server is not None
        start = self.server.command("spark profiler start --thread * --interval 4")
        output = self.server.wait_command_output(start, 8)
        joined = "\n".join(output).lower()
        if "couldn't start" in joined or "not available" in joined or "isn't available" in joined:
            raise RuntimeError("Execution profiler start failed: " + " | ".join(output[-30:]))
        return start

    def stop_profiler(self, start_index: int | None) -> tuple[str | None, dict[str, object] | None]:
        if start_index is None:
            return None, None
        assert self.server is not None
        stop_at = self.server.command("spark profiler stop")
        deadline = time.monotonic() + 90
        url: str | None = None
        while time.monotonic() < deadline:
            url = self._viewer_url(self.server.snapshot(), min(start_index, stop_at))
            if url:
                break
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while stopping performance profile")
            time.sleep(0.5)
        if not url:
            raise RuntimeError("Performance execution profile produced no viewer URL")
        raw = fetch_viewer_payload(url)
        self.profile_path.write_bytes(raw)
        profile = parse_sampler_data(raw)
        return url, profile_summary(profile)

    def measure(self) -> tuple[dict[str, Any], int | None]:
        assert self.server is not None
        profiler_start = self.start_profiler_if_needed()
        time.sleep(5)
        tick_start = self.query_gametime()
        cpu_start = self.process_cpu_seconds()
        rss_start = self.process_rss_bytes()
        wall_start = time.monotonic()
        self.measure_start_ns = time.monotonic_ns()
        deadline = wall_start + self.duration_seconds
        rss: list[int] = []
        while time.monotonic() < deadline:
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during Python attribution performance window")
            rss.append(self.process_rss_bytes())
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        self.measure_end_ns = time.monotonic_ns()
        wall_end = time.monotonic()
        cpu_end = self.process_cpu_seconds()
        tick_end = self.query_gametime()
        rss_end = self.process_rss_bytes()
        ticks = tick_end - tick_start
        if ticks <= 0:
            raise RuntimeError(f"Non-positive Bedrock gametime delta: {tick_start} -> {tick_end}")
        wall_seconds = wall_end - wall_start
        cpu_seconds = max(0.0, cpu_end - cpu_start)
        return {
            "ticks": ticks,
            "wall_seconds": wall_seconds,
            "cpu_seconds": cpu_seconds,
            "process_cpu_percent_of_one_core": cpu_seconds / wall_seconds * 100.0,
            "cpu_ms_per_tick": cpu_seconds * 1000.0 / ticks,
            "observed_tps": ticks / wall_seconds,
            "rss_start_bytes": rss_start,
            "rss_end_bytes": rss_end,
            "rss_mean_bytes": int(sum(rss) / len(rss)) if rss else rss_end,
            "rss_peak_bytes": max(rss) if rss else rss_end,
        }, profiler_start

    def tick_statistics(self) -> dict[str, Any]:
        data = json.loads(self.tick_metrics_path.read_text(encoding="utf-8"))
        samples = [
            item
            for item in data.get("samples", [])
            if self.measure_start_ns <= int(item["monotonic_ns"]) <= self.measure_end_ns
        ]
        if len(samples) < 100:
            raise RuntimeError(f"Too few per-tick metrics inside benchmark window: {len(samples)}")
        mspt = [float(item["mspt"]) for item in samples]
        tps = [float(item["tps"]) for item in samples]
        return {
            "samples": len(samples),
            "mspt_mean": statistics.fmean(mspt),
            "mspt_p50": percentile(mspt, 0.50),
            "mspt_p95": percentile(mspt, 0.95),
            "mspt_p99": percentile(mspt, 0.99),
            "mspt_max": max(mspt),
            "tps_mean": statistics.fmean(tps),
            "tps_p50": percentile(tps, 0.50),
            "tps_p05": percentile(tps, 0.05),
            "tps_min": min(tps),
        }

    def shadow_only_diagnostics(self) -> dict[str, Any] | None:
        if self.benchmark_mode != "shadow":
            return None
        assert self.server is not None
        matches = []
        for line in self.server.snapshot():
            match = BENCH_RE.search(line)
            if match:
                matches.append(tuple(int(value) for value in match.groups()))
        if len(matches) < 2:
            return {"records": len(matches), "note": "insufficient benchmark log snapshots"}
        first, last = matches[0], matches[-1]
        elapsed = max(1.0, (len(matches) - 1) * 10.0)
        return {
            "records": len(matches),
            "approx_seconds": elapsed,
            "pushes_per_second": (last[0] - first[0]) / elapsed,
            "pops_per_second": (last[1] - first[1]) / elapsed,
            "snapshot_attempts_delta": last[2] - first[2],
            "snapshot_failures_delta": last[3] - first[3],
            "attributed_samples_delta": last[4] - first[4],
            "native_only_samples_delta": last[5] - first[5],
            "cache_hits_delta": last[6] - first[6],
            "cache_misses_delta": last[7] - first[7],
            "code_objects": last[8],
        }

    def execute(self) -> int:
        stage = "initialization"
        try:
            attribution_mode = {
                "off": "off",
                "native": "off",
                "shadow": "shadow-only",
                "full": "auto",
                "stress": "auto",
            }[self.benchmark_mode]
            os.environ["SPARK_PYTHON_ATTRIBUTION_MODE"] = attribution_mode
            os.environ["SPARK_PYTHON_HOTSPOT_MODE"] = self.mode
            os.environ["SPARK_PYTHON_HOTSPOT_ITERATIONS"] = "12000"
            os.environ["SPARK_PYTHON_TICK_METRICS"] = str(self.tick_metrics_path)
            stage = "artifact-install"
            self.install_artifacts()
            stage = "server-bootstrap"
            self.bootstrap_server()
            stage = "bots-connect"
            self.start_bots()
            time.sleep(20)
            stage = "performance-window"
            metrics, profiler_start = self.measure()
            stage = "profile-stop"
            viewer_url, profile = self.stop_profiler(profiler_start)
            metrics["viewer_url"] = viewer_url
            metrics["profile_summary"] = profile
            metrics["shadow_only_diagnostics"] = self.shadow_only_diagnostics()
            stage = "bots-disconnect"
            self.stop_bots()
            stage = "shutdown"
            self.shutdown()
            stage = "tick-statistics"
            metrics["tick_statistics"] = self.tick_statistics()
            self.result["performance"] = metrics
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
                self.stop_bots()
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
    parser.add_argument("--mode", required=True, choices=["off", "native", "shadow", "full", "stress"])
    parser.add_argument("--duration-seconds", type=int, default=180)
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()
    validator = PythonAttributionPerformance(
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
