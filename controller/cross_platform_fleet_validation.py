#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import threading

from controller.fleet_spark_validation import FleetBotProcess, FleetSparkValidation
from controller.run_test import IntegrationTest, write_json


class CrossPlatformFleetBotProcess(FleetBotProcess):
    def start(self) -> None:
        cmd = [
            str(self.binary),
            "--host",
            "127.0.0.1",
            "--port",
            "19132",
            "--count",
            str(self.count),
            "--name-prefix",
            self.name_prefix,
            "--scenario",
            self.scenario,
            "--login-stagger",
            "250ms",
            "--chunk-radius",
            "8",
            "--connect-timeout",
            "20s",
            "--spawn-timeout",
            "45s",
            "--json",
        ]
        print("+", " ".join(cmd), flush=True)
        self._log = self.log_path.open("w", encoding="utf-8")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._read_loop, name="fleet-bot-log-reader", daemon=True)
        self._reader.start()


class CrossPlatformFleetSparkValidation(FleetSparkValidation):
    def __init__(
        self,
        platform_name: str,
        bot_binary: pathlib.Path,
        count: int,
        scenario: str,
        profile_seconds: int,
    ):
        IntegrationTest.__init__(self, platform_name)
        self.bot_binary = bot_binary.resolve()
        self.count = count
        self.scenario = scenario
        self.profile_seconds = max(30, profile_seconds)
        self.bot_log = self.root / f"fleet-{platform_name}-{count}-{scenario}.log"
        self.fleet_result = self.root / "fleet-spark-result.json"
        self.bot: FleetBotProcess | None = None
        self.result.update(
            {
                "test_kind": "spark-cross-platform-real-player-load",
                "platform": platform_name,
                "bot_count": count,
                "scenario": scenario,
                "profile_seconds": self.profile_seconds,
                "spark_profile_viewer_url": None,
                "metrics": None,
                "fleet_online_event": None,
                "fleet_shutdown_event": None,
                "bot_stats": [],
            }
        )
        self._write_results()

    def _write_results(self) -> None:
        write_json(self.result_path, self.result)
        write_json(self.fleet_result, self.result)

    def start_fleet(self) -> None:
        assert self.server is not None
        self.bot = CrossPlatformFleetBotProcess(self.bot_binary, self.bot_log, self.count, self.scenario)
        self.bot.start()
        online = self.bot.wait_event("fleet_online", max(90.0, self.count * 5.0))
        if int(online.get("online", -1)) != self.count or int(online.get("count", -1)) != self.count:
            raise RuntimeError(f"Invalid fleet_online event: {online}")
        self.result["fleet_online_event"] = online
        output, convergence = self.wait_player_count(self.count)
        joined = "\n".join(output).lower()
        missing = [name for name in self.expected_names() if name.lower() not in joined]
        if missing:
            raise RuntimeError(f"BDS list reached {self.count} players but names are missing: {missing}")
        self.check(
            "fleet-all-online",
            "PASS",
            f"{self.count} independent players visible in BDS",
            convergence_seconds=round(convergence, 3),
            fleet_online_event=online,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--count", required=True, type=int, choices=[1, 5])
    parser.add_argument("--scenario", required=True, choices=["idle", "chunk-walk"])
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()
    validator = CrossPlatformFleetSparkValidation(
        args.platform,
        pathlib.Path(args.bot),
        args.count,
        args.scenario,
        args.profile_seconds,
    )
    code = validator.execute()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
