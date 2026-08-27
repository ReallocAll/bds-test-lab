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
import traceback
from typing import Any

import psutil

from controller.bot_validation import patch_server_properties
from controller.cross_platform_fleet_validation import CrossPlatformFleetSparkValidation
from controller.fleet_spark_validation import set_server_property
from controller.run_test import READY_HINTS, SPARK_LOAD_HINTS, ServerProcess, now_iso, write_json

BOT_COUNT = 20
WORLD_NAME = "SparkProfilerOverhead"
TIME_VALUE_RE = re.compile(r"(?:game\s*time|time\s+is|time:)\D*(-?\d+)", re.IGNORECASE)

# Large enough to stress entity ticking/AI without deliberately driving hosted runners into OOM.
ENTITY_GROUPS = (
    ("zombie", 100),
    ("skeleton", 100),
    ("villager", 100),
    ("cow", 100),
)


def median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return float(ordered[index])


class ProfilerOverheadValidation(CrossPlatformFleetSparkValidation):
    def __init__(
        self,
        platform_name: str,
        bot_binary: pathlib.Path,
        window_seconds: int,
        repeats: int,
    ) -> None:
        super().__init__(platform_name, bot_binary, BOT_COUNT, "idle", max(30, window_seconds))
        self.window_seconds = max(10, window_seconds)
        self.repeats = max(2, repeats)
        self.benchmark_result = self.root / f"profiler-overhead-{platform_name}.json"
        self.report_path = self.root / f"profiler-overhead-{platform_name}.md"
        self.spark_binary_enabled: pathlib.Path | None = None
        self.spark_binary_disabled: pathlib.Path | None = None
        self.shim_enabled: pathlib.Path | None = None
        self.shim_disabled: pathlib.Path | None = None
        self.result.update(
            {
                "test_kind": "spark-profiler-overhead",
                "spark_sha": os.environ.get("EXPECTED_SPARK_SHA", ""),
                "bot_count": BOT_COUNT,
                "window_seconds": self.window_seconds,
                "repeats": self.repeats,
                "entity_target": sum(count for _, count in ENTITY_GROUPS),
                "phases": [],
                "profiles": [],
                "comparisons": [],
                "measurement_method": {
                    "primary": "BDS process CPU-time delta divided by Bedrock /time query gametime tick delta",
                    "secondary": "wall-clock milliseconds per game tick; Spark TPS/MSPT/CPU when Spark is loaded",
                    "note": "CPU ms/tick is aggregate process CPU time, not main-thread wall MSPT; OFF/ON deltas use identical lab logic and load shape.",
                },
            }
        )
        self._write_results()

    def _write_results(self) -> None:
        write_json(self.result_path, self.result)
        write_json(self.fleet_result, self.result)
        if hasattr(self, "benchmark_result"):
            write_json(self.benchmark_result, self.result)

    def install_artifacts(self) -> None:
        super().install_artifacts()
        plugin_dir = self.server_dir / "plugins"
        suffix = ".dll" if self.platform == "windows" else ".so"
        self.spark_binary_enabled = plugin_dir / f"endstone_spark{suffix}"
        if not self.spark_binary_enabled.exists():
            matches = sorted(plugin_dir.glob(f"*spark*{suffix}"))
            matches = [p for p in matches if "allocation_shim" not in p.name]
            if not matches:
                raise FileNotFoundError(f"Spark plugin binary not found in {plugin_dir}")
            self.spark_binary_enabled = matches[0]
        self.spark_binary_disabled = self.spark_binary_enabled.with_suffix(self.spark_binary_enabled.suffix + ".disabled")
        if self.platform == "windows":
            candidate = plugin_dir / "spark_allocation_shim.dll"
            if candidate.exists():
                self.shim_enabled = candidate
                self.shim_disabled = candidate.with_suffix(candidate.suffix + ".disabled")
        self._write_results()

    def _stop_server(self) -> None:
        if self.server is None:
            return
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop gracefully between benchmark phases")
        self.server.close()
        self.server = None

    def _toggle_file(self, enabled_path: pathlib.Path | None, disabled_path: pathlib.Path | None, enabled: bool) -> None:
        if enabled_path is None or disabled_path is None:
            return
        if enabled:
            if disabled_path.exists():
                if enabled_path.exists():
                    enabled_path.unlink()
                disabled_path.rename(enabled_path)
        else:
            if enabled_path.exists():
                if disabled_path.exists():
                    disabled_path.unlink()
                enabled_path.rename(disabled_path)

    def set_spark_enabled(self, enabled: bool) -> None:
        if self.server is not None:
            raise RuntimeError("Spark binary state can only change while BDS is stopped")
        self._toggle_file(self.spark_binary_enabled, self.spark_binary_disabled, enabled)
        self._toggle_file(self.shim_enabled, self.shim_disabled, enabled)
        self.check(
            f"spark-binary-{'enabled' if enabled else 'disabled'}",
            "PASS",
            f"Spark binary state changed for {'instrumented' if enabled else 'baseline'} phase",
        )

    def start_server_mode(self, expect_spark: bool) -> None:
        cmd = [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(self.server_dir)]
        self.server = ServerProcess(cmd, self.root, self.log_path)
        self.server.start()
        self.server.wait_for(
            lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines),
            240,
            "BDS ready",
        )
        if expect_spark:
            self.server.wait_for(
                lambda lines: any(
                    "spark" in line.lower() and any(hint in line.lower() for hint in SPARK_LOAD_HINTS)
                    for line in lines
                ),
                30,
                "Spark enable",
            )
            self.wait_post_start_initialization()
        else:
            # Give Endstone plugin discovery time to finish and prove Spark did not load.
            time.sleep(5)
            recent = "\n".join(self.server.snapshot()).lower()
            if "[spark]" in recent and "enabled. run /spark" in recent:
                raise RuntimeError("Spark unexpectedly loaded during baseline phase")
        version_file = self.server_dir / "version.txt"
        if version_file.exists():
            self.result["bds_version"] = version_file.read_text(encoding="utf-8").strip()
        self._write_results()

    def bootstrap_world(self) -> None:
        # First boot provisions server.properties.
        self.set_spark_enabled(True)
        self.start_server_mode(True)
        self._stop_server()

        properties = self.server_dir / "server.properties"
        patch_server_properties(properties)
        set_server_property(properties, "max-players", "30")
        set_server_property(properties, "level-name", WORLD_NAME)
        set_server_property(properties, "allow-cheats", "true")
        set_server_property(properties, "player-idle-timeout", "0")
        set_server_property(properties, "view-distance", "12")
        set_server_property(properties, "tick-distance", "6")

        self.start_server_mode(True)
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
        self.check("benchmark-world-bootstrap", "PASS", "fixed world and gamerules prepared")

    def _bedrock_processes(self) -> list[psutil.Process]:
        assert self.server is not None and self.server.process is not None
        root = psutil.Process(self.server.process.pid)
        processes = [root]
        try:
            processes.extend(root.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        bedrock: list[psutil.Process] = []
        for process in processes:
            try:
                name = process.name().lower()
                cmdline = " ".join(process.cmdline()).lower()
                if "bedrock_server" in name or "bedrock_server" in cmdline:
                    bedrock.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return bedrock or processes

    def process_cpu_seconds(self) -> float:
        total = 0.0
        for process in self._bedrock_processes():
            try:
                times = process.cpu_times()
                total += float(times.user) + float(times.system)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total

    def process_rss_bytes(self) -> int:
        total = 0
        for process in self._bedrock_processes():
            try:
                total += int(process.memory_info().rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
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
                # Bedrock prefixes may contain timestamps; the command result is the final integer.
                return int(numbers[-1])
        raise RuntimeError("Unable to parse /time query gametime output: " + " | ".join(output[-20:]))

    def spark_metrics(self) -> dict[str, Any] | None:
        assert self.server is not None
        start = self.server.command("spark tps")
        output = self.server.wait_command_output(start, 8)
        try:
            return self.parse_spark_metrics(output, [self.process_rss_bytes()])
        except Exception as exc:
            self.check("spark-metrics-parse-warning", "WARN", str(exc)[:500])
            return None

    def measure_once(self) -> dict[str, Any]:
        assert self.server is not None
        tick_start = self.query_gametime()
        cpu_start = self.process_cpu_seconds()
        rss_start = self.process_rss_bytes()
        wall_start = time.monotonic()
        deadline = wall_start + self.window_seconds
        rss_samples: list[int] = []
        while time.monotonic() < deadline:
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during benchmark measurement window")
            rss_samples.append(self.process_rss_bytes())
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        wall_end = time.monotonic()
        cpu_end = self.process_cpu_seconds()
        tick_end = self.query_gametime()
        rss_end = self.process_rss_bytes()
        ticks = tick_end - tick_start
        if ticks <= 0:
            raise RuntimeError(f"Non-positive gametime delta: {tick_start} -> {tick_end}")
        wall_seconds = wall_end - wall_start
        cpu_seconds = max(0.0, cpu_end - cpu_start)
        return {
            "ticks": ticks,
            "wall_seconds": wall_seconds,
            "tick_rate": ticks / wall_seconds,
            "wall_ms_per_tick": wall_seconds * 1000.0 / ticks,
            "cpu_seconds": cpu_seconds,
            "cpu_ms_per_tick": cpu_seconds * 1000.0 / ticks,
            "rss_start_bytes": rss_start,
            "rss_end_bytes": rss_end,
            "rss_mean_bytes": int(sum(rss_samples) / len(rss_samples)) if rss_samples else rss_end,
            "rss_peak_bytes": max(rss_samples) if rss_samples else rss_end,
        }

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
        try:
            for _ in range(self.repeats):
                windows.append(self.measure_once())
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
            "spark_metrics": self.spark_metrics() if spark_enabled else None,
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

    def start_load(self, scenario: str) -> None:
        self.scenario = scenario
        self.bot_log = self.root / f"profiler-overhead-{self.platform}-{scenario}.log"
        self.start_fleet()
        time.sleep(15)
        self.wait_player_count(BOT_COUNT, timeout=15)

    def spawn_entity_load(self) -> None:
        assert self.server is not None
        # Remove prior non-player entities so the high-load phases are reproducible.
        start = self.server.command("kill @e[type=!player]")
        self.server.wait_command_output(start, 5)
        start_index = len(self.server.snapshot())
        species_index = 0
        for entity_type, count in ENTITY_GROUPS:
            for index in range(count):
                x = (index % 20) - 10
                z = ((index // 20) % 20) - 10
                # A name tag keeps the synthetic mobs from normal distance despawn.
                command = (
                    f'execute at @a[c=1] run summon {entity_type} "SparkBench{species_index}" ~{x} ~ ~{z}'
                )
                self.server.command(command)
            species_index += 1
        time.sleep(20)
        output = self.server.snapshot()[start_index:]
        failures = [
            line
            for line in output
            if "syntax error" in line.lower()
            or "failed to execute" in line.lower()
            or "no targets matched" in line.lower()
        ]
        if failures:
            raise RuntimeError("Entity load generation failed: " + " | ".join(failures[:20]))
        self.check(
            "high-entity-load-created",
            "PASS",
            f"issued {sum(count for _, count in ENTITY_GROUPS)} named mob summons across four AI entity types",
        )

    def stop_load(self) -> None:
        if self.bot is not None:
            self.stop_fleet()
            self.bot = None

    def run_server_case(
        self,
        *,
        spark_enabled: bool,
        scenario: str | None,
        high_entities: bool,
        phase_specs: list[tuple[str, str | None]],
    ) -> None:
        self.set_spark_enabled(spark_enabled)
        self.start_server_mode(spark_enabled)
        try:
            if scenario:
                self.start_load(scenario)
            if high_entities:
                self.spawn_entity_load()
            for phase_name, profiler in phase_specs:
                self.measure_phase(
                    phase_name,
                    spark_enabled=spark_enabled,
                    load=(
                        f"{BOT_COUNT} {scenario} bots + {sum(count for _, count in ENTITY_GROUPS)} named mobs"
                        if high_entities
                        else (f"{BOT_COUNT} {scenario} bots" if scenario else "no players")
                    ),
                    profiler=profiler,
                )
        finally:
            if self.bot is not None:
                self.stop_load()
            self._stop_server()

    def build_comparisons(self) -> None:
        by_name = {phase["name"]: phase for phase in self.result["phases"]}
        pairs = (
            ("empty_loaded", "empty_baseline", "Spark loaded, no players"),
            ("idle20_loaded", "idle20_baseline", "Spark loaded, 20 idle bots"),
            ("idle20_execution", "idle20_loaded", "Execution profiler, 20 idle bots"),
            ("walk20_loaded", "walk20_baseline", "Spark loaded, 20 chunk-walk bots"),
            ("walk20_execution_before_alloc", "walk20_loaded", "Execution profiler before alloc"),
            ("walk20_allocation", "walk20_loaded", "Allocation profiler"),
            ("walk20_execution_after_alloc", "walk20_loaded", "Execution profiler after alloc"),
            ("mobs_loaded", "mobs_baseline", "Spark loaded, 20 bots + large mob load"),
            ("mobs_execution_before_alloc", "mobs_loaded", "High-load execution before alloc"),
            ("mobs_allocation", "mobs_loaded", "High-load allocation"),
            ("mobs_execution_after_alloc", "mobs_loaded", "High-load execution after alloc"),
        )
        comparisons: list[dict[str, Any]] = []
        for current_name, base_name, description in pairs:
            current = by_name.get(current_name)
            base = by_name.get(base_name)
            if not current or not base:
                continue
            current_cpu = float(current["cpu_ms_per_tick"]["median"])
            base_cpu = float(base["cpu_ms_per_tick"]["median"])
            delta = current_cpu - base_cpu
            comparisons.append(
                {
                    "description": description,
                    "phase": current_name,
                    "baseline": base_name,
                    "cpu_ms_per_tick_delta": delta,
                    "cpu_percent_delta": (delta / base_cpu * 100.0) if base_cpu > 0 else None,
                    "wall_ms_per_tick_delta": float(current["wall_ms_per_tick"]["median"])
                    - float(base["wall_ms_per_tick"]["median"]),
                }
            )
        self.result["comparisons"] = comparisons
        self._write_results()

    def write_report(self) -> None:
        self.build_comparisons()
        lines = [
            f"# Spark profiler overhead benchmark — {self.platform}",
            "",
            f"- Spark SHA: `{self.result.get('spark_sha')}`",
            f"- BDS: `{self.result.get('bds_version')}`",
            f"- Window: {self.window_seconds}s × {self.repeats} repeats per phase",
            f"- Bots: {BOT_COUNT}",
            f"- High entity target: {self.result.get('entity_target')}",
            "",
            "## Phase results",
            "",
            "| Phase | Load | Profiler | CPU ms/tick median | Wall ms/tick median | Tick rate | Viewer |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
        for phase in self.result["phases"]:
            viewer = phase.get("viewer_url") or ""
            if viewer:
                viewer = f"[report]({viewer})"
            lines.append(
                f"| {phase['name']} | {phase['load']} | {phase['profiler']} | "
                f"{phase['cpu_ms_per_tick']['median']:.4f} | "
                f"{phase['wall_ms_per_tick']['median']:.4f} | "
                f"{phase['tick_rate']['median']:.3f} | {viewer} |"
            )
        lines += [
            "",
            "## Differential overhead",
            "",
            "| Comparison | Δ CPU ms/tick | Δ CPU % | Δ wall ms/tick |",
            "|---|---:|---:|---:|",
        ]
        for item in self.result["comparisons"]:
            pct_delta = item["cpu_percent_delta"]
            pct_text = "" if pct_delta is None else f"{pct_delta:.2f}%"
            lines.append(
                f"| {item['description']} | {item['cpu_ms_per_tick_delta']:.4f} | "
                f"{pct_text} | {item['wall_ms_per_tick_delta']:.4f} |"
            )
        lines += [
            "",
            "## Method",
            "",
            "Primary overhead is measured externally from BDS process CPU-time divided by Bedrock game-time ticks.",
            "This avoids using Spark's own MSPT as the only source for Spark-overhead measurement. "
            "Spark TPS/MSPT/CPU are captured as secondary evidence whenever Spark is loaded.",
            "",
        ]
        self.report_path.write_text("\n".join(lines), encoding="utf-8")

    def execute_benchmark(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()

            stage = "world-bootstrap"
            self.bootstrap_world()

            stage = "empty-baseline"
            self.run_server_case(
                spark_enabled=False,
                scenario=None,
                high_entities=False,
                phase_specs=[("empty_baseline", None)],
            )
            stage = "empty-loaded"
            self.run_server_case(
                spark_enabled=True,
                scenario=None,
                high_entities=False,
                phase_specs=[("empty_loaded", None)],
            )

            stage = "idle20-baseline"
            self.run_server_case(
                spark_enabled=False,
                scenario="idle",
                high_entities=False,
                phase_specs=[("idle20_baseline", None)],
            )
            stage = "idle20-spark"
            self.run_server_case(
                spark_enabled=True,
                scenario="idle",
                high_entities=False,
                phase_specs=[
                    ("idle20_loaded", None),
                    ("idle20_execution", "execution"),
                ],
            )

            stage = "walk20-baseline"
            self.run_server_case(
                spark_enabled=False,
                scenario="chunk-walk",
                high_entities=False,
                phase_specs=[("walk20_baseline", None)],
            )
            stage = "walk20-spark"
            self.run_server_case(
                spark_enabled=True,
                scenario="chunk-walk",
                high_entities=False,
                phase_specs=[
                    ("walk20_loaded", None),
                    ("walk20_execution_before_alloc", "execution"),
                    ("walk20_allocation", "allocation"),
                    ("walk20_execution_after_alloc", "execution"),
                ],
            )

            stage = "mobs-baseline"
            self.run_server_case(
                spark_enabled=False,
                scenario="idle",
                high_entities=True,
                phase_specs=[("mobs_baseline", None)],
            )
            stage = "mobs-spark"
            self.run_server_case(
                spark_enabled=True,
                scenario="idle",
                high_entities=True,
                phase_specs=[
                    ("mobs_loaded", None),
                    ("mobs_execution_before_alloc", "execution"),
                    ("mobs_allocation", "allocation"),
                    ("mobs_execution_after_alloc", "execution"),
                ],
            )

            stage = "report"
            self.write_report()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self._write_results()
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1600]
            diagnostic = traceback.format_exc()
            try:
                if self.bot is not None:
                    self.bot.terminate(10)
                    self.bot = None
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.server.close()
                    self.server = None
            except Exception:
                diagnostic += "\n\nCleanup failure:\n" + traceback.format_exc()
            self.diagnostics.write_text(diagnostic, encoding="utf-8")
            try:
                self.write_report()
            except Exception:
                pass
            self._write_results()
            return 1
        finally:
            self.result["completed_at"] = now_iso()
            self.split_logs()
            self._write_results()
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--window-seconds", type=int, default=int(os.environ.get("BENCH_WINDOW_SECONDS", "20")))
    parser.add_argument("--repeats", type=int, default=int(os.environ.get("BENCH_REPEATS", "3")))
    args = parser.parse_args()
    validator = ProfilerOverheadValidation(
        args.platform,
        pathlib.Path(args.bot),
        args.window_seconds,
        args.repeats,
    )
    return validator.execute_benchmark()


if __name__ == "__main__":
    raise SystemExit(main())
