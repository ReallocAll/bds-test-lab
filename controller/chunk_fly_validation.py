#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import pathlib
import time
from typing import Any

from controller.fleet_spark_validation import FleetSparkValidation, set_server_property


class ChunkFlySparkValidation(FleetSparkValidation):
    def __init__(self, bot_binary: pathlib.Path, count: int, profile_seconds: int):
        super().__init__(bot_binary, count, "chunk-fly", profile_seconds)
        self.result["chunk_fly_evidence"] = None
        self._write_results()

    def bootstrap_offline_server(self) -> None:
        # Diagnostic: force the current BDS server-authoritative movement checker
        # to its strictest useful settings. If PlayerAuthInput is being simulated,
        # a bad client prediction should now produce corrections instead of being
        # silently tolerated. This distinguishes invalid input-state replay from
        # merely inaccurate local prediction.
        super().bootstrap_offline_server()
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop before strict movement diagnostic")
        self.server.close()
        self.server = None
        properties = self.server_dir / "server.properties"
        set_server_property(properties, "server-authoritative-movement-strict", "true")
        set_server_property(properties, "player-position-acceptance-threshold", "0.01")
        self.check(
            "chunk-fly-strict-movement-diagnostic",
            "PASS",
            "server-authoritative-movement-strict=true, player-position-acceptance-threshold=0.01",
        )
        self.start_server()
        self.wait_post_start_initialization()

    def start_fleet(self) -> None:
        super().start_fleet()
        assert self.bot is not None

        expected = set(self.expected_names())
        deadline = time.monotonic() + 30
        confirmed: set[str] = set()
        while time.monotonic() < deadline:
            for event in self.bot.event_snapshot():
                if event.get("event") == "flight_state" and event.get("flying") is True:
                    bot = str(event.get("bot", ""))
                    if bot in expected:
                        confirmed.add(bot)
            if confirmed == expected:
                break
            if not self.bot.is_alive():
                raise RuntimeError("Bot fleet exited while waiting for BDS flight acknowledgement")
            time.sleep(0.25)

        missing = sorted(expected - confirmed)
        if missing:
            raise RuntimeError(f"BDS did not acknowledge creative flight for bots: {missing}")
        self.check(
            "chunk-fly-server-flight",
            "PASS",
            f"BDS acknowledged creative flying for all {self.count} bots",
            confirmed=sorted(confirmed),
        )

    @staticmethod
    def _horizontal_between(a: Any, b: Any) -> float | None:
        if not isinstance(a, list) or not isinstance(b, list) or len(a) < 3 or len(b) < 3:
            return None
        return math.hypot(float(b[0]) - float(a[0]), float(b[2]) - float(a[2]))

    def stop_fleet(self) -> None:
        super().stop_fleet()
        assert self.bot is not None

        stats: list[dict[str, Any]] = self.result.get("bot_stats") or []
        if len(stats) != self.count:
            raise RuntimeError(f"Expected {self.count} chunk-fly bot_stats events, got {len(stats)}")

        events = self.bot.event_snapshot()
        progress_by_bot: dict[str, list[dict[str, Any]]] = {name: [] for name in self.expected_names()}
        post_move_by_bot: dict[str, list[dict[str, Any]]] = {name: [] for name in self.expected_names()}
        for event in events:
            bot = str(event.get("bot", ""))
            if bot not in progress_by_bot:
                continue
            if event.get("event") == "bot_progress":
                progress_by_bot[bot].append(event)
            elif event.get("event") == "server_post_move":
                post_move_by_bot[bot].append(event)

        evidence: list[dict[str, Any]] = []
        bad: list[dict[str, Any]] = []
        for event in stats:
            bot = str(event.get("bot", ""))
            samples = progress_by_bot.get(bot, [])
            post_moves = post_move_by_bot.get(bot, [])
            auth = int(event.get("auth_inputs_sent", 0))
            movement = int(event.get("movement_inputs_sent", 0))
            chunks = int(event.get("chunks_received", 0))
            distance = float(event.get("horizontal_distance", 0.0))
            flying = event.get("flying_confirmed") is True
            span_x = int(event.get("chunk_span_x", 0))
            span_z = int(event.get("chunk_span_z", 0))

            entry: dict[str, Any] = {
                "bot": bot,
                "progress_samples": len(samples),
                "auth_inputs_sent": auth,
                "movement_inputs_sent": movement,
                "chunks_received": chunks,
                "horizontal_distance": distance,
                "flying_confirmed": flying,
                "chunk_span_x": span_x,
                "chunk_span_z": span_z,
                "server_post_move_events": len(post_moves),
            }
            if post_moves:
                first_post = post_moves[0].get("position")
                final_post = post_moves[-1].get("position")
                entry["server_post_move_first_position"] = first_post
                entry["server_post_move_final_position"] = final_post
                entry["server_post_move_horizontal_distance"] = self._horizontal_between(first_post, final_post)

            valid = (
                event.get("scenario") == "chunk-fly"
                and flying
                and auth > 0
                and movement > 0
                and chunks > 0
                and distance > 0
                and len(samples) >= 4
            )

            if len(samples) >= 4:
                late_index = min(len(samples) - 2, max(1, (len(samples) * 2) // 3))
                late = samples[late_index]
                final = samples[-1]
                late_chunks = int(late.get("chunks_received", 0))
                final_chunks = int(final.get("chunks_received", 0))
                late_distance = float(late.get("horizontal_distance", 0.0))
                final_distance = float(final.get("horizontal_distance", 0.0))
                late_chunk_growth = final_chunks - late_chunks
                late_distance_growth = final_distance - late_distance
                late_post = late.get("server_post_move_position")
                final_post = final.get("server_post_move_position")
                post_move_late_growth = self._horizontal_between(late_post, final_post)
                entry.update(
                    {
                        "late_sample_index": late_index,
                        "late_chunks_received": late_chunks,
                        "final_progress_chunks_received": final_chunks,
                        "late_chunk_growth": late_chunk_growth,
                        "late_horizontal_distance": late_distance,
                        "final_progress_horizontal_distance": final_distance,
                        "late_horizontal_growth": late_distance_growth,
                        "late_server_post_move_position": late_post,
                        "final_server_post_move_position": final_post,
                        "late_server_post_move_horizontal_growth": post_move_late_growth,
                        "server_post_move_updates": int(final.get("server_post_move_updates", 0)),
                    }
                )
                valid = valid and late_chunk_growth > 0 and late_distance_growth > 0

            if not valid:
                bad.append(entry)
            evidence.append(entry)

        self.result["chunk_fly_evidence"] = evidence
        self._write_results()
        if bad:
            raise RuntimeError(f"Chunk-fly traversal evidence failed for {len(bad)} bots: {bad[:3]}")

        shutdown: dict[str, Any] = self.result.get("fleet_shutdown_event") or {}
        if int(shutdown.get("flying_confirmed", -1)) != self.count:
            raise RuntimeError(f"Fleet shutdown did not retain flight confirmation: {shutdown}")
        min_distance = float(shutdown.get("min_horizontal_distance", 0.0))
        if min_distance <= 0:
            raise RuntimeError(f"Fleet minimum horizontal distance did not advance: {min_distance}")

        self.check(
            "chunk-fly-sustained-traversal",
            "PASS",
            f"all {self.count} bots remained airborne and continued chunk traversal in the late window",
            min_horizontal_distance=min_distance,
            evidence=evidence,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--count", required=True, type=int, choices=[1, 5, 10, 20])
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()
    return ChunkFlySparkValidation(pathlib.Path(args.bot), args.count, args.profile_seconds).execute()


if __name__ == "__main__":
    raise SystemExit(main())
