#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import shutil
import time
import traceback

from controller.bot_validation import patch_server_properties
from controller.fleet_spark_validation import set_server_property
from controller.release_validation import SparkReleaseValidation
from controller.run_test import now_iso, write_json

# Keep the validation BlockActor in the spawn area that the real TestBot has
# already loaded. This avoids treating a remote ticking-area generation delay
# as a Spark failure and makes the same probe deterministic across world seeds.
TARGET_X = 0
TARGET_Y = 100
TARGET_Z = 0
TICKING_AREA_NAME = "spark_pr11_seed_report"
RECONCILE_WAIT_SECONDS = 80


class PR11SeedReportValidation(SparkReleaseValidation):
    def __init__(self, seed: int, bot_binary: pathlib.Path):
        super().__init__("linux", 30, bot_binary)
        self.seed = seed
        suffix = f"neg{abs(seed)}" if seed < 0 else str(seed)
        self.world_name = f"PR11SeedReport_{suffix}"
        self.result.update(
            {
                "test_kind": "spark-pr11-seeded-online-report",
                "seed": seed,
                "world_name": self.world_name,
                "block_actor_target": {"x": TARGET_X, "y": TARGET_Y, "z": TARGET_Z},
                "reconcile_wait_seconds": RECONCILE_WAIT_SECONDS,
                "report_viewer_url": None,
            }
        )
        write_json(self.result_path, self.result)

    @staticmethod
    def _command_failed(lines: list[str]) -> bool:
        text = "\n".join(lines).lower()
        return any(
            marker in text
            for marker in (
                " error]",
                "failed",
                "cannot place",
                "could not",
                "couldn't",
                "does not match",
                "expected:",
            )
        )

    def bootstrap_seed_world(self) -> None:
        self.start_server()
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop after initial server.properties bootstrap")
        self.server.close()
        self.server = None

        properties = self.server_dir / "server.properties"
        patch_server_properties(properties)
        set_server_property(properties, "level-name", self.world_name)
        set_server_property(properties, "level-type", "DEFAULT")
        set_server_property(properties, "level-seed", str(self.seed))
        set_server_property(properties, "allow-cheats", "true")
        set_server_property(properties, "player-idle-timeout", "0")

        world = self.server_dir / "worlds" / self.world_name
        if world.exists():
            shutil.rmtree(world)

        text = properties.read_text(encoding="utf-8", errors="replace")
        if f"level-seed={self.seed}" not in text or f"level-name={self.world_name}" not in text:
            raise RuntimeError("seeded server.properties did not retain requested level seed/name")
        self.check(
            "seeded-world-config",
            "PASS",
            "fresh DEFAULT world configured with requested seed",
            seed=self.seed,
            world_name=self.world_name,
        )
        self.start_server()

    def place_block_actor_and_wait(self) -> None:
        assert self.server is not None
        output = self.command_check(
            "seeded-ticking-area",
            f"tickingarea add circle {TARGET_X} 64 {TARGET_Z} 1 {TICKING_AREA_NAME}",
        )
        if self._command_failed(output):
            raise RuntimeError("BDS rejected ticking area: " + " | ".join(output[-20:]))
        time.sleep(3)

        output = self.command_check(
            "seeded-block-actor-place",
            f"setblock {TARGET_X} {TARGET_Y} {TARGET_Z} chest",
        )
        if self._command_failed(output) or not any("block placed" in line.lower() for line in output):
            raise RuntimeError("BDS failed to place BlockActor chest: " + " | ".join(output[-20:]))

        verify = self.command_check(
            "seeded-block-actor-verify",
            f"testforblock {TARGET_X} {TARGET_Y} {TARGET_Z} chest",
        )
        if self._command_failed(verify) or not any("successfully found the block" in line.lower() for line in verify):
            raise RuntimeError("BDS did not confirm BlockActor chest: " + " | ".join(verify[-20:]))

        deadline = time.monotonic() + RECONCILE_WAIT_SECONDS
        while time.monotonic() < deadline:
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while waiting for BlockActor reconciliation")
            if self.bot is None or not self.bot.is_alive():
                raise RuntimeError("TestBot disconnected while waiting for BlockActor reconciliation")
            time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))

        self.validate_player_health()
        self.command_check("seeded-post-reconcile-tps", "spark tps")
        # Re-confirm the chest immediately before uploading so the report cannot
        # be generated after the validation BlockActor disappeared.
        verify = self.command_check(
            "seeded-block-actor-pre-upload-verify",
            f"testforblock {TARGET_X} {TARGET_Y} {TARGET_Z} chest",
        )
        if self._command_failed(verify) or not any("successfully found the block" in line.lower() for line in verify):
            raise RuntimeError("BlockActor chest was not present immediately before health upload")
        self.check(
            "seeded-block-actor-reconciled-window",
            "PASS",
            "BlockActor chest remained loaded for longer than one full 60s world-gauge reconciliation interval while TestBot stayed online",
            seed=self.seed,
            wait_seconds=RECONCILE_WAIT_SECONDS,
        )

    def cleanup_block_actor(self) -> None:
        if self.server is None or not self.server.is_alive():
            return
        try:
            self.command_check("seeded-block-actor-remove", f"setblock {TARGET_X} {TARGET_Y} {TARGET_Z} air")
        except Exception:
            pass
        try:
            self.command_check("seeded-ticking-area-remove", f"tickingarea remove {TICKING_AREA_NAME}")
        except Exception:
            pass

    def execute_report(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            self.verify_endstone_version()

            stage = "seeded-world-bootstrap"
            self.bootstrap_seed_world()
            assert self.server is not None

            stage = "real-player-online"
            self.start_real_player()
            self.validate_player_health()

            stage = "block-actor-placement-and-reconcile"
            self.place_block_actor_and_wait()

            stage = "online-health-report"
            self.validate_health_upload()
            self.result["report_viewer_url"] = self.result.get("health_upload_viewer_url")
            if not self.result["report_viewer_url"]:
                raise RuntimeError("Spark health upload completed without a viewer URL")
            self.check(
                "seeded-online-report",
                "PASS",
                seed=self.seed,
                viewer_url=self.result["report_viewer_url"],
                bot_online=True,
            )

            stage = "cleanup"
            self.cleanup_block_actor()
            self.stop_real_player()

            stage = "shutdown"
            self.shutdown()
            if self.result.get("shutdown_status") != "graceful":
                raise RuntimeError(f"Final shutdown was not graceful: {self.result.get('shutdown_status')}")

            self.result["status"] = "PASS"
            self.result["state"] = "completed"
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
            return 1
        finally:
            if self.bot is not None and self.bot.is_alive():
                self.bot.force_close()
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)
            print(self.result, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--bot", required=True)
    args = parser.parse_args()
    return PR11SeedReportValidation(args.seed, pathlib.Path(args.bot)).execute_report()


if __name__ == "__main__":
    raise SystemExit(main())
