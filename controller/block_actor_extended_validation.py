#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import re
import signal
import subprocess
import time

from controller.block_actor_validation import TARGET_X, TARGET_Y, TARGET_Z, TICKING_AREA_NAME, BlockActorValidation
from controller.bot_validation import wait_player_state
from controller.run_test import write_json

MOVEMENT_RE = re.compile(r'"movement_inputs_sent":(\d+)')


class ExtendedBlockActorValidation(BlockActorValidation):
    def __init__(self, bot_binary: pathlib.Path):
        super().__init__()
        self.bot_binary = bot_binary.resolve()
        self.bot_log = self.root / "block-actor-bot.log"
        self.result["test_kind"] = "spark-block-actor-extended-real-bds"
        self.result["movement"] = {}
        self.result["second_dimension"] = "not_attempted"
        self.result["performance_compare"] = {}
        write_json(self.result_path, self.result)

    def _start_chunk_walk_bot(self) -> tuple[subprocess.Popen[str], object]:
        cmd = [
            str(self.bot_binary),
            "--host",
            "127.0.0.1",
            "--port",
            "19132",
            "--name",
            "TestBot",
            "--scenario",
            "chunk-walk",
            "--chunk-radius",
            "8",
            "--connect-timeout",
            "15s",
            "--spawn-timeout",
            "30s",
            "--json",
        ]
        print("+", " ".join(cmd), flush=True)
        log = self.bot_log.open("w", encoding="utf-8")
        process = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
        return process, log

    @staticmethod
    def _stop_chunk_walk_bot(process: subprocess.Popen[str], log: object) -> None:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()

    def validate_player_movement(self, min_sample_count: int) -> int:
        assert self.server is not None
        before, before_samples = self.wait_world_info(
            "movement-baseline",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] == 0,
            timeout=45,
            min_sample_count=min_sample_count,
        )
        process, log = self._start_chunk_walk_bot()
        online_seconds = 0.0
        online_probes = 0
        offline_seconds = 0.0
        offline_probes = 0
        moved_sample: dict[str, object] | None = None
        moved_samples = before_samples
        try:
            _, online_seconds, online_probes = wait_player_state(self.server, True, timeout=60)
            moved_sample, moved_samples = self.wait_world_info(
                "player-movement-loaded-chunks",
                lambda sample: sample["chunks"] > before["chunks"],
                timeout=60,
                min_sample_count=before_samples,
            )
            time.sleep(12)
        finally:
            self._stop_chunk_walk_bot(process, log)
        _, offline_seconds, offline_probes = wait_player_state(self.server, False, timeout=30)

        text = self.bot_log.read_text(encoding="utf-8", errors="replace")
        movement_values = [int(match.group(1)) for match in MOVEMENT_RE.finditer(text)]
        movement_max = max(movement_values, default=0)
        if movement_max <= 0:
            raise RuntimeError("chunk-walk bot reported no movement input packets")
        if moved_sample is None:
            raise RuntimeError("chunk-walk bot produced no world-info sample")

        self.result["movement"] = {
            "chunks_before": before["chunks"],
            "chunks_after": moved_sample["chunks"],
            "movement_inputs_sent": movement_max,
            "online_convergence_seconds": round(online_seconds, 3),
            "online_probes": online_probes,
            "offline_convergence_seconds": round(offline_seconds, 3),
            "offline_probes": offline_probes,
        }
        write_json(self.result_path, self.result)
        self.check(
            "player-movement-chunk-load",
            "PASS",
            json.dumps(self.result["movement"], sort_keys=True),
            **self.result["movement"],
        )
        return moved_samples

    def validate_second_dimension(self, min_sample_count: int) -> int:
        assert self.server is not None
        name = "spark_tile_nether"
        x = 8192
        y = 64
        z = 8192
        try:
            output = self.command(f"execute in nether run tickingarea add circle {x} {y} {z} 1 {name}")
        except RuntimeError as exc:
            self.result["second_dimension"] = "unavailable"
            self.check("second-dimension", "SKIP", str(exc))
            return min_sample_count
        joined = "\n".join(output).lower()
        if "failed" in joined or "cannot" in joined or "could not" in joined:
            self.result["second_dimension"] = "unavailable"
            self.check("second-dimension", "SKIP", "Nether ticking-area probe unavailable", output=" | ".join(output[-20:]))
            return min_sample_count

        try:
            self.command(f"execute in nether run setblock {x} {y} {z} chest")
            sample, sample_count = self.wait_world_info(
                "nether-block-actor",
                lambda current: current["tile_entities_present"] and current["tile_entities"] >= 1,
                timeout=90,
                min_sample_count=min_sample_count,
            )
            self.result["second_dimension"] = "PASS"
            write_json(self.result_path, self.result)
            self.check(
                "second-dimension",
                "PASS",
                "Nether BlockActor contributed to tile_entities",
                sample=sample,
            )
            return sample_count
        finally:
            try:
                self.command(f"execute in nether run setblock {x} {y} {z} air")
            except Exception:
                pass
            try:
                self.command(f"execute in nether run tickingarea remove {name}")
            except Exception:
                pass

    def run_block_actor_lifecycle(self) -> None:
        baseline, baseline_samples = self.wait_world_info(
            "baseline-zero-present",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] == 0,
            timeout=90,
        )
        self.record_reconcile_mspt("idle-baseline")
        idle_mspt = self.result["reconcile_mspt_max"][-1]["mspt_10s_max"]

        self.command(f"tickingarea add circle {TARGET_X} {TARGET_Y} {TARGET_Z} 1 {TICKING_AREA_NAME}")
        time.sleep(3)
        self.command(f"setblock {TARGET_X} {TARGET_Y} {TARGET_Z} chest")
        placed, placed_samples = self.wait_world_info(
            "loaded-chunk-create-rise",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] >= 1,
            timeout=90,
            min_sample_count=baseline_samples,
        )

        self.command(f"setblock {TARGET_X + 1} {TARGET_Y} {TARGET_Z} furnace")
        multiple, multiple_samples = self.wait_world_info(
            "multiple-block-actors",
            lambda sample: sample["tile_entities_present"]
            and sample["tile_entities"] >= placed["tile_entities"] + 1,
            timeout=90,
            min_sample_count=placed_samples,
        )
        self.record_reconcile_mspt("multiple")
        multiple_mspt = self.result["reconcile_mspt_max"][-1]["mspt_10s_max"]

        self.command(f"setblock {TARGET_X + 1} {TARGET_Y} {TARGET_Z} air")
        decreased, decreased_samples = self.wait_world_info(
            "loaded-chunk-delete-fall",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] == placed["tile_entities"],
            timeout=90,
            min_sample_count=multiple_samples,
        )
        if decreased["tile_entities"] >= multiple["tile_entities"]:
            raise RuntimeError(f"BlockActor count did not fall after loaded-chunk delete: {multiple} -> {decreased}")

        self.command(f"tickingarea remove {TICKING_AREA_NAME}")
        unloaded, unloaded_samples = self.wait_world_info(
            "unloaded-zero-present",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] == 0,
            timeout=60,
            min_sample_count=decreased_samples,
        )
        if unloaded["tile_entities"] >= decreased["tile_entities"]:
            raise RuntimeError(f"BlockActor count did not fall after chunk unload: {decreased} -> {unloaded}")

        self.command(f"tickingarea add circle {TARGET_X} {TARGET_Y} {TARGET_Z} 1 {TICKING_AREA_NAME}")
        incomplete, incomplete_samples = self.wait_world_info(
            "reload-presence-cleared",
            lambda sample: not sample["tile_entities_present"],
            timeout=35,
            min_sample_count=unloaded_samples,
        )
        if incomplete["tile_entities_present"]:
            raise RuntimeError(f"tile_entities presence stayed true while reloaded chunk awaited reconciliation: {incomplete}")

        reloaded, reloaded_samples = self.wait_world_info(
            "reload-reconciled-nonzero",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] >= placed["tile_entities"],
            timeout=90,
            min_sample_count=incomplete_samples,
        )
        self.record_reconcile_mspt("reloaded")
        reconcile_mspt = self.result["reconcile_mspt_max"][-1]["mspt_10s_max"]
        self.result["performance_compare"] = {
            "idle_10s_max_mspt": idle_mspt,
            "multiple_10s_max_mspt": multiple_mspt,
            "reconcile_10s_max_mspt": reconcile_mspt,
        }
        write_json(self.result_path, self.result)
        if reconcile_mspt > max(100.0, idle_mspt + 100.0):
            raise RuntimeError(
                f"BlockActor reconciliation caused an obvious tick stall: idle={idle_mspt}ms reconcile={reconcile_mspt}ms"
            )
        self.check("reconcile-performance-compare", "PASS", **self.result["performance_compare"])

        self.command(f"setblock {TARGET_X} {TARGET_Y} {TARGET_Z} air")
        self.command(f"tickingarea remove {TICKING_AREA_NAME}")
        clean, clean_samples = self.wait_world_info(
            "cleanup-zero-present",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] == 0,
            timeout=60,
            min_sample_count=reloaded_samples,
        )
        if clean["tile_entities"] != 0:
            raise RuntimeError(f"BlockActor cleanup did not converge to zero: {clean}")

        dimension_samples = self.validate_second_dimension(clean_samples)
        movement_samples = self.validate_player_movement(dimension_samples)
        self.check(
            "block-actor-extended-lifecycle",
            "PASS",
            "real BDS covered zero/nonzero, multiple actors, loaded-chunk create/delete, unload/reload presence, optional second dimension, and player movement",
            final_sample_count=movement_samples,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    args = parser.parse_args()
    return ExtendedBlockActorValidation(pathlib.Path(args.bot)).execute_validation()


if __name__ == "__main__":
    raise SystemExit(main())
