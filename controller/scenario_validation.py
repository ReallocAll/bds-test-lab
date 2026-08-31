#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import subprocess
import threading
from typing import Any

from controller.bot_validation import BotProcess
from controller.fleet_spark_validation import FleetSparkValidation
from controller.run_test import child_process_env


class ScenarioFileBotProcess(BotProcess):
    def __init__(
        self,
        binary: pathlib.Path,
        log_path: pathlib.Path,
        count: int,
        scenario_file: pathlib.Path,
        name_prefix: str = "TestBot",
    ):
        super().__init__(binary, log_path)
        self.count = count
        self.scenario_file = scenario_file.resolve()
        self.name_prefix = name_prefix

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
            "--scenario-file",
            str(self.scenario_file),
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
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_process_env(),
        )
        self._reader = threading.Thread(target=self._read_loop, name="scenario-bot-log-reader", daemon=True)
        self._reader.start()


class ScenarioSparkValidation(FleetSparkValidation):
    def __init__(
        self,
        bot_binary: pathlib.Path,
        count: int,
        scenario_file: pathlib.Path,
        profile_seconds: int,
        min_action_packets_per_bot: int = 0,
    ):
        self.scenario_file = scenario_file.resolve()
        self.min_action_packets_per_bot = min_action_packets_per_bot
        super().__init__(bot_binary, count, self.scenario_file.stem, profile_seconds)
        self.result["scenario_file"] = str(self.scenario_file)
        self.result["min_action_packets_per_bot"] = self.min_action_packets_per_bot
        self._write_results()

    def start_fleet(self) -> None:
        assert self.server is not None
        self.bot = ScenarioFileBotProcess(
            self.bot_binary,
            self.bot_log,
            self.count,
            self.scenario_file,
        )
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
            "scenario-fleet-all-online",
            "PASS",
            f"{self.count} configured-scenario players visible in BDS",
            convergence_seconds=round(convergence, 3),
            fleet_online_event=online,
        )

    def stop_fleet(self) -> None:
        super().stop_fleet()

        stats: list[dict[str, Any]] = self.result.get("bot_stats") or []
        movement_total = 0
        auth_total = 0
        action_total = 0
        bad: list[dict[str, Any]] = []
        min_ratio = 1.0
        expected_scenario = self.scenario_file.stem
        for event in stats:
            movement = int(event.get("movement_inputs_sent", 0))
            auth = int(event.get("auth_inputs_sent", 0))
            action_packets = int(event.get("action_packets_sent", 0))
            ratio = movement / auth if auth else 0.0
            movement_total += movement
            auth_total += auth
            action_total += action_packets
            min_ratio = min(min_ratio, ratio)
            if (
                event.get("scenario") != expected_scenario
                or movement < self.profile_seconds * 10
                or auth <= 0
                or ratio < 0.70
                or action_packets < self.min_action_packets_per_bot
            ):
                bad.append(event)

        if bad:
            raise RuntimeError(f"Configured scenario action evidence failed for {len(bad)} bots: {bad[:3]}")

        self.check(
            "scenario-action-sequence",
            "PASS",
            f"all {self.count} bots progressed through configured actions into sustained movement",
            movement_inputs_sent=movement_total,
            auth_inputs_sent=auth_total,
            action_packets_sent=action_total,
            min_action_packets_per_bot=self.min_action_packets_per_bot,
            min_movement_ratio=round(min_ratio, 4),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--count", required=True, type=int, choices=[1, 5, 10, 20])
    parser.add_argument("--scenario-file", required=True)
    parser.add_argument("--profile-seconds", type=int, default=30)
    parser.add_argument("--min-action-packets-per-bot", type=int, default=0)
    args = parser.parse_args()
    return ScenarioSparkValidation(
        pathlib.Path(args.bot),
        args.count,
        pathlib.Path(args.scenario_file),
        args.profile_seconds,
        args.min_action_packets_per_bot,
    ).execute()


if __name__ == "__main__":
    raise SystemExit(main())
