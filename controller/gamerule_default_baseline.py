#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import traceback

from controller.run_test import IntegrationTest, now_iso, write_json

CURRENT_VANILLA_RULES = (
    "commandblockoutput",
    "commandblocksenabled",
    "dodaylightcycle",
    "doentitydrops",
    "dofiretick",
    "doimmediaterespawn",
    "doinsomnia",
    "dolimitedcrafting",
    "domobloot",
    "domobspawning",
    "dotiledrops",
    "doweathercycle",
    "drowningdamage",
    "falldamage",
    "firedamage",
    "freezedamage",
    "functioncommandlimit",
    "keepinventory",
    "maxcommandchainlength",
    "mobgriefing",
    "naturalregeneration",
    "playerssleepingpercentage",
    "playerwaypoints",
    "projectilescanbreakblocks",
    "pvp",
    "randomtickspeed",
    "recipesunlock",
    "respawnblocksexplode",
    "sendcommandfeedback",
    "showbordereffect",
    "showcoordinates",
    "showdaysplayed",
    "showdeathmessages",
    "showrecipemessages",
    "showtags",
    "spawnradius",
    "tntexplodes",
    "tntexplosiondropdecay",
)

VALUE_RE = re.compile(r"(?:=|:)\s*(true|false|-?\d+(?:\.\d+)?)\s*$", re.IGNORECASE)


def parse_value(lines: list[str]) -> str:
    for line in reversed(lines):
        match = VALUE_RE.search(line.strip())
        if match:
            return match.group(1).lower()
    raise RuntimeError("Could not parse gamerule value from: " + " | ".join(lines[-10:]))


class GameRuleDefaultBaseline(IntegrationTest):
    def __init__(self) -> None:
        super().__init__("linux")
        self.result["test_kind"] = "gamerule-default-baseline"
        self.result["gamerules"] = {}
        write_json(self.result_path, self.result)

    def execute_baseline(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            stage = "bds-start"
            self.start_server()

            stage = "gamerule-query"
            values: dict[str, str] = {}
            for rule in CURRENT_VANILLA_RULES:
                output = self.command_check(f"gamerule-{rule}", f"gamerule {rule}")
                value = parse_value(output)
                values[rule] = value
                print(f"GAMERULE_DEFAULT {rule}={value}", flush=True)
            self.result["gamerules"] = values
            self.check("gamerule-default-count", "PASS", f"captured {len(values)} current vanilla gamerules")

            stage = "unknown-rule"
            assert self.server is not None
            start = self.server.command("gamerule spark_unknown_future_rule")
            self.result["unknown_rule_output"] = self.server.wait_command_output(start, 8.0)

            stage = "shutdown"
            self.shutdown()
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
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:
                diagnostic += "\n\nCleanup failure:\n" + traceback.format_exc()
            self.diagnostics.write_text(diagnostic, encoding="utf-8")
            return 1
        finally:
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    return GameRuleDefaultBaseline().execute_baseline()


if __name__ == "__main__":
    raise SystemExit(main())
