#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import re
import signal
import subprocess
import time
import traceback

from controller.block_actor_validation import (
    SAMPLE_TIMEOUT_SECONDS,
    TARGET_X,
    TARGET_Y,
    TARGET_Z,
    BlockActorValidation,
    TileSample,
)
from controller.bot_validation import wait_player_state
from controller.run_test import now_iso, write_json

MOVEMENT_RE = re.compile(r'"movement_inputs_sent":(\d+)')


class ExtendedBlockActorValidation(BlockActorValidation):
    def __init__(self, spark_sha: str, bot_binary: pathlib.Path):
        super().__init__(spark_sha)
        self.bot_binary = bot_binary.resolve()
        self.bot_log = self.root / "block-actor-bot.log"
        self.result["test_kind"] = "spark-block-actor-extended-validation"
        self.result["performance"] = {}
        self.result["second_dimension"] = "not_attempted"
        self.result["movement"] = {}
        write_json(self.result_path, self.result)

    def command_output(self, command: str, timeout: float = 8.0) -> list[str]:
        assert self.server is not None
        start = self.server.command(command)
        return self.server.wait_command_output(start, timeout)

    @staticmethod
    def command_succeeded(output: list[str]) -> bool:
        text = "\n".join(output).lower()
        failure_markers = (
            "unknown command",
            "syntax error",
            "failed to execute",
            "could not execute",
            "no targets matched selector",
        )
        return not any(marker in text for marker in failure_markers)

    def wait_exact_count(self, expected: int, timeout: float = SAMPLE_TIMEOUT_SECONDS) -> TileSample:
        return self.wait_for_sample(
            lambda sample: sample.present and sample.value == expected,
            timeout,
            f"tile_entities={expected} with presence",
        )

    def start_chunk_walk_bot(self) -> subprocess.Popen[str]:
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
        setattr(process, "_spark_validation_log", log)
        return process

    @staticmethod
    def stop_chunk_walk_bot(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError("chunk-walk bot did not stop after SIGTERM")
        log = getattr(process, "_spark_validation_log", None)
        if log is not None:
            log.close()
        if process.returncode not in (0, None):
            raise RuntimeError(f"chunk-walk bot exited with code {process.returncode}")

    def validate_player_movement(self) -> None:
        assert self.server is not None
        before = self.capture()
        process = self.start_chunk_walk_bot()
        try:
            _, online_seconds, probes = wait_player_state(self.server, True, timeout=60)
            sample = self.wait_for_sample(
                lambda current: current.present and current.chunks > before.chunks,
                60,
                "player movement to load chunks",
            )
            time.sleep(12)
            self.stop_chunk_walk_bot(process)
            _, offline_seconds, offline_probes = wait_player_state(self.server, False, timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            log = getattr(process, "_spark_validation_log", None)
            if log is not None and not log.closed:
                log.close()

        text = self.bot_log.read_text(encoding="utf-8", errors="replace")
        movement_values = [int(match.group(1)) for match in MOVEMENT_RE.finditer(text)]
        movement_max = max(movement_values, default=0)
        if movement_max <= 0:
            raise RuntimeError("chunk-walk bot reported no movement input packets")
        self.result["movement"] = {
            "chunks_before": before.chunks,
            "chunks_after": sample.chunks,
            "movement_inputs_sent": movement_max,
            "online_convergence_seconds": round(online_seconds, 3),
            "online_probes": probes,
            "offline_convergence_seconds": round(offline_seconds, 3),
            "offline_probes": offline_probes,
        }
        self.check("player-movement-chunk-load", "PASS", **self.result["movement"])

    def validate_second_dimension(self) -> None:
        assert self.server is not None
        name = "spark_tile_nether"
        x = 8192
        y = 64
        z = 8192
        add = self.command_output(f"execute in nether run tickingarea add {x} 0 {z} {x} 255 {z} {name} true")
        if not self.command_succeeded(add):
            self.result["second_dimension"] = "unavailable"
            self.check(
                "second-dimension",
                "SKIP",
                "BDS command surface did not permit a Nether ticking-area probe",
                output=" | ".join(add[-20:]),
            )
            return

        try:
            self.server.command(f"execute in nether run setblock {x} {y} {z} chest")
            sample = self.wait_for_sample(
                lambda current: current.present and current.value is not None and current.value >= 1,
                SAMPLE_TIMEOUT_SECONDS,
                "Nether BlockActor reconciliation",
            )
            self.result["second_dimension"] = "PASS"
            self.check("second-dimension", "PASS", "Nether chest contributed to tile_entities", sample=sample.__dict__)
        finally:
            self.server.command(f"execute in nether run setblock {x} {y} {z} air")
            self.server.command(f"execute in nether run tickingarea remove {name}")

    def execute(self) -> int:
        stage = "initialization"
        ticking_area = "spark_tile_test"
        bot_process: subprocess.Popen[str] | None = None
        try:
            stage = "artifacts"
            self.setup_endstone()
            self.install_spark()
            self.write_config()
            self.capture_server.start()
            self.start_server()
            assert self.server is not None

            stage = "idle-baseline"
            time.sleep(5)
            idle_mspt = self.sample_10s_max_mspt()
            baseline = self.wait_exact_count(0)
            self.result["performance"]["idle_10s_max_mspt"] = idle_mspt
            self.check("fresh-zero", "PASS", "fresh world reports a real zero with field presence", sample=baseline.__dict__)

            stage = "loaded-chunk-create"
            self.server.command(
                f"tickingarea add {TARGET_X} 0 {TARGET_Z} {TARGET_X} 255 {TARGET_Z} {ticking_area} true"
            )
            time.sleep(2)
            self.server.command(f"setblock {TARGET_X} {TARGET_Y} {TARGET_Z} chest")
            one = self.wait_exact_count(1)
            self.check("loaded-chunk-create-rise", "PASS", sample=one.__dict__)

            stage = "multiple-block-actors"
            self.server.command(f"setblock {TARGET_X + 1} {TARGET_Y} {TARGET_Z} furnace")
            multiple = self.wait_for_sample(
                lambda sample: sample.present and sample.value is not None and sample.value >= 2,
                SAMPLE_TIMEOUT_SECONDS,
                "multiple BlockActors",
            )
            multi_mspt = self.sample_10s_max_mspt()
            self.result["performance"]["multiple_10s_max_mspt"] = multi_mspt
            self.check("multiple-block-actors", "PASS", sample=multiple.__dict__, max_mspt=multi_mspt)

            stage = "loaded-chunk-delete"
            self.server.command(f"setblock {TARGET_X + 1} {TARGET_Y} {TARGET_Z} air")
            decreased = self.wait_exact_count(1)
            self.check("loaded-chunk-delete-fall", "PASS", sample=decreased.__dict__)

            stage = "chunk-unload"
            self.server.command(f"tickingarea remove {ticking_area}")
            unloaded = self.wait_exact_count(0, 30)
            self.check("chunk-unload", "PASS", sample=unloaded.__dict__)

            stage = "chunk-reload-unavailable"
            self.server.command(
                f"tickingarea add {TARGET_X} 0 {TARGET_Z} {TARGET_X} 255 {TARGET_Z} {ticking_area} true"
            )
            unavailable = self.wait_for_sample(
                lambda sample: sample.chunks > 0 and not sample.present,
                30,
                "tile_entities presence to clear after chunk reload",
            )
            self.check("reload-unavailable", "PASS", sample=unavailable.__dict__)

            stage = "chunk-reload-reconcile"
            reconciled = self.wait_exact_count(1)
            reconcile_mspt = self.sample_10s_max_mspt()
            self.result["performance"]["reconcile_10s_max_mspt"] = reconcile_mspt
            self.check("reload-reconciled", "PASS", sample=reconciled.__dict__, max_mspt=reconcile_mspt)

            if reconcile_mspt > max(50.0, idle_mspt + 50.0):
                raise RuntimeError(
                    f"tile_entities reconciliation caused an obvious tick stall: idle max={idle_mspt:.2f}ms, "
                    f"reconcile max={reconcile_mspt:.2f}ms"
                )
            self.check(
                "reconcile-performance",
                "PASS",
                idle_max_mspt=idle_mspt,
                multiple_max_mspt=multi_mspt,
                reconcile_max_mspt=reconcile_mspt,
            )

            stage = "overworld-cleanup"
            self.server.command(f"setblock {TARGET_X} {TARGET_Y} {TARGET_Z} air")
            self.server.command(f"tickingarea remove {ticking_area}")
            self.wait_exact_count(0, 30)

            stage = "second-dimension"
            self.validate_second_dimension()

            stage = "player-movement"
            self.validate_player_movement()

            stage = "shutdown"
            if not self.server.graceful_stop(60):
                raise RuntimeError("BDS did not stop gracefully")
            self.server.close()
            self.server = None
            self.result["shutdown_status"] = "graceful"
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1200]
            self.diagnostics.write_text(traceback.format_exc(), encoding="utf-8")
            if bot_process is not None and bot_process.poll() is None:
                bot_process.kill()
            if self.server is not None and self.server.is_alive():
                self.server.force_kill_tree()
                self.server.close()
                self.server = None
                self.result["shutdown_status"] = "forced_after_failure"
            return 1
        finally:
            self.capture_server.stop()
            self.result["completed_at"] = now_iso()
            write_json(self.result_path, self.result)
            print(self.result, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spark-sha", required=True)
    parser.add_argument("--bot", required=True)
    args = parser.parse_args()
    return ExtendedBlockActorValidation(args.spark_sha, pathlib.Path(args.bot)).execute()


if __name__ == "__main__":
    raise SystemExit(main())
