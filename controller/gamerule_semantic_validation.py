#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from typing import Any

from controller.block_actor_validation import BytebinCapture
from controller.gamerule_fallback_validation import decode_gamerules
from controller.run_test import IntegrationTest, now_iso, write_json

SCENARIOS = ("defaults", "off", "everyone", "modified")

EXPECTED_MODIFIED_VALUES = {
    "keepinventory": "true",
    "showcoordinates": "true",
    "domobspawning": "false",
    "randomtickspeed": "3",
    "spawnradius": "4",
}

# Names are presentation metadata and follow Minecraft Wiki's Bedrock Edition
# spelling/casing. Values are intentionally not copied from the Wiki; the
# assertions below use values measured by bds-test-lab against current BDS.
# Lookup keys are derived from the canonical names to avoid maintaining a
# second hand-written lowercase spelling.
CANONICAL_BEDROCK_GAMERULE_NAMES = (
    "commandBlockOutput",
    "commandBlocksEnabled",
    "doDaylightCycle",
    "doEntityDrops",
    "doFireTick",
    "doImmediateRespawn",
    "doInsomnia",
    "doLimitedCrafting",
    "doMobLoot",
    "doMobSpawning",
    "doTileDrops",
    "doWeatherCycle",
    "drowningDamage",
    "fallDamage",
    "fireDamage",
    "freezeDamage",
    "functionCommandLimit",
    "keepInventory",
    "maxCommandChainLength",
    "mobGriefing",
    "naturalRegeneration",
    "playersSleepingPercentage",
    "playerWaypoints",
    "projectilesCanBreakBlocks",
    "pvp",
    "randomTickSpeed",
    "recipesUnlock",
    "respawnBlocksExplode",
    "sendCommandFeedback",
    "showBorderEffect",
    "showCoordinates",
    "showDaysPlayed",
    "showDeathMessages",
    "showRecipeMessages",
    "showTags",
    "spawnRadius",
    "tntExplodes",
    "tntExplosionDropDecay",
)
EXPECTED_CANONICAL_NAMES = {name.lower(): name for name in CANONICAL_BEDROCK_GAMERULE_NAMES}
PLAYERS_SLEEPING_KEY = "playersSleepingPercentage".lower()


