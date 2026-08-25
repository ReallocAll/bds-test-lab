#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import time
import traceback
from typing import Any

from controller.bot_validation import BotProcess, patch_server_properties, require_player, wait_player_state
from controller.run_test import IntegrationTest, now_iso, write_json


class SparkBotValidation(IntegrationTest):
    def __init__(self, bot_binary: pathlib.Path, profile_seconds: int):
        super().__init__("linux")
        self.bot_binary = bot_binary.resolve()
        self.profile_seconds = max(20, profile_seconds)
        self.bot_log = self.root / "bot-spark.log"
        self.spark_bot_result = self.root / "spark-bot-result.json"
        self.bot: BotProcess | None = None
        self.result.update(
            {
                "test_kind": "spark-real-bot-load",
                "profile_seconds": self.profile_seconds,
                "baseline_profile_viewer_url": None,
                "bot_profile_viewer_url": None,
                "bot_online_event": None,
                "bot_disconnect_propagation_seconds": None,
            }
        )
        self._write_results()

    def _write_results(self) -> None:
        write_json(self.result_path, self.result)
        write_json(self.spark_bot_result, self.result)

    def check(self, name: str, status: str, detail: str | None = None, **extra: Any) -> None:
        super().check(name, status, detail, **extra)
        self._write_results()

    def profile_execution(self, label: str) -> str:
        assert self.server is not None
        start = self.server.command(f"spark profiler start --timeout {self.profile_seconds}")
        deadline = time.monotonic() + self.profile_seconds + 75
        url: str | None = None
        while time.monotonic() < deadline:
            url = self._viewer_url(self.server.snapshot(), start)
            if url:
                break
            if not self.server.is_alive():
                raise RuntimeError(f"BDS exited while collecting {label} Spark profile")
            time.sleep(1)
        if url is None:
            stop_at = self.server.command("spark profiler stop")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                url = self._viewer_url(self.server.snapshot(), min(start, stop_at))
                if url:
                    break
                if not self.server.is_alive():
                    raise RuntimeError(f"BDS exited while finalizing {label} Spark profile")
                time.sleep(1)
        if url is None:
            raise RuntimeError(f"{label} Spark profiler produced no viewer URL")
        self.check(
            f"spark-profile-{label}",
            "PASS",
            f"{self.profile_seconds}s execution profile",
            viewer_url=url,
        )
        return url

    def bootstrap_offline_server(self) -> None:
        # Endstone creates server.properties on first launch. Bootstrap once,
        # stop cleanly, then apply the offline test profile needed by TestBot.
        self.start_server()
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop after initial server.properties bootstrap")
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
        self._write_results()
        output = require_player(self.server, True)
        self.check("bot-real-player-visible", "PASS", output=" | ".join(output[-20:]), online_event=online)

    def stop_bot(self) -> None:
        if self.bot is None:
            return
        assert self.server is not None
        code = self.bot.terminate(15)
        if code != 0:
            raise RuntimeError(f"Bot exited with code {code} after SIGTERM")
        events = self.bot.event_snapshot()
        if not any(event.get("event") == "disconnected" for event in events):
            raise RuntimeError("Bot exited after SIGTERM without disconnected event")
        output, propagation_seconds, probes = wait_player_state(self.server, False, timeout=20)
        self.result["bot_disconnect_propagation_seconds"] = round(propagation_seconds, 3)
        self._write_results()
        self.check(
            "bot-clean-disconnect",
            "PASS",
            output=" | ".join(output[-20:]),
            propagation_seconds=round(propagation_seconds, 3),
            probes=probes,
        )

    def execute(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            self._write_results()

            stage = "bds-bootstrap"
            self.bootstrap_offline_server()
            assert self.server is not None

            stage = "spark-sanity"
            self.run_basic_commands()

            # Let post-start work settle before taking the no-player control.
            stage = "baseline-settle"
            time.sleep(10)
            self.command_check("baseline-player-count", "list")

            stage = "baseline-profile"
            baseline_url = self.profile_execution("baseline-no-player")
            self.result["baseline_profile_viewer_url"] = baseline_url
            self._write_results()

            stage = "bot-connect"
            self.start_bot()

            # Initial chunk streaming is intentionally excluded from the steady
            # profile. The measured load is a stable real player: loaded chunks,
            # normal BDS ticking and 20 TPS PlayerAuthInput from the bot.
            stage = "bot-settle"
            time.sleep(15)
            self.command_check("bot-spark-tps", "spark tps")
            self.command_check("bot-spark-health", "spark health")
            self.command_check("bot-spark-activity", "spark activity")

            stage = "bot-profile"
            bot_url = self.profile_execution("real-bot-steady")
            self.result["bot_profile_viewer_url"] = bot_url
            self._write_results()

            stage = "bot-disconnect"
            self.stop_bot()

            stage = "shutdown"
            self.shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self._write_results()
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
            self._write_results()
            return 1
        finally:
            if self.bot is not None and self.bot.is_alive():
                self.bot.force_close()
            self.result["completed_at"] = now_iso()
            self.split_logs()
            self._write_results()
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()
    return SparkBotValidation(pathlib.Path(args.bot), args.profile_seconds).execute()


if __name__ == "__main__":
    raise SystemExit(main())
