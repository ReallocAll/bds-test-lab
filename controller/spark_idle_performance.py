#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import sys
import time
from typing import Any

import psutil

from controller.cross_platform_fleet_validation import CrossPlatformFleetSparkValidation
from controller.run_test import READY_HINTS, ServerProcess, locate_one, run_checked, write_json
from providers.artifact_provider import resolve_artifacts

TIME_VALUE_RE = re.compile(r"(?:game\s*time|time\s+is|time:)\D*(-?\d+)", re.IGNORECASE)
TPS_RE = re.compile(r"TPS \(5s/10s/1m/5m/15m\):\s*([0-9.]+)\s*/\s*([0-9.]+)\s*/\s*([0-9.]+)\s*/\s*([0-9.]+)\s*/\s*([0-9.]+)")
MSPT_RE = re.compile(r"MSPT 10s \(mean/min/median/p95/max\):\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)")
PROCESS_CPU_RE = re.compile(r"Process CPU \(10s/1m/15m\):\s*([0-9.]+)%\s*/\s*([0-9.]+)%\s*/\s*([0-9.]+)%")
SYSTEM_CPU_RE = re.compile(r"System CPU \(10s/1m/15m\):\s*([0-9.]+)%\s*/\s*([0-9.]+)%\s*/\s*([0-9.]+)%")


def _cpu_totals(snapshot: Any) -> tuple[float, float]:
    values = snapshot._asdict()
    total = float(sum(values.values()))
    idle = float(values.get("idle", 0.0) + values.get("iowait", 0.0))
    return total, max(0.0, total - idle)