class GameruleSemanticValidation(IntegrationTest):
    def __init__(self, scenario: str) -> None:
        super().__init__("linux")
        if scenario not in SCENARIOS:
            raise ValueError(f"unsupported scenario: {scenario}")
        self.scenario = scenario
        self.capture = BytebinCapture(self.root / f"gamerule-semantic-{scenario}-capture")
        self.result.update(
            {
                "test_kind": "spark-gamerule-semantic-real-bds",
                "scenario": scenario,
                "spark_sha": os.environ.get("EXPECTED_SPARK_SHA", ""),
                "validated_gamerules": {},
                "report_viewer_url": None,
            }
        )
        write_json(self.result_path, self.result)

    def apply_scenario(self) -> None:
        assert self.server is not None
        commands: list[str] = []
        if self.scenario == "off":
            commands = ["gamerule playerwaypoints off"]
        elif self.scenario == "everyone":
            commands = ["gamerule playerwaypoints everyone"]
        elif self.scenario == "modified":
            commands = [
                "gamerule keepinventory true",
                "gamerule showcoordinates true",
                "gamerule domobspawning false",
                "gamerule randomtickspeed 3",
                "gamerule spawnradius 4",
            ]

        for index, command in enumerate(commands):
            output = self.command_check(f"scenario-command-{index + 1}", command)
            joined = "\n".join(output).casefold()
            if any(marker in joined for marker in ("syntax error", "invalid", "cannot set", "failed")):
                raise RuntimeError(f"BDS rejected scenario command {command!r}: " + " | ".join(output[-20:]))

    def capture_health(self) -> bytes:
        assert self.server is not None
        before = self.capture.count()
        start = self.server.command("spark health upload")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            lines = self.server.snapshot()[start:]
            if any("health report upload failed" in line.casefold() for line in lines):
                raise RuntimeError("Spark health upload failed: " + " | ".join(lines[-30:]))
            if any("health report uploaded!" in line.casefold() for line in lines) and self.capture.count() > before:
                return self.capture.latest()
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during local Gamerule health capture")
            time.sleep(0.25)
        raise RuntimeError("timed out waiting for local Gamerule health capture")

    @staticmethod
    def single_world_value(rules: dict[str, dict[str, Any]], name: str) -> str:
        rule = rules.get(name)
        if rule is None:
            raise RuntimeError(f"expected Gamerule {name!r} is absent")
        values = set(rule["world_values"].values())
        if len(values) != 1:
            raise RuntimeError(f"expected one world value for {name!r}, got {sorted(values)!r}")
        return next(iter(values))

    def validate_semantics(self) -> None:
        rules = decode_gamerules(self.capture_health())
        self.result["validated_gamerules"] = rules
        write_json(self.result_path, self.result)

        if "locatorbar" in rules:
            raise RuntimeError("deprecated locatorbar must not be exported on current BDS")
        self.check("locatorbar-current-omitted", "PASS", "locatorbar absent from current BDS report metadata")

        for lookup_name, expected_name in EXPECTED_CANONICAL_NAMES.items():
            rule = rules.get(lookup_name)
            if rule is None:
                raise RuntimeError(f"expected Gamerule {expected_name!r} is absent from current BDS report metadata")
            actual_name = rule.get("name")
            if actual_name != expected_name:
                raise RuntimeError(
                    f"Gamerule canonical name mismatch for {lookup_name!r}: expected {expected_name!r}, got {actual_name!r}"
                )
        self.check(
            "canonical-bedrock-gamerule-names",
            "PASS",
            count=len(EXPECTED_CANONICAL_NAMES),
        )

        player_waypoints = rules.get("playerwaypoints")
        if player_waypoints is None:
            raise RuntimeError("playerWaypoints is absent from current BDS report metadata")
        if not player_waypoints["default_present"] or player_waypoints["default"] != "everyone":
            raise RuntimeError(
                "playerWaypoints current default mismatch: "
                + repr(None if player_waypoints is None else player_waypoints["default"])
            )
        self.check(
            "playerwaypoints-current-default",
            "PASS",
            expected="everyone",
            actual=player_waypoints["default"],
        )

        expected_waypoints = "off" if self.scenario == "off" else "everyone"
        actual_waypoints = self.single_world_value(rules, "playerwaypoints")
        if actual_waypoints != expected_waypoints:
            raise RuntimeError(
                f"playerWaypoints semantic mismatch for {self.scenario}: expected {expected_waypoints!r}, got {actual_waypoints!r}"
            )
        self.check(
            "playerwaypoints-semantic-value",
            "PASS",
            scenario=self.scenario,
            expected=expected_waypoints,
            actual=actual_waypoints,
        )

        players_sleeping = rules.get(PLAYERS_SLEEPING_KEY)
        if players_sleeping is None or not players_sleeping["default_present"] or players_sleeping["default"] != "100":
            raise RuntimeError(
                "playersSleepingPercentage current default mismatch: "
                + repr(None if players_sleeping is None else players_sleeping["default"])
            )
        actual_sleeping = self.single_world_value(rules, PLAYERS_SLEEPING_KEY)
        if actual_sleeping != "100":
            raise RuntimeError(
                f"playersSleepingPercentage fresh-world value mismatch: expected '100', got {actual_sleeping!r}"
            )
        self.check(
            "playerssleepingpercentage-current-default",
            "PASS",
            expected="100",
            actual=players_sleeping["default"],
            world_value=actual_sleeping,
        )

        max_chain = rules.get("maxcommandchainlength")
        if max_chain is None or max_chain["default"] != "65535":
            raise RuntimeError(
                "maxCommandChainLength current fallback mismatch: "
                + repr(None if max_chain is None else max_chain["default"])
            )
        self.check("maxcommandchainlength-current-default", "PASS", expected="65535", actual=max_chain["default"])

        if self.scenario == "modified":
            for name, expected in EXPECTED_MODIFIED_VALUES.items():
                actual = self.single_world_value(rules, name)
                if actual != expected:
                    raise RuntimeError(f"modified Gamerule {name!r}: expected {expected!r}, got {actual!r}")
                self.check("modified-gamerule-value", "PASS", rule=name, expected=expected, actual=actual)

    def stop_server_for_phase_change(self) -> None:
        self.shutdown()
        self.server = None

    def generate_online_profiler_report(self) -> str:
        self.start_server()
        self.apply_scenario()
        self.command_check("online-report-health-sanity", "spark health show")
        url = self.run_profiler(allocation=False)
        if not url or not url.startswith("https://spark.lucko.me/"):
            raise RuntimeError(f"unexpected Spark profiler viewer URL: {url!r}")
        self.result["report_viewer_url"] = url
        write_json(self.result_path, self.result)
        self.check("online-profiler-report", "PASS", scenario=self.scenario, viewer_url=url)
        return url

    def execute_validation(self) -> int:
        stage = "initialization"
        previous_bytebin = os.environ.get("SPARK_BYTEBINURL")
        try:
            stage = "artifact-discovery"
            self.install_artifacts()

            stage = "local-semantic-capture"
            self.capture.start()
            os.environ["SPARK_BYTEBINURL"] = self.capture.base_url
            self.start_server()
            self.apply_scenario()
            self.validate_semantics()
            self.stop_server_for_phase_change()
            self.capture.stop()
            if previous_bytebin is None:
                os.environ.pop("SPARK_BYTEBINURL", None)
            else:
                os.environ["SPARK_BYTEBINURL"] = previous_bytebin

            stage = "online-profiler-report"
            self.generate_online_profiler_report()

            stage = "shutdown"
            self.shutdown()
            self.server = None
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self.result["completed_at"] = now_iso()
            write_json(self.result_path, self.result)
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "failed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"
            self.result["completed_at"] = now_iso()
            write_json(self.result_path, self.result)
            diagnostic = traceback.format_exc()
            traceback.print_exc()
            try:
                if self.server is not None:
                    if self.server.is_alive():
                        self.server.force_kill_tree()
                        self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:
                diagnostic += "\n\nCleanup failure:\n" + traceback.format_exc()
            last_lines = self.server.snapshot()[-300:] if self.server is not None else []
            self.diagnostics.write_text(
                diagnostic + "\n\nLast BDS log lines:\n" + "\n".join(last_lines), encoding="utf-8"
            )
            write_json(self.result_path, self.result)
            return 1
        finally:
            self.capture.stop()
            if previous_bytebin is None:
                os.environ.pop("SPARK_BYTEBINURL", None)
            else:
                os.environ["SPARK_BYTEBINURL"] = previous_bytebin


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    args = parser.parse_args()
    return GameruleSemanticValidation(args.scenario).execute_validation()


if __name__ == "__main__":
    raise SystemExit(main())
