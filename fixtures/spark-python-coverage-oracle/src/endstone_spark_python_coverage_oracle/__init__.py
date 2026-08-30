from __future__ import annotations

import json
import os
from pathlib import Path
from time import perf_counter_ns

from endstone.plugin import Plugin

WINDOW_NS = 20_000_000
WRITE_EVERY = 128


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    part = index - lower
    return round(ordered[lower] * (1.0 - part) + ordered[upper] * part)


class CoveragePlugin(Plugin):
    """Keep one Python callback on the shadow stack for a fixed wall window."""

    api_version = "0.11"

    def on_enable(self) -> None:
        self.stats_path = Path(os.environ.get("SPARK_PYTHON_COVERAGE_STATS", "coverage-counters.json"))
        self.invocation_count = 0
        self.nested_call_count = 0
        self.elapsed_ns_total = 0
        self.elapsed_ns: list[int] = []
        self.window_starts_ns: list[int] = []
        self.window_ends_ns: list[int] = []
        self.window_records: list[dict[str, int]] = []
        self._write_stats()
        self.server.scheduler.run_task(self, self.fixed_window_tick, delay=0, period=1)
        self.logger.info("Spark Python coverage oracle enabled: window_ns=20000000 period_ticks=1")

    def _write_stats(self) -> None:
        durations = self.elapsed_ns
        if durations:
            first_start = self.window_starts_ns[0]
            last_end = self.window_ends_ns[-1]
            active_seconds = max(0.0, (last_end - first_start) / 1_000_000_000.0)
        else:
            active_seconds = 0.0
        payload = {
            "module": "endstone_spark_python_coverage_oracle",
            "tick_method": "CoveragePlugin.fixed_window_tick",
            "nested_method": "CoveragePlugin.fixed_window_tick.<locals>.nested_call",
            "window_ns": WINDOW_NS,
            "invocation_count": self.invocation_count,
            "nested_call_count": self.nested_call_count,
            "elapsed_ns_total": self.elapsed_ns_total,
            "elapsed_ns": {
                "count": len(durations),
                "min": min(durations) if durations else 0,
                "max": max(durations) if durations else 0,
                "mean": (self.elapsed_ns_total / len(durations)) if durations else 0.0,
                "p50": _percentile(durations, 0.50),
                "p95": _percentile(durations, 0.95),
            },
            "active_seconds": active_seconds,
            "invocation_rate_hz": self.invocation_count / active_seconds if active_seconds else 0.0,
            "window_intervals_ns": [
                current - previous for previous, current in zip(self.window_starts_ns, self.window_starts_ns[1:])
            ],
            "clock": {
                "name": "monotonic_ns",
                "source": "time.perf_counter_ns",
                "unit": "ns",
            },
            "counter_scope": "process-cumulative-until-plugin-shutdown",
            "window_records": self.window_records,
        }
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.stats_path.with_suffix(self.stats_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.stats_path)

    def fixed_window_tick(self) -> int:
        window_start = perf_counter_ns()
        value = self.invocation_count
        nested_count_at_start = self.nested_call_count

        def nested_call(seed: int) -> int:
            return ((seed * 33) ^ (seed >> 3) ^ 0x9E37) & 0xFFFFFFFF

        while perf_counter_ns() - window_start < WINDOW_NS:
            value ^= nested_call(self.nested_call_count)
            self.nested_call_count += 1

        window_end = perf_counter_ns()
        elapsed = window_end - window_start
        nested_calls = self.nested_call_count - nested_count_at_start
        self.invocation_count += 1
        self.elapsed_ns_total += elapsed
        self.elapsed_ns.append(elapsed)
        self.window_starts_ns.append(window_start)
        self.window_ends_ns.append(window_end)
        self.window_records.append(
            {
                "start_ns": window_start,
                "end_ns": window_end,
                "elapsed_ns": elapsed,
                "nested_call_count": nested_calls,
            }
        )
        if self.invocation_count % WRITE_EVERY == 0:
            self._write_stats()
        return value

    def on_disable(self) -> None:
        self._write_stats()
        self.logger.info(
            f"Spark Python coverage oracle counters: invocations={self.invocation_count} "
            f"nested_calls={self.nested_call_count} elapsed_ns_total={self.elapsed_ns_total}"
        )
