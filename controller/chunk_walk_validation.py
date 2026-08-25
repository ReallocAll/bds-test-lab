#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
from typing import Any

from controller.fleet_spark_validation import FleetSparkValidation


class ChunkWalkSparkValidation(FleetSparkValidation):
    def __init__(self, bot_binary: pathlib.Path, count: int, profile_seconds: int):
        super().__init__(bot_binary, count, "chunk-walk", profile_seconds)

    def stop_fleet(self) -> None:
        super().stop_fleet()

        stats: list[dict[str, Any]] = self.result.get("bot_stats") or []
        if len(stats) != self.count:
            raise RuntimeError(f"Expected {self.count} chunk-walk bot_stats events, got {len(stats)}")

        movement_total = 0
        auth_total = 0
        chunk_total = 0
        min_ratio = 1.0
        bad: list[dict[str, Any]] = []
        for event in stats:
            movement = int(event.get("movement_inputs_sent", 0))
            auth = int(event.get("auth_inputs_sent", 0))
            chunks = int(event.get("chunks_received", 0))
            ratio = movement / auth if auth else 0.0
            movement_total += movement
            auth_total += auth
            chunk_total += chunks
            min_ratio = min(min_ratio, ratio)
            if (
                event.get("scenario") != "chunk-walk"
                or movement <= 0
                or auth <= 0
                or ratio < 0.80
                or movement < self.profile_seconds * 10
                or chunks <= 0
            ):
                bad.append(event)

        if bad:
            raise RuntimeError(f"Chunk-walk movement evidence failed for {len(bad)} bots: {bad[:3]}")

        shutdown: dict[str, Any] = self.result.get("fleet_shutdown_event") or {}
        aggregate_movement = int(shutdown.get("movement_inputs_sent", -1))
        if aggregate_movement != movement_total:
            raise RuntimeError(
                "Fleet movement aggregate mismatch: "
                f"shutdown={aggregate_movement}, per_bot={movement_total}"
            )

        self.check(
            "chunk-walk-movement",
            "PASS",
            f"all {self.count} bots sustained forward PlayerAuthInput movement",
            movement_inputs_sent=movement_total,
            auth_inputs_sent=auth_total,
            min_movement_ratio=round(min_ratio, 4),
            chunks_received=chunk_total,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--count", required=True, type=int, choices=[1, 5, 10, 20])
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()
    return ChunkWalkSparkValidation(pathlib.Path(args.bot), args.count, args.profile_seconds).execute()


if __name__ == "__main__":
    raise SystemExit(main())
