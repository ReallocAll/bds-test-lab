#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import site
import traceback

import providers.artifact_provider as artifact_provider
from controller.release_validation import SparkReleaseValidation
from controller.run_test import now_iso, write_json

ENDSTONE_SHA = "27cc2e04d843bd70f089b0814ddba3054d4c55ef"
RULES = [
    "commandblockoutput", "commandblocksenabled", "dodaylightcycle", "doentitydrops", "dofiretick",
    "doimmediaterespawn", "doinsomnia", "dolimitedcrafting", "domobloot", "domobspawning", "dotiledrops",
    "doweathercycle", "drowningdamage", "falldamage", "firedamage", "freezedamage", "keepinventory",
    "locatorbar", "mobgriefing", "naturalregeneration", "projectilescanbreakblocks", "pvp", "recipesunlock",
    "respawnblocksexplode", "sendcommandfeedback", "showbordereffect", "showcoordinates", "showdaysplayed",
    "showdeathmessages", "showrecipemessages", "showtags", "tntexplodes", "tntexplosiondropdecay",
    "functioncommandlimit", "maxcommandchainlength", "playerssleepingpercentage", "playerwaypoints",
    "randomtickspeed", "spawnradius",
]


def install_api_probe_plugin() -> None:
    site_packages = pathlib.Path(site.getsitepackages()[0])
    module = site_packages / "gamerule_probe_plugin.py"
    module.write_text(
        '''from endstone.plugin import Plugin\n\n\nclass GameruleProbePlugin(Plugin):\n    api_version = "0.11"\n\n    def on_enable(self) -> None:\n        level = self.server.level\n        for rule in ("minecraft:playerwaypoints", "minecraft:locatorbar"):\n            present = level.has_game_rule(rule)\n            value = level.get_game_rule(rule) if present else None\n            self.logger.info(\n                f"GAMERULE_API {rule} has={present} value={value!r} type={type(value).__name__}"\n            )\n''',
        encoding="utf-8",
    )
    dist_info = site_packages / "endstone_gamerule_probe-0.0.0.dist-info"
    dist_info.mkdir(exist_ok=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: endstone-gamerule-probe\nVersion: 0.0.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[endstone]\ngamerule-probe = gamerule_probe_plugin:GameruleProbePlugin\n",
        encoding="utf-8",
    )


class GameruleBaselineProbe(SparkReleaseValidation):
    def __init__(self) -> None:
        super().__init__("linux", 30, pathlib.Path("/bin/true"))
        self.result.update({"test_kind": "temporary-gamerule-baseline-probe", "gamerules": {}, "api_probe": []})
        write_json(self.result_path, self.result)

    def install_artifacts(self) -> None:
        original_discover = artifact_provider.discover

        def pinned_discover(component: str, platform_name: str, expected_sha: str | None = None):
            if component == "endstone":
                return original_discover(component, platform_name, expected_sha=ENDSTONE_SHA)
            return original_discover(component, platform_name, expected_sha=expected_sha)

        artifact_provider.discover = pinned_discover
        try:
            super().install_artifacts()
        finally:
            artifact_provider.discover = original_discover

    def execute_probe(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            self.verify_endstone_version()
            install_api_probe_plugin()
            stage = "bds-bootstrap"
            self.bootstrap_offline_server()
            assert self.server is not None

            api_lines = [line for line in self.server.snapshot() if "GAMERULE_API" in line]
            self.result["api_probe"] = api_lines
            for line in api_lines:
                print(line, flush=True)

            stage = "query-gamerules"
            for rule in RULES:
                start = self.server.command(f"gamerule {rule}")
                lines = self.server.wait_command_output(start, 5)
                self.result["gamerules"][rule] = lines[-10:]
                print(f"GAMERULE {rule}: " + " | ".join(lines[-10:]), flush=True)

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
            pathlib.Path("failure-diagnostics.txt").write_text(traceback.format_exc(), encoding="utf-8")
            if self.server is not None and self.server.is_alive():
                self.server.force_kill_tree()
            return 1
        finally:
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)
            pathlib.Path("gamerule-baseline.json").write_text(
                json.dumps(
                    {"gamerules": self.result.get("gamerules", {}), "api_probe": self.result.get("api_probe", [])},
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )


def main() -> int:
    return GameruleBaselineProbe().execute_probe()


if __name__ == "__main__":
    raise SystemExit(main())
