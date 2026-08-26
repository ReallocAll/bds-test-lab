#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import traceback

from controller.bot_validation import patch_server_properties
from controller.fleet_spark_validation import set_server_property
from controller.release_validation import SparkReleaseValidation
from controller.run_test import now_iso, write_json

# Vanilla rules currently exposed by the Endstone/BDS Gamerule API. Keep this
# probe separate from Spark's fallback table: its purpose is to measure a fresh
# current Bedrock world so hard-coded fallback values can be checked against the
# runtime rather than guessed.
RULES = (
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
    "locatorbar",
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

VALUE_RE = re.compile(r"(?:=|is(?: currently)? set to)\s*([A-Za-z0-9_.+-]+)\s*$", re.IGNORECASE)


class GameruleDefaultProbe(SparkReleaseValidation):
    def __init__(self) -> None:
        super().__init__("linux", 30, pathlib.Path("unused-bot"))
        self.world_name = "GameruleDefaultProbe"
        self.result.update({"test_kind": "bedrock-gamerule-default-probe", "gamerules": {}})
        write_json(self.result_path, self.result)

    def bootstrap_fresh_world(self) -> None:
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
        set_server_property(properties, "allow-cheats", "true")
        world = self.server_dir / "worlds" / self.world_name
        if world.exists():
            shutil.rmtree(world)
        self.start_server()

    @staticmethod
    def parse_value(rule: str, lines: list[str]) -> str | None:
        for line in reversed(lines):
            lower = line.lower()
            if rule not in lower:
                continue
            match = VALUE_RE.search(line.strip())
            if match:
                return match.group(1).lower()
        # BDS often emits just "<rule> = <value>" but retain raw output instead
        # of guessing if a future version changes the text format.
        return None

    def execute_probe(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            self.verify_endstone_version()
            stage = "fresh-world"
            self.bootstrap_fresh_world()
            assert self.server is not None

            stage = "gamerule-query"
            measured: dict[str, dict[str, object]] = {}
            for rule in RULES:
                lines = self.command_check(f"gamerule-{rule}", f"gamerule {rule}")
                value = self.parse_value(rule, lines)
                measured[rule] = {"value": value, "raw": lines[-8:]}
            self.result["gamerules"] = measured
            self.result["bds_version"] = self.read_bds_version()
            unresolved = [rule for rule, row in measured.items() if row["value"] is None]
            self.result["unresolved"] = unresolved
            write_json(self.result_path, self.result)
            if unresolved:
                raise RuntimeError("could not parse gamerule values: " + ", ".join(unresolved))

            stage = "shutdown"
            self.shutdown()
            if self.result.get("shutdown_status") != "graceful":
                raise RuntimeError(f"shutdown was not graceful: {self.result.get('shutdown_status')}")
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1200]
            self.diagnostics.write_text(traceback.format_exc(), encoding="utf-8")
            try:
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:
                pass
            return 1
        finally:
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    return GameruleDefaultProbe().execute_probe()


if __name__ == "__main__":
    raise SystemExit(main())
