#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from typing import Any

from controller.run_test import IntegrationTest, now_iso, write_json

RULES = [
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
]

QUERY_RE = re.compile(r"Gamerule\s+(\S+)\s+is currently set to:\s*(\S+)", re.IGNORECASE)


class GameruleDefaultsProbe(IntegrationTest):
    def __init__(self) -> None:
        super().__init__("linux")
        self.result.update({"test_kind": "gamerule-defaults-probe", "gamerules": {}})
        write_json(self.result_path, self.result)

    def query_rule(self, rule: str) -> dict[str, Any]:
        assert self.server is not None
        start = self.server.command(f"gamerule {rule}")
        lines = self.server.wait_command_output(start, timeout=8.0)
        value = None
        for line in lines:
            match = QUERY_RE.search(line)
            if match and match.group(1).lower() == rule.lower():
                value = match.group(2)
                break
        return {"value": value, "output": lines}

    def run(self) -> int:
        try:
            self.install_artifacts()
            self.start_server()
            assert self.server is not None
            values: dict[str, Any] = {}
            for rule in RULES:
                entry = self.query_rule(rule)
                values[rule] = entry
                print(f"GAMERULE_DEFAULT {rule}={entry['value']}", flush=True)
            self.result["gamerules"] = values
            missing = [name for name, entry in values.items() if entry["value"] is None]
            self.check("gamerule-current-defaults", "PASS" if not missing else "FAIL", json.dumps({"missing": missing}))
            if missing:
                raise RuntimeError("failed to query gamerules: " + ", ".join(missing))
            if not self.server.graceful_stop(60):
                self.server.force_kill_tree()
                raise RuntimeError("BDS did not stop gracefully")
            self.result["shutdown_status"] = "graceful"
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self.result["completed_at"] = now_iso()
            write_json(self.result_path, self.result)
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["error_summary"] = str(exc)
            self.result["completed_at"] = now_iso()
            write_json(self.result_path, self.result)
            if self.server is not None:
                self.server.force_kill_tree()
            raise
        finally:
            if self.server is not None:
                self.server.close()


def main() -> int:
    return GameruleDefaultsProbe().run()


if __name__ == "__main__":
    raise SystemExit(main())
