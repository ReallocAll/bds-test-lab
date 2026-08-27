#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import pathlib
import time
from typing import Any

from controller.fleet_spark_validation import set_server_property
from controller.profiler_overhead_runner import ResilientProfilerOverheadValidation
from controller.profiler_overhead_validation import BOT_COUNT, ENTITY_GROUPS


class PairedProfilerOverheadValidation(ResilientProfilerOverheadValidation):
    """Benchmark profiler overhead against immediately adjacent local baselines."""

    def __init__(self, platform_name: str, bot_binary: pathlib.Path, window_seconds: int, repeats: int) -> None:
        super().__init__(platform_name, bot_binary, window_seconds, repeats)
        self.result["measurement_method"].update(
            {
                "profiler_delta": (
                    "each profiler phase is bracketed by Spark-loaded profiler-off phases; "
                    "overhead is profiler median minus the mean of the immediately adjacent baseline medians"
                ),
                "paired_baseline_reason": (
                    "local bracketing reduces chunk/player/entity workload drift observed when a profiler phase "
                    "was compared only with a single baseline collected minutes earlier"
                ),
                "entity_load": (
                    "400 AI mobs are summoned directly by the privileged console on a deterministic 20x20 grid; "
                    "20 bots are teleported to the same area so the entities stay active without name tags"
                ),
                "difficulty": (
                    "normal is persisted in server.properties and reasserted immediately before synthetic mob "
                    "generation so hostile AI entities remain valid across BDS restarts"
                ),
            }
        )
        self._write_results()

    def bootstrap_world(self) -> None:
        super().bootstrap_world()
        # The /difficulty command used during bootstrap only affected that process in
        # previous benchmark runs. BDS subsequently restarted from difficulty=peaceful,
        # making hostile /summon commands invalid. Persist the benchmark difficulty.
        properties = self.server_dir / "server.properties"
        set_server_property(properties, "difficulty", "normal")
        self.check(
            "benchmark-difficulty-persisted",
            "PASS",
            "server.properties difficulty fixed to normal for every benchmark restart",
        )
        self._write_results()

    def spawn_entity_load(self) -> None:
        assert self.server is not None

        # Reassert at runtime as well as in server.properties. This makes the synthetic
        # load independent of any level-state difficulty value retained by BDS.
        difficulty_at = self.server.command("difficulty normal")
        difficulty_output = self.server.wait_command_output(difficulty_at, 5)
        difficulty_text = "\n".join(difficulty_output).lower()
        if "error" in difficulty_text or "failed" in difficulty_text:
            raise RuntimeError("Unable to set normal difficulty for entity load: " + " | ".join(difficulty_output[-20:]))

        # Clear any previous synthetic/non-player entities. No-target is harmless here.
        start = self.server.command("kill @e[type=!player]")
        self.server.wait_command_output(start, 5)

        # Put every bot beside the synthetic load, then use absolute coordinates so
        # /summon runs directly as the privileged console. This avoids both failure
        # modes already observed in BDS 26.44: a Null executor through execute-at and
        # execute-as downgrading to a normal bot that lacks Game-Director permission.
        move_at = self.server.command("tp @a 0 10 0")
        move_output = self.server.wait_command_output(move_at, 5)
        if any("no targets matched" in line.lower() or "error" in line.lower() for line in move_output):
            raise RuntimeError("Unable to position bots for entity load: " + " | ".join(move_output[-20:]))

        start_index = len(self.server.snapshot())
        global_index = 0
        for entity_type, count in ENTITY_GROUPS:
            for _ in range(count):
                x = (global_index % 20) - 10
                z = ((global_index // 20) % 20) - 10
                self.server.command(f"summon {entity_type} {x} 10 {z}")
                global_index += 1
                # Avoid turning stdin command flooding into part of the benchmark.
                if global_index % 25 == 0:
                    time.sleep(0.1)

        # Let commands execute and allow the mobs/players to fall onto the flat world,
        # then wait for AI/collision state to settle before the first timed window.
        time.sleep(20)
        output = self.server.snapshot()[start_index:]
        failures = [
            line
            for line in output
            if "syntax error" in line.lower()
            or "failed to execute" in line.lower()
            or "unable to summon" in line.lower()
            or "no targets matched" in line.lower()
        ]
        if failures:
            raise RuntimeError("Entity load generation failed: " + " | ".join(failures[:20]))
        self.check(
            "high-entity-load-created",
            "PASS",
            f"issued {sum(count for _, count in ENTITY_GROUPS)} AI mob summons across four entity types as console",
        )

    def run_server_case(
        self,
        *,
        spark_enabled: bool,
        scenario: str | None,
        high_entities: bool,
        phase_specs: list[tuple[str, str | None]],
    ) -> None:
        # Bracket every profiler-on measurement with a local profiler-off phase in the
        # same BDS process, with the same connected bots and the same entity population.
        # The original first profiler-off phase is the pre-baseline; each profiler gets
        # a post-baseline, which becomes the next profiler's pre-baseline as well.
        if spark_enabled and any(profiler is not None for _, profiler in phase_specs):
            expanded: list[tuple[str, str | None]] = []
            for phase_name, profiler in phase_specs:
                expanded.append((phase_name, profiler))
                if profiler is not None:
                    expanded.append((f"{phase_name}__post_baseline", None))
            phase_specs = expanded
        super().run_server_case(
            spark_enabled=spark_enabled,
            scenario=scenario,
            high_entities=high_entities,
            phase_specs=phase_specs,
        )

    @staticmethod
    def _phase_cpu(phase: dict[str, Any]) -> float:
        return float(phase["cpu_ms_per_tick"]["median"])

    @staticmethod
    def _phase_wall(phase: dict[str, Any]) -> float:
        return float(phase["wall_ms_per_tick"]["median"])

    def build_comparisons(self) -> None:
        phases: list[dict[str, Any]] = list(self.result.get("phases", []))
        by_name = {phase["name"]: phase for phase in phases}
        comparisons: list[dict[str, Any]] = []

        loaded_pairs = (
            ("empty_loaded", "empty_baseline", "Spark loaded, no players"),
            ("idle20_loaded", "idle20_baseline", "Spark loaded, 20 idle bots"),
            ("walk20_loaded", "walk20_baseline", "Spark loaded, 20 chunk-walk bots"),
            ("mobs_loaded", "mobs_baseline", "Spark loaded, 20 bots + large mob load"),
        )
        for current_name, baseline_name, description in loaded_pairs:
            current = by_name.get(current_name)
            baseline = by_name.get(baseline_name)
            if current is None or baseline is None:
                continue
            base_cpu = self._phase_cpu(baseline)
            delta = self._phase_cpu(current) - base_cpu
            comparisons.append(
                {
                    "description": description,
                    "phase": current_name,
                    "baseline": baseline_name,
                    "comparison_method": "cross-restart off/on baseline",
                    "cpu_ms_per_tick_delta": delta,
                    "cpu_percent_delta": (delta / base_cpu * 100.0) if base_cpu > 0 else None,
                    "wall_ms_per_tick_delta": self._phase_wall(current) - self._phase_wall(baseline),
                }
            )

        descriptions = {
            "idle20_execution": "Execution profiler, 20 idle bots",
            "walk20_execution_before_alloc": "Execution profiler before alloc",
            "walk20_allocation": "Allocation profiler",
            "walk20_execution_after_alloc": "Execution profiler after alloc",
            "mobs_execution_before_alloc": "High-load execution before alloc",
            "mobs_allocation": "High-load allocation",
            "mobs_execution_after_alloc": "High-load execution after alloc",
        }
        for index, current in enumerate(phases):
            if current.get("profiler") not in {"execution", "allocation"}:
                continue
            previous = None
            for candidate in reversed(phases[:index]):
                if (
                    candidate.get("spark_enabled")
                    and candidate.get("profiler") == "off"
                    and candidate.get("load") == current.get("load")
                ):
                    previous = candidate
                    break
            following = None
            for candidate in phases[index + 1 :]:
                if candidate.get("load") != current.get("load"):
                    break
                if candidate.get("spark_enabled") and candidate.get("profiler") == "off":
                    following = candidate
                    break
            if previous is None or following is None:
                continue

            before_cpu = self._phase_cpu(previous)
            after_cpu = self._phase_cpu(following)
            baseline_cpu = (before_cpu + after_cpu) / 2.0
            before_wall = self._phase_wall(previous)
            after_wall = self._phase_wall(following)
            baseline_wall = (before_wall + after_wall) / 2.0
            delta = self._phase_cpu(current) - baseline_cpu
            comparisons.append(
                {
                    "description": descriptions.get(current["name"], current["name"]),
                    "phase": current["name"],
                    "baseline": [previous["name"], following["name"]],
                    "comparison_method": "paired local baseline mean",
                    "baseline_cpu_ms_per_tick": baseline_cpu,
                    "baseline_cpu_spread_ms_per_tick": abs(after_cpu - before_cpu),
                    "cpu_ms_per_tick_delta": delta,
                    "cpu_percent_delta": (delta / baseline_cpu * 100.0) if baseline_cpu > 0 else None,
                    "wall_ms_per_tick_delta": self._phase_wall(current) - baseline_wall,
                }
            )

        self.result["comparisons"] = comparisons
        self._write_results()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--window-seconds", type=int, default=int(os.environ.get("BENCH_WINDOW_SECONDS", "20")))
    parser.add_argument("--repeats", type=int, default=int(os.environ.get("BENCH_REPEATS", "3")))
    args = parser.parse_args()
    validator = PairedProfilerOverheadValidation(
        args.platform,
        pathlib.Path(args.bot),
        args.window_seconds,
        args.repeats,
    )
    return validator.execute_benchmark()


if __name__ == "__main__":
    raise SystemExit(main())