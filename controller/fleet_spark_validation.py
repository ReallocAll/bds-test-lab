#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import threading
import time
import traceback
from typing import Any

import psutil

from controller.bot_validation import BotProcess, list_players, patch_server_properties
from controller.run_test import IntegrationTest, now_iso, write_json

PLAYER_COUNT_RE = re.compile(r"There are\s+(\d+)/(\d+)\s+players online", re.IGNORECASE)
TPS_RE = re.compile(r"TPS \(5s/10s/1m/5m/15m\):\s*([0-9.]+)\s*/\s*([0-9.]+)\s*/\s*([0-9.]+)\s*/\s*([0-9.]+)\s*/\s*([0-9.]+)")
MSPT_RE = re.compile(r"MSPT 10s \(mean/min/median/p95/max\):\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)")
PROCESS_CPU_RE = re.compile(r"Process CPU \(10s/1m/15m\):\s*([0-9.]+)%\s*/\s*([0-9.]+)%\s*/\s*([0-9.]+)%")
SYSTEM_CPU_RE = re.compile(r"System CPU \(10s/1m/15m\):\s*([0-9.]+)%\s*/\s*([0-9.]+)%\s*/\s*([0-9.]+)%")


class FleetBotProcess(BotProcess):
    def __init__(
        self,
        binary: pathlib.Path,
        log_path: pathlib.Path,
        count: int,
        scenario: str,
        name_prefix: str = "TestBot",
    ):
        super().__init__(binary, log_path)
        self.count = count
        self.scenario = scenario
        self.name_prefix = name_prefix

    def start(self) -> None:
        cmd = [
            str(self.binary),
            "--host",
            "127.0.0.1",
            "--port",
            "19132",
            "--count",
            str(self.count),
            "--name-prefix",
            self.name_prefix,
            "--scenario",
            self.scenario,
            "--login-stagger",
            "250ms",
            "--chunk-radius",
            "8",
            "--connect-timeout",
            "20s",
            "--spawn-timeout",
            "45s",
            "--json",
        ]
        print("+", " ".join(cmd), flush=True)
        self._log = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, name="fleet-bot-log-reader", daemon=True)
        self._reader.start()