class SparkIdlePerformance(CrossPlatformFleetSparkValidation):
    def __init__(
        self,
        platform_name: str,
        bot_binary: pathlib.Path,
        mode: str,
        duration_seconds: int,
        bot_count: int,
    ) -> None:
        super().__init__(platform_name, bot_binary, bot_count, "chunk-walk", max(30, duration_seconds))
        self.mode = mode
        self.duration_seconds = max(120, duration_seconds)
        self.performance_path = self.root / "spark-idle-performance.json"
        self.result.update(
            {
                "test_kind": "spark-idle-component-performance",
                "benchmark_mode": mode,
                "duration_seconds": self.duration_seconds,
                "bot_count": bot_count,
                "performance": None,
            }
        )
        self._write_results()

    @property
    def spark_loaded(self) -> bool:
        return self.mode != "control"

    def _write_results(self) -> None:
        super()._write_results()
        if hasattr(self, "performance_path"):
            write_json(self.performance_path, self.result)

    def configure_environment(self) -> None:
        os.environ["SPARK_PYTHON_ATTRIBUTION_MODE"] = "off"
        if self.mode == "control":
            return
        # The idle/monitoring baselines intentionally contain no background
        # execution sampler. This isolates passive Spark tax from active sampling.
        os.environ["SPARK_BACKGROUNDPROFILER"] = "false"
        os.environ["SPARK_ALLOCATIONRATEMETRICS"] = "true" if self.mode == "monitoring" else "false"

    def install_artifacts(self) -> None:
        if self.spark_loaded:
            return super().install_artifacts()

        # Absolute control: same exact Endstone artifact and BDS bootstrap, but do
        # not deploy Spark. Artifact discovery still pins/downloads the exact Spark
        # build so metadata remains directly comparable with Spark-loaded modes.
        self.metadata = resolve_artifacts(self.platform, self.downloads, self.metadata_path)
        self.check("artifact-discovery", "PASS", "absolute control; Spark artifact resolved but not deployed")
        endstone_root = self.downloads / "endstone" / "payload"
        wheel = locate_one(endstone_root, ["endstone-*-cp313-cp313-*.whl", "endstone-*.whl"])
        self.check("endstone-wheel-located", "PASS", str(wheel.relative_to(self.root)))
        run_checked(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall", str(wheel)],
            timeout=300,
        )
        self.server_dir.mkdir(parents=True, exist_ok=True)
        self.check("spark-not-deployed", "PASS", "BDS + Endstone absolute control")

    def start_server(self) -> None:
        if self.spark_loaded:
            return super().start_server()
        cmd = [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(self.server_dir)]
        self.server = ServerProcess(cmd, self.root, self.log_path)
        self.server.start()
        self.server.wait_for(
            lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines),
            240,
            "BDS ready",
        )
        self.check("bds-start", "PASS")
        self.check("ready", "PASS")
        version_file = self.server_dir / "version.txt"
        if version_file.exists():
            self.result["bds_version"] = version_file.read_text(encoding="utf-8").strip()
            self._write_results()

    def wait_post_start_initialization(self) -> None:
        if self.spark_loaded:
            return super().wait_post_start_initialization()
        return None

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

    def process_context_switches(self) -> tuple[int, int]:
        voluntary = 0
        involuntary = 0
        for process in self._bedrock_processes():
            try:
                current = process.num_ctx_switches()
                voluntary += int(current.voluntary)
                involuntary += int(current.involuntary)
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                pass
        return voluntary, involuntary

    def process_page_faults(self) -> tuple[int, int]:
        minor = 0
        major = 0
        for process in self._bedrock_processes():
            try:
                if sys.platform.startswith("linux"):
                    fields = pathlib.Path(f"/proc/{process.pid}/stat").read_text(encoding="utf-8").split()
                    minor += int(fields[9])
                    major += int(fields[11])
            except (OSError, ValueError, IndexError, psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return minor, major

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

    def spark_metrics(self) -> dict[str, Any] | None:
        if not self.spark_loaded:
            return None
        assert self.server is not None
        start = self.server.command("spark tps")
        output = self.server.wait_command_output(start, 8)
        text = "\n".join(output)
        tps = TPS_RE.search(text)
        mspt = MSPT_RE.search(text)
        process_cpu = PROCESS_CPU_RE.search(text)
        system_cpu = SYSTEM_CPU_RE.search(text)
        if not all((tps, mspt, process_cpu, system_cpu)):
            raise RuntimeError("Unable to parse Spark TPS/MSPT/CPU metrics: " + " | ".join(output[-30:]))
        return {
            "tps": {
                "5s": float(tps.group(1)),
                "10s": float(tps.group(2)),
                "1m": float(tps.group(3)),
                "5m": float(tps.group(4)),
                "15m": float(tps.group(5)),
            },
            "mspt_10s": {
                "mean": float(mspt.group(1)),
                "min": float(mspt.group(2)),
                "p50": float(mspt.group(3)),
                "p95": float(mspt.group(4)),
                "max": float(mspt.group(5)),
            },
            "process_cpu_percent": {
                "10s": float(process_cpu.group(1)),
                "1m": float(process_cpu.group(2)),
                "15m": float(process_cpu.group(3)),
            },
            "system_cpu_percent": {
                "10s": float(system_cpu.group(1)),
                "1m": float(system_cpu.group(2)),
                "15m": float(system_cpu.group(3)),
            },
        }

    def measure(self) -> dict[str, Any]:
        assert self.server is not None
        time.sleep(10)
        tick_start = self.query_gametime()
        cpu_start = self.process_cpu_seconds()
        ctx_start = self.process_context_switches()
        faults_start = self.process_page_faults()
        system_start = _cpu_totals(psutil.cpu_times())
        wall_start = time.monotonic()
        rss: list[int] = []
        thread_counts: list[int] = []
        deadline = wall_start + self.duration_seconds
        while time.monotonic() < deadline:
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during idle performance window")
            rss.append(self.process_rss_bytes())
            try:
                thread_counts.append(sum(process.num_threads() for process in self._bedrock_processes()))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        wall_end = time.monotonic()
        cpu_end = self.process_cpu_seconds()
        ctx_end = self.process_context_switches()
        faults_end = self.process_page_faults()
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
            "thread_count_mean": sum(thread_counts) / len(thread_counts) if thread_counts else None,
            "spark_metrics": self.spark_metrics(),
        }

    def execute(self) -> int:
        stage = "initialization"
        try:
            self.configure_environment()
            stage = "artifact-install"
            self.install_artifacts()
            stage = "server-bootstrap"
            self.bootstrap_offline_server()
            if self.spark_loaded:
                stage = "spark-sanity"
                self.run_basic_commands()
            stage = "fleet-connect"
            self.start_fleet()
            stage = "fleet-settle"
            time.sleep(20)
            stage = "performance-window"
            self.result["performance"] = self.measure()
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
    parser.add_argument("--mode", required=True, choices=["control", "idle", "monitoring"])
    parser.add_argument("--duration-seconds", type=int, default=180)
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()
    validator = SparkIdlePerformance(
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
