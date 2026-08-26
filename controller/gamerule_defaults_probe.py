#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import traceback

from controller.release_validation import SparkReleaseValidation
from controller.run_test import now_iso, write_json

RULES = [
    "commandblockoutput", "commandblocksenabled", "dodaylightcycle", "doentitydrops", "dofiretick",
    "doimmediaterespawn", "doinsomnia", "dolimitedcrafting", "domobloot", "domobspawning",
    "dotiledrops", "doweathercycle", "drowningdamage", "falldamage", "firedamage", "freezedamage",
    "functioncommandlimit", "keepinventory", "locatorbar", "maxcommandchainlength", "mobgriefing",
    "naturalregeneration", "playerssleepingpercentage", "playerwaypoints", "projectilescanbreakblocks",
    "pvp", "randomtickspeed", "recipesunlock", "respawnblocksexplode", "sendcommandfeedback",
    "showbordereffect", "showcoordinates", "showdaysplayed", "showdeathmessages", "showrecipemessages",
    "showtags", "spawnradius", "tntexplodes", "tntexplosiondropdecay",
]


class GameruleDefaultsProbe(SparkReleaseValidation):
    def __init__(self) -> None:
        super().__init__("linux", 30, pathlib.Path("unused-bot"))
        self.result.update({"test_kind": "bedrock-gamerule-default-probe", "gamerules": {}})
        write_json(self.result_path, self.result)

    def execute_probe(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            self.verify_endstone_version()

            stage = "bds-bootstrap"
            self.bootstrap_offline_server()
            assert self.server is not None

            stage = "gamerule-query"
            values: dict[str, list[str]] = {}
            for rule in RULES:
                start = self.server.command(f"gamerule {rule}")
                lines = self.server.wait_command_output(start, 5)
                values[rule] = lines[-10:]
                print(f"GAMERULE_PROBE {rule}: " + " | ".join(lines[-10:]), flush=True)
            self.result["gamerules"] = values
            self.check("gamerule-default-query", "PASS", f"queried {len(values)} gamerules on a fresh world")

            stage = "shutdown"
            self.shutdown()
            if self.result.get("shutdown_status") != "graceful":
                raise RuntimeError(f"probe shutdown was not graceful: {self.result.get('shutdown_status')}")

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
            write_json(self.result_path, self.result)
            print(json.dumps(self.result, ensure_ascii=False), flush=True)


def main() -> int:
    return GameruleDefaultsProbe().execute_probe()


if __name__ == "__main__":
    raise SystemExit(main())