def set_server_property(path: pathlib.Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    output: list[str] = []
    replaced = False
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            current, _ = line.split("=", 1)
            if current.strip() == key:
                output.append(f"{key}={value}")
                replaced = True
                continue
        output.append(line)
    if not replaced:
        output.append(f"{key}={value}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def percentile(values: list[int], percent: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percent + 0.999999)))
    return ordered[index]


class FleetSparkValidation(IntegrationTest):
    def __init__(self, bot_binary: pathlib.Path, count: int, scenario: str, profile_seconds: int):
        super().__init__("linux")
        self.bot_binary = bot_binary.resolve()
        self.count = count
        self.scenario = scenario
        self.profile_seconds = max(30, profile_seconds)
        self.bot_log = self.root / f"fleet-{count}-{scenario}.log"
        self.fleet_result = self.root / "fleet-spark-result.json"
        self.bot: FleetBotProcess | None = None
        self.result.update(
            {
                "test_kind": "spark-real-bot-fleet-load",
                "bot_count": count,
                "scenario": scenario,
                "profile_seconds": self.profile_seconds,
                "spark_profile_viewer_url": None,
                "metrics": None,
                "fleet_online_event": None,
                "fleet_shutdown_event": None,
                "bot_stats": [],
            }
        )
        self._write_results()

    def _write_results(self) -> None:
        write_json(self.result_path, self.result)
        write_json(self.fleet_result, self.result)

    def check(self, name: str, status: str, detail: str | None = None, **extra: Any) -> None:
        super().check(name, status, detail, **extra)
        self._write_results()

    def wait_post_start_initialization(self) -> None:
        assert self.server is not None
        self.server.wait_for(
            lambda lines: any("packet limit config updated" in line.lower() for line in lines),
            15,
            "BDS post-start packet-limit initialization",
        )

    def bootstrap_offline_server(self) -> None:
        self.start_server()
        assert self.server is not None
        self.wait_post_start_initialization()
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop after server.properties bootstrap")
        self.server.close()
        self.server = None
        properties = self.server_dir / "server.properties"
        patch_server_properties(properties)
        set_server_property(properties, "max-players", "30")
        self.check("fleet-server-properties", "PASS", "offline mode, idle timeout disabled, max-players=30")
        self.start_server()
        self.wait_post_start_initialization()

    def expected_names(self) -> list[str]:
        if self.count == 1:
            return ["TestBot"]
        width = max(2, len(str(self.count)))
        return [f"TestBot-{index:0{width}d}" for index in range(1, self.count + 1)]

    def wait_player_count(self, expected: int, timeout: float = 45.0) -> tuple[list[str], float]:
        assert self.server is not None
        started = time.monotonic()
        deadline = started + timeout
        last: list[str] = []
        while time.monotonic() < deadline:
            last = list_players(self.server)
            for line in last:
                match = PLAYER_COUNT_RE.search(line)
                if match and int(match.group(1)) == expected:
                    return last, time.monotonic() - started
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while waiting for fleet player count")
            time.sleep(0.5)
        raise RuntimeError(f"Expected {expected} online players, last list output: {' | '.join(last[-40:])}")

    def start_fleet(self) -> None:
        assert self.server is not None
        self.bot = FleetBotProcess(self.bot_binary, self.bot_log, self.count, self.scenario)
        self.bot.start()
        online = self.bot.wait_event("fleet_online", max(90.0, self.count * 5.0))
        if int(online.get("online", -1)) != self.count or int(online.get("count", -1)) != self.count:
            raise RuntimeError(f"Invalid fleet_online event: {online}")
        self.result["fleet_online_event"] = online
        output, convergence = self.wait_player_count(self.count)
        joined = "\n".join(output).lower()
        missing = [name for name in self.expected_names() if name.lower() not in joined]
        if missing:
            raise RuntimeError(f"BDS list reached {self.count} players but names are missing: {missing}")
        self.check(
            "fleet-all-online",
            "PASS",
            f"{self.count} independent players visible in BDS",
            convergence_seconds=round(convergence, 3),
            fleet_online_event=online,
        )

    def bds_rss_bytes(self) -> int:
        assert self.server is not None and self.server.process is not None
        root = psutil.Process(self.server.process.pid)
        candidates = [root]
        try:
            candidates.extend(root.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        bedrock: list[psutil.Process] = []
        for process in candidates:
            try:
                name = process.name().lower()
                cmdline = " ".join(process.cmdline()).lower()
                if "bedrock_server" in name or "bedrock_server" in cmdline:
                    bedrock.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        selected = bedrock or candidates
        rss = 0
        for process in selected:
            try:
                rss += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return rss

    def profile_execution(self) -> tuple[str, list[int]]:
        assert self.server is not None
        samples: list[int] = []
        sample_stop = threading.Event()

        def sample_memory() -> None:
            while not sample_stop.is_set():
                try:
                    samples.append(self.bds_rss_bytes())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                sample_stop.wait(1.0)

        sampler = threading.Thread(target=sample_memory, name="bds-rss-sampler", daemon=True)
        sampler.start()
        start = self.server.command(f"spark profiler start --timeout {self.profile_seconds}")
        deadline = time.monotonic() + self.profile_seconds + 75
        url: str | None = None
        try:
            while time.monotonic() < deadline:
                url = self._viewer_url(self.server.snapshot(), start)
                if url:
                    break
                if not self.server.is_alive():
                    raise RuntimeError("BDS exited while collecting fleet Spark profile")
                time.sleep(1)
            if url is None:
                stop_at = self.server.command("spark profiler stop")
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    url = self._viewer_url(self.server.snapshot(), min(start, stop_at))
                    if url:
                        break
                    if not self.server.is_alive():
                        raise RuntimeError("BDS exited while finalizing fleet Spark profile")
                    time.sleep(1)
            if url is None:
                raise RuntimeError("Fleet Spark profiler produced no viewer URL")
            return url, samples
        finally:
            sample_stop.set()
            sampler.join(timeout=3)

    def parse_spark_metrics(self, output: list[str], rss_samples: list[int]) -> dict[str, Any]:
        text = "\n".join(output)
        tps = TPS_RE.search(text)
        mspt = MSPT_RE.search(text)
        process_cpu = PROCESS_CPU_RE.search(text)
        system_cpu = SYSTEM_CPU_RE.search(text)
        if not all((tps, mspt, process_cpu, system_cpu)):
            raise RuntimeError("Unable to parse required Spark TPS/MSPT/CPU metrics: " + " | ".join(output[-30:]))
        if not rss_samples:
            raise RuntimeError("No BDS RSS samples were collected during profiling")
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
            "memory_rss_bytes": {
                "mean": int(sum(rss_samples) / len(rss_samples)),
                "p95": percentile(rss_samples, 0.95),
                "max": max(rss_samples),
                "samples": len(rss_samples),
            },
        }

    def stop_fleet(self) -> None:
        if self.bot is None:
            return
        assert self.server is not None
        code = self.bot.terminate(20)
        if code != 0:
            raise RuntimeError(f"Fleet exited with code {code} after SIGTERM")
        events = self.bot.event_snapshot()
        shutdown = next((event for event in reversed(events) if event.get("event") == "fleet_shutdown"), None)
        if shutdown is None or shutdown.get("graceful_shutdown") is not True:
            raise RuntimeError(f"Missing successful fleet_shutdown event: {shutdown}")
        stats = [event for event in events if event.get("event") == "bot_stats"]
        if len(stats) != self.count:
            raise RuntimeError(f"Expected {self.count} bot_stats events, got {len(stats)}")
        bad = [event for event in stats if not event.get("online") or int(event.get("auth_inputs_sent", 0)) <= 0]
        if bad:
            raise RuntimeError(f"Per-bot online/AuthInput statistics failed: {bad[:3]}")
        self.result["fleet_shutdown_event"] = shutdown
        self.result["bot_stats"] = stats
        output, propagation = self.wait_player_count(0, timeout=30)
        self.check(
            "fleet-graceful-shutdown",
            "PASS",
            f"all {self.count} bots disconnected cleanly",
            propagation_seconds=round(propagation, 3),
            output=" | ".join(output[-30:]),
            shutdown_event=shutdown,
        )

    def execute(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            self._write_results()

            stage = "bds-bootstrap"
            self.bootstrap_offline_server()
            assert self.server is not None

            stage = "spark-sanity"
            self.run_basic_commands()

            stage = "fleet-connect"
            self.start_fleet()

            stage = "fleet-settle"
            time.sleep(20)
            output, _ = self.wait_player_count(self.count, timeout=10)
            self.check("fleet-stable-before-profile", "PASS", output=" | ".join(output[-30:]))

            stage = "fleet-profile"
            url, rss_samples = self.profile_execution()
            self.result["spark_profile_viewer_url"] = url
            self._write_results()

            stage = "fleet-metrics"
            assert self.server is not None
            start = self.server.command("spark tps")
            spark_output = self.server.wait_command_output(start, 8)
            metrics = self.parse_spark_metrics(spark_output, rss_samples)
            self.result["metrics"] = metrics
            self.check("fleet-load-metrics", "PASS", viewer_url=url, **metrics)

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
            self.result["state"] = "completed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1200]
            diagnostic = traceback.format_exc()
            try:
                if self.bot is not None and self.bot.is_alive():
                    self.bot.force_close()
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:
                diagnostic += "\n\nCleanup failure:\n" + traceback.format_exc()
            last_lines = self.server.snapshot()[-300:] if self.server is not None else []
            self.diagnostics.write_text(
                diagnostic + "\n\nLast BDS log lines:\n" + "\n".join(last_lines),
                encoding="utf-8",
            )
            self._write_results()
            return 1
        finally:
            if self.bot is not None and self.bot.is_alive():
                self.bot.force_close()
            self.result["completed_at"] = now_iso()
            self.split_logs()
            self._write_results()
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--count", required=True, type=int, choices=[1, 5, 10, 20])
    parser.add_argument("--scenario", default="idle", choices=["idle"])
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()
    return FleetSparkValidation(pathlib.Path(args.bot), args.count, args.scenario, args.profile_seconds).execute()


if __name__ == "__main__":
    raise SystemExit(main())
