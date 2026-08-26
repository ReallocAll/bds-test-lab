#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import shutil
import sys
import time
import traceback
from typing import Any

from controller.bot_validation import list_players, patch_server_properties
from controller.fleet_spark_validation import FleetBotProcess, set_server_property
from controller.run_test import IntegrationTest, READY_HINTS, locate_one, now_iso, run_checked, write_json
from providers.artifact_provider import resolve_artifacts

PLAYER_COUNT_RE = re.compile(r"There are\s+(\d+)/(\d+)\s+players online", re.IGNORECASE)
AXIS_RE = {
    axis: re.compile(rf'"{axis}"\s*:\s*(-?[0-9]+(?:\.[0-9]+)?)', re.IGNORECASE)
    for axis in ("x", "y", "z")
}


class MovementBDSOnlyValidation(IntegrationTest):
    def __init__(self, bot_binary: pathlib.Path, scenario: str):
        super().__init__("linux")
        self.bot_binary = bot_binary.resolve()
        self.scenario = scenario
        self.bot_log = self.root / f"bds-only-{scenario}.log"
        self.bot: FleetBotProcess | None = None
        self.result.update(
            {
                "test_kind": "bds-only-authoritative-movement",
                "scenario": scenario,
                "server_position_before": None,
                "server_position_after": None,
                "server_horizontal_displacement": None,
                "fleet_shutdown_event": None,
                "bot_stats": [],
            }
        )
        self._write_results()

    def _write_results(self) -> None:
        write_json(self.result_path, self.result)

    def install_endstone_only(self) -> None:
        self.metadata = resolve_artifacts(self.platform, self.downloads, self.metadata_path)
        self.check("artifact-discovery", "PASS")
        endstone_root = self.downloads / "endstone" / "payload"
        wheel = locate_one(endstone_root, ["endstone-*-cp313-cp313-*.whl", "endstone-*.whl"])
        self.check("endstone-wheel-located", "PASS", str(wheel.relative_to(self.root)))
        run_checked(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall", str(wheel)],
            timeout=300,
        )
        plugin_dir = self.server_dir / "plugins"
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir)
        plugin_dir.mkdir(parents=True, exist_ok=True)
        self.check("spark-absent", "PASS", "BDS-only diagnostic intentionally loads no Spark plugin")

    def start_server(self) -> None:
        from controller.run_test import ServerProcess

        cmd = [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(self.server_dir)]
        self.server = ServerProcess(cmd, self.root, self.log_path)
        self.server.start()
        self.server.wait_for(
            lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines),
            240,
            "BDS ready",
        )
        self.check("bds-start", "PASS")
        self.check("ready", "PASS")
        version_file = self.server_dir / "version.txt"
        if version_file.exists():
            self.result["bds_version"] = version_file.read_text(encoding="utf-8").strip()
            self._write_results()

    def bootstrap_offline_server(self) -> None:
        self.start_server()
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop after server.properties bootstrap")
        self.server.close()
        self.server = None

        properties = self.server_dir / "server.properties"
        patch_server_properties(properties)
        set_server_property(properties, "max-players", "30")
        set_server_property(properties, "gamemode", "creative")
        set_server_property(properties, "force-gamemode", "true")
        self.check("server-properties", "PASS", "offline, creative, force-gamemode, idle timeout disabled")
        self.start_server()

    def wait_player_count(self, expected: int, timeout: float = 45.0) -> list[str]:
        assert self.server is not None
        deadline = time.monotonic() + timeout
        last: list[str] = []
        while time.monotonic() < deadline:
            last = list_players(self.server)
            for line in last:
                match = PLAYER_COUNT_RE.search(line)
                if match and int(match.group(1)) == expected:
                    return last
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while waiting for player count")
            time.sleep(0.5)
        raise RuntimeError(f"Expected {expected} players, last output: {' | '.join(last[-30:])}")

    @staticmethod
    def parse_querytarget_position(lines: list[str]) -> list[float]:
        values: dict[str, float] = {}
        in_position = False
        for line in lines:
            if '"position"' in line and "{" in line:
                in_position = True
                values.clear()
                continue
            if not in_position:
                continue
            for axis, pattern in AXIS_RE.items():
                match = pattern.search(line)
                if match:
                    values[axis] = float(match.group(1))
            if len(values) == 3:
                return [values["x"], values["y"], values["z"]]
            if "}" in line and values:
                in_position = False
                values.clear()
        raise RuntimeError("querytarget did not contain a parseable position: " + " | ".join(lines[-30:]))

    def query_position(self) -> list[float]:
        assert self.server is not None
        start = self.server.command("querytarget TestBot")
        output = self.server.wait_command_output(start, 8)
        return self.parse_querytarget_position(output)

    def start_fleet(self) -> None:
        assert self.server is not None
        self.bot = FleetBotProcess(self.bot_binary, self.bot_log, 1, self.scenario)
        self.bot.start()
        online = self.bot.wait_event("fleet_online", 90)
        if int(online.get("online", -1)) != 1:
            raise RuntimeError(f"Invalid fleet_online event: {online}")
        self.wait_player_count(1)
        self.check("fleet-online", "PASS")

        if self.scenario == "chunk-fly":
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                for event in self.bot.event_snapshot():
                    if event.get("event") == "flight_state" and event.get("flying") is True:
                        self.check("server-flight", "PASS")
                        return
                if not self.bot.is_alive():
                    raise RuntimeError("Bot exited before server flight acknowledgement")
                if not self.server.is_alive():
                    raise RuntimeError("BDS exited before server flight acknowledgement")
                time.sleep(0.25)
            raise RuntimeError("BDS did not acknowledge creative flying")

    def validate_authoritative_movement(self) -> None:
        assert self.server is not None and self.bot is not None
        time.sleep(8)
        before = self.query_position()
        time.sleep(20)
        if not self.server.is_alive():
            raise RuntimeError("BDS exited during movement window")
        after = self.query_position()
        displacement = math.hypot(after[0] - before[0], after[2] - before[2])
        self.result["server_position_before"] = before
        self.result["server_position_after"] = after
        self.result["server_horizontal_displacement"] = displacement
        self._write_results()
        if displacement <= 8.0:
            raise RuntimeError(
                f"Server-authoritative movement did not advance: before={before}, after={after}, horizontal={displacement:.3f}"
            )
        self.check(
            "server-authoritative-movement",
            "PASS",
            f"querytarget horizontal displacement={displacement:.3f}",
            before=before,
            after=after,
        )

    def stop_fleet(self) -> None:
        if self.bot is None:
            return
        code = self.bot.terminate(20)
        if code != 0:
            raise RuntimeError(f"Fleet exited with code {code} after SIGTERM")
        events = self.bot.event_snapshot()
        shutdown = next((event for event in reversed(events) if event.get("event") == "fleet_shutdown"), None)
        stats = [event for event in events if event.get("event") == "bot_stats"]
        if shutdown is None or shutdown.get("graceful_shutdown") is not True:
            raise RuntimeError(f"Missing graceful fleet shutdown: {shutdown}")
        if len(stats) != 1:
            raise RuntimeError(f"Expected one bot_stats event, got {len(stats)}")
        self.result["fleet_shutdown_event"] = shutdown
        self.result["bot_stats"] = stats
        self._write_results()
        self.wait_player_count(0, 30)
        self.check("fleet-graceful-shutdown", "PASS")

    def execute(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_endstone_only()
            stage = "bds-bootstrap"
            self.bootstrap_offline_server()
            stage = "fleet-connect"
            self.start_fleet()
            stage = "movement"
            self.validate_authoritative_movement()
            stage = "fleet-disconnect"
            self.stop_fleet()
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
            last_lines = self.server.snapshot()[-300:] if self.server is not None else []
            self.diagnostics.write_text(diagnostic + "\n\nLast BDS log lines:\n" + "\n".join(last_lines), encoding="utf-8")
            self._write_results()
            return 1
        finally:
            if self.bot is not None and self.bot.is_alive():
                self.bot.force_close()
            self.result["completed_at"] = now_iso()
            self._write_results()
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--scenario", required=True, choices=["chunk-walk", "chunk-fly"])
    args = parser.parse_args()
    return MovementBDSOnlyValidation(pathlib.Path(args.bot), args.scenario).execute()


if __name__ == "__main__":
    raise SystemExit(main())
