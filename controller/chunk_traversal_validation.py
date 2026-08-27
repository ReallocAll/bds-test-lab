#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import pathlib
from typing import Any

from controller.fleet_spark_validation import FleetSparkValidation


FLY_MIN_PUBLISHER_DISTANCE = 32.0
WALK_MIN_MOVING_DISTANCE = 1.0
WALK_REQUIRED_MAX_DISTANCE = 8.0


def collect_publisher_evidence(events: list[dict[str, Any]], expected_names: list[str]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in expected_names}
    for event in events:
        if event.get("event") != "chunk_publisher":
            continue
        bot = str(event.get("bot", ""))
        if bot not in grouped:
            continue
        if all(float(event.get(axis, 0)) == 0.0 for axis in ("x", "y", "z")):
            continue
        grouped[bot].append(event)

    bots: dict[str, dict[str, Any]] = {}
    distances: list[float] = []
    for name in expected_names:
        samples = grouped[name]
        if not samples:
            bots[name] = {"samples": 0, "horizontal_distance": 0.0, "first": None, "last": None}
            distances.append(0.0)
            continue
        first = samples[0]
        last = samples[-1]
        dx = float(last.get("x", 0)) - float(first.get("x", 0))
        dz = float(last.get("z", 0)) - float(first.get("z", 0))
        distance = math.hypot(dx, dz)
        distances.append(distance)
        bots[name] = {
            "samples": len(samples),
            "horizontal_distance": round(distance, 3),
            "first": {
                "x": first.get("x"),
                "y": first.get("y"),
                "z": first.get("z"),
                "chunk_x": first.get("chunk_x"),
                "chunk_z": first.get("chunk_z"),
                "updates": first.get("updates"),
            },
            "last": {
                "x": last.get("x"),
                "y": last.get("y"),
                "z": last.get("z"),
                "chunk_x": last.get("chunk_x"),
                "chunk_z": last.get("chunk_z"),
                "updates": last.get("updates"),
            },
        }

    return {
        "bots": bots,
        "moving_bots_ge_1": sum(distance >= WALK_MIN_MOVING_DISTANCE for distance in distances),
        "moving_bots_ge_32": sum(distance >= FLY_MIN_PUBLISHER_DISTANCE for distance in distances),
        "mean_horizontal_distance": round(sum(distances) / len(distances), 3) if distances else 0.0,
        "min_horizontal_distance": round(min(distances), 3) if distances else 0.0,
        "max_horizontal_distance": round(max(distances), 3) if distances else 0.0,
    }


def authoritative_failures(evidence: dict[str, Any], scenario: str, count: int) -> list[str]:
    bots = evidence["bots"]
    if scenario == "chunk-fly":
        return [
            name
            for name, item in bots.items()
            if int(item["samples"]) < 2 or float(item["horizontal_distance"]) < FLY_MIN_PUBLISHER_DISTANCE
        ]
    required_moving = max(1, count // 2)
    failures: list[str] = []
    if int(evidence["moving_bots_ge_1"]) < required_moving:
        failures.append(f"moving_bots<{required_moving}")
    if float(evidence["max_horizontal_distance"]) < WALK_REQUIRED_MAX_DISTANCE:
        failures.append(f"max_distance<{WALK_REQUIRED_MAX_DISTANCE:g}")
    return failures


class ChunkTraversalValidation(FleetSparkValidation):
    def run_basic_commands(self) -> None:
        assert self.server is not None
        cancel_at = self.server.command("spark profiler cancel")
        self.server.wait_command_output(cancel_at, 8)
        tps_at = self.server.command("spark tps")
        output = self.server.wait_command_output(tps_at, 8)
        if not any("TPS (5s/10s/1m/5m/15m):" in line for line in output):
            raise RuntimeError("spark tps sanity command returned no metrics")
        self.check("spark-safe-sanity", "PASS", "background profiler cancelled; spark tps responsive")

    def stop_fleet(self) -> None:
        super().stop_fleet()
        assert self.bot is not None
        events = self.bot.event_snapshot()
        evidence = collect_publisher_evidence(events, self.expected_names())
        self.result["authoritative_publisher"] = evidence

        failures = authoritative_failures(evidence, self.scenario, self.count)
        if failures:
            raise RuntimeError(f"Server-authoritative publisher traversal failed: {failures}")

        shutdown = self.result.get("fleet_shutdown_event") or {}
        stats = self.result.get("bot_stats") or []
        if self.scenario == "chunk-fly":
            not_flying = [event.get("bot") for event in stats if event.get("flying_confirmed") is not True]
            if not_flying:
                raise RuntimeError(f"Server flight acknowledgement missing for: {not_flying}")
            if int(shutdown.get("flying_confirmed", -1)) != self.count:
                raise RuntimeError(f"Fleet flight acknowledgement count mismatch: {shutdown}")

        self.check(
            "authoritative-traversal",
            "PASS",
            "NetworkChunkPublisherUpdate confirms server-side traversal",
            authoritative_publisher=evidence,
            chunks_received=int(shutdown.get("chunks_received", 0)),
            chunk_span_x=int(shutdown.get("chunk_span_x", 0)),
            chunk_span_z=int(shutdown.get("chunk_span_z", 0)),
        )
        self._write_results()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--count", required=True, type=int, choices=[1, 5, 10, 20])
    parser.add_argument("--scenario", required=True, choices=["chunk-walk", "chunk-fly"])
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()
    return ChunkTraversalValidation(
        pathlib.Path(args.bot), args.count, args.scenario, args.profile_seconds
    ).execute()


if __name__ == "__main__":
    raise SystemExit(main())
