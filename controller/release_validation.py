#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.metadata
import pathlib
import re
import time
import traceback
from typing import Any

from packaging.version import Version

from controller.bot_validation import BotProcess, patch_server_properties, wait_player_state
from controller.extended_validation import ExtendedIntegrationTest
from controller.run_test import VIEWER_RE, now_iso, write_json

MIN_ENDSTONE_VERSION = Version("0.11.10.dev371")
PLAYER_COUNT_RE = re.compile(r"Players online:\s*(\d+)", re.IGNORECASE)


class SparkReleaseValidation(ExtendedIntegrationTest):
    def __init__(self, platform_name: str, soak_minutes: int, bot_binary: pathlib.Path):
        super().__init__(platform_name, soak_minutes)
        self.bot_binary = bot_binary.resolve()
        self.bot_log = self.root / f"release-bot-{platform_name}.log"
        self.bot: BotProcess | None = None
        self.result.update(
            {
                "test_kind": "spark-v0.6-release-validation",
                "minimum_endstone_version": str(MIN_ENDSTONE_VERSION),
                "installed_endstone_version": None,
                "bot_online_event": None,
                "bot_disconnect_propagation_seconds": None,
                "health_upload_viewer_url": None,
                "health_dashboard_viewer_url": None,
                "post_soak_health_upload_viewer_url": None,
                "player_health_output": None,
            }
        )
        write_json(self.result_path, self.result)

    def verify_endstone_version(self) -> None:
        installed = importlib.metadata.version("endstone")
        parsed = Version(installed)
        self.result["installed_endstone_version"] = installed
        write_json(self.result_path, self.result)
        if parsed < MIN_ENDSTONE_VERSION:
            raise RuntimeError(
                f"Endstone {installed} is older than required {MIN_ENDSTONE_VERSION}; refusing ABI-mismatched validation"
            )
        self.check("endstone-abi-version", "PASS", f"Endstone {installed} >= {MIN_ENDSTONE_VERSION}")

    def start_server(self) -> None:
        super().start_server()
        assert self.server is not None
        self.server.wait_for(
            lambda lines: any("endstone-spark" in line.lower() and "enabled" in line.lower() for line in lines),
            30,
            "Spark fully enabled",
        )

    def bootstrap_offline_server(self) -> None:
        self.start_server()
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop after server.properties bootstrap")
        self.server.close()
        self.server = None
        patch_server_properties(self.server_dir / "server.properties")
        self.check("release-server-properties", "PASS", "offline mode and idle timeout disabled for real Bot")
        self.start_server()

    def start_real_player(self) -> None:
        assert self.server is not None
        self.bot = BotProcess(self.bot_binary, self.bot_log)
        self.bot.start()
        online = self.bot.wait_event("online", 60)
        output, convergence_seconds, probes = wait_player_state(self.server, True, timeout=30)
        self.result["bot_online_event"] = online
        self.check(
            "real-player-online",
            "PASS",
            "TestBot is online and visible to BDS",
            convergence_seconds=round(convergence_seconds, 3),
            probes=probes,
            output=" | ".join(output[-20:]),
            bot_event=online,
        )
        time.sleep(3)

    def stop_real_player(self) -> None:
        if self.bot is None:
            return
        assert self.server is not None
        code = self.bot.terminate(20)
        if code != 0:
            raise RuntimeError(f"Bot exited with code {code} after SIGTERM")
        output, propagation_seconds, probes = wait_player_state(self.server, False, timeout=30)
        self.result["bot_disconnect_propagation_seconds"] = round(propagation_seconds, 3)
        self.check(
            "real-player-clean-disconnect",
            "PASS",
            "TestBot disconnected and BDS player list converged to zero",
            propagation_seconds=round(propagation_seconds, 3),
            probes=probes,
            output=" | ".join(output[-20:]),
        )

    def validate_player_health(self) -> None:
        assert self.server is not None
        start = self.server.command("spark health show")
        output = self.server.wait_command_output(start, 10)
        text = "\n".join(output)
        match = PLAYER_COUNT_RE.search(text)
        if match is None or int(match.group(1)) < 1:
            raise RuntimeError("spark health show did not report the real online player: " + " | ".join(output[-30:]))
        self.result["player_health_output"] = output[-40:]
        self.check("player-inclusive-health", "PASS", "spark health show reported at least one online player")

    def _wait_health_url(self, start: int, success_marker: str, failure_marker: str, timeout: float) -> str:
        assert self.server is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.server.snapshot()[start:]
            for line in lines:
                lowered = line.lower()
                if failure_marker in lowered:
                    raise RuntimeError(f"Health operation failed: {line}")
                if success_marker in lowered:
                    match = VIEWER_RE.search(line)
                    if match:
                        return match.group(0).rstrip(").,]")
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while waiting for health viewer URL")
            time.sleep(0.5)
        raise RuntimeError(f"Timed out waiting for health success marker: {success_marker}")

    def validate_health_upload(self) -> None:
        assert self.server is not None
        start = self.server.command("spark health upload")
        url = self._wait_health_url(start, "health report uploaded!", "health report upload failed", 75)
        self.result["health_upload_viewer_url"] = url
        self.check("health-upload-with-player", "PASS", viewer_url=url)

    def validate_post_soak_health_upload(self) -> None:
        assert self.server is not None
        start = self.server.command("spark health upload")
        url = self._wait_health_url(start, "health report uploaded!", "health report upload failed", 75)
        self.result["post_soak_health_upload_viewer_url"] = url
        self.check(
            "post-soak-allocation-rate-health-upload",
            "PASS",
            f"health report captured after {self.soak_minutes}-minute soak",
            viewer_url=url,
        )

    def validate_health_dashboard(self) -> None:
        assert self.server is not None
        start = self.server.command("spark health")
        url = self._wait_health_url(start, "health dashboard opened!", "health dashboard connection failed", 75)
        self.result["health_dashboard_viewer_url"] = url
        self.check("health-dashboard-with-player", "PASS", viewer_url=url)

    def graceful_dashboard_restart(self) -> None:
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop gracefully with health dashboard open")
        self.server.close()
        self.server = None
        self.result["shutdown_status"] = "graceful_dashboard_restart"
        self.check("health-dashboard-bounded-shutdown", "PASS", "graceful stop completed with dashboard open")
        self.start_server()

    def execute_release(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            self.verify_endstone_version()

            stage = "bds-bootstrap"
            self.bootstrap_offline_server()
            assert self.server is not None

            stage = "real-player-online"
            self.start_real_player()

            stage = "player-health"
            self.validate_player_health()
            self.command_check("spark-tps-with-player", "spark tps")

            stage = "health-upload"
            self.validate_health_upload()

            stage = "health-dashboard"
            self.validate_health_dashboard()

            stage = "execution-profiler"
            execution_url = self.run_profiler(allocation=False)
            self.record_online_profile("execution", execution_url, 12, "Execution profiler with real player online")

            stage = "allocation-profiler"
            allocation_url = self.run_profiler(allocation=True)
            if allocation_url:
                self.record_online_profile(
                    "allocation", allocation_url, 12, "Native allocation profiler with real player online"
                )

            stage = "real-player-disconnect"
            self.stop_real_player()

            stage = "dashboard-graceful-restart"
            self.graceful_dashboard_restart()

            stage = "crash-recovery"
            self.run_crash_recovery_probe()

            stage = "soak"
            self.run_soak()

            stage = "post-soak-health-upload"
            self.validate_post_soak_health_upload()

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
                diagnostic + "\n\nLast BDS log lines:\n" + "\n".join(last_lines), encoding="utf-8"
            )
            return 1
        finally:
            if self.bot is not None and self.bot.is_alive():
                self.bot.force_close()
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--soak-minutes", type=int, default=30)
    args = parser.parse_args()
    return SparkReleaseValidation(args.platform, args.soak_minutes, pathlib.Path(args.bot)).execute_release()


if __name__ == "__main__":
    raise SystemExit(main())
