#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import time
import traceback
from typing import Any

from controller.bot_validation import BotProcess, patch_server_properties, require_player
from controller.run_test import IntegrationTest, now_iso, write_json


class WindowsSamplerShutdownValidation(IntegrationTest):
    def __init__(self, bot_binary: pathlib.Path, settle_seconds: int):
        super().__init__("windows")
        self.bot_binary = bot_binary.resolve()
        self.settle_seconds = max(5, settle_seconds)
        self.bot_log = self.root / "bot-windows-sampler.log"
        self.bot: BotProcess | None = None
        self.result.update(
            {
                "test_kind": "windows-sampler-active-shutdown-real-player",
                "bot_online_event": None,
                "active_profile_shutdown_seconds": None,
            }
        )
        write_json(self.result_path, self.result)

    def check(self, name: str, status: str, detail: str | None = None, **extra: Any) -> None:
        super().check(name, status, detail, **extra)
        write_json(self.result_path, self.result)

    def bootstrap_offline_server(self) -> None:
        self.start_server()
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop after server.properties bootstrap")
        self.server.close()
        self.server = None
        patch_server_properties(self.server_dir / "server.properties")
        self.check("offline-bot-server-properties", "PASS")
        self.start_server()

    def start_bot(self) -> None:
        assert self.server is not None
        self.bot = BotProcess(self.bot_binary, self.bot_log)
        self.bot.start()
        online = self.bot.wait_event("online", 60)
        self.result["bot_online_event"] = online
        write_json(self.result_path, self.result)
        output = require_player(self.server, True)
        self.check("bot-real-player-visible", "PASS", output=" | ".join(output[-20:]), online_event=online)

    def shutdown_with_active_profiler(self) -> None:
        assert self.server is not None
        start = self.server.command("spark profiler start --timeout 120")
        output = self.server.wait_command_output(start, 8)
        if not self.server.is_alive():
            raise RuntimeError("BDS exited while starting active-shutdown profiler")
        joined = "\n".join(output).lower()
        if "already running" in joined or "failed" in joined or "error" in joined:
            raise RuntimeError("Spark profiler did not start cleanly before shutdown: " + " | ".join(output[-20:]))
        self.check("active-profiler-started", "PASS", output=" | ".join(output[-20:]))

        time.sleep(3)
        info_start = self.server.command("spark profiler info")
        info = self.server.wait_command_output(info_start, 8)
        if not self.server.is_alive():
            raise RuntimeError("BDS exited before active-profiler shutdown")
        self.check("active-profiler-confirmed", "PASS", output=" | ".join(info[-20:]))

        started = time.monotonic()
        graceful = self.server.graceful_stop(60)
        elapsed = time.monotonic() - started
        self.result["active_profile_shutdown_seconds"] = round(elapsed, 3)
        write_json(self.result_path, self.result)
        if not graceful:
            self.server.force_kill_tree()
            self.result["shutdown_status"] = "forced"
            write_json(self.result_path, self.result)
            raise RuntimeError(f"BDS did not stop within 60s while Spark profiler was active (elapsed={elapsed:.3f}s)")

        self.server.close()
        self.result["shutdown_status"] = "graceful"
        write_json(self.result_path, self.result)
        leftovers = self.residual_processes()
        if leftovers:
            raise RuntimeError("Residual BDS process after active-profiler shutdown: " + " | ".join(leftovers[:5]))
        self.check(
            "active-profiler-shutdown",
            "PASS",
            "BDS exited gracefully with a real player online while execution sampling was active",
            elapsed_seconds=round(elapsed, 3),
        )
        self.server = None

    def execute(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()

            stage = "bds-bootstrap"
            self.bootstrap_offline_server()
            assert self.server is not None

            stage = "spark-sanity"
            self.run_basic_commands()

            stage = "bot-connect"
            self.start_bot()

            stage = "bot-settle"
            time.sleep(self.settle_seconds)
            self.command_check("bot-spark-tps", "spark tps")
            self.command_check("bot-spark-health", "spark health")

            stage = "active-profiler-shutdown"
            self.shutdown_with_active_profiler()

            stage = "restart-after-active-shutdown"
            if self.bot is not None and self.bot.is_alive():
                self.bot.force_close()
            self.bot = None
            self.start_server()
            self.run_basic_commands()
            self.shutdown()
            self.server = None
            self.check("restart-after-active-shutdown", "PASS")

            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            write_json(self.result_path, self.result)
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
            last_lines = self.server.snapshot()[-250:] if self.server is not None else []
            self.diagnostics.write_text(
                diagnostic + "\n\nLast BDS log lines:\n" + "\n".join(last_lines),
                encoding="utf-8",
            )
            write_json(self.result_path, self.result)
            return 1
        finally:
            if self.bot is not None and self.bot.is_alive():
                self.bot.force_close()
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--settle-seconds", type=int, default=15)
    args = parser.parse_args()
    return WindowsSamplerShutdownValidation(pathlib.Path(args.bot), args.settle_seconds).execute()


if __name__ == "__main__":
    raise SystemExit(main())
