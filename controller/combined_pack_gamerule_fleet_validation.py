#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import shutil
import time
import traceback
from typing import Any

from controller.block_actor_validation import (
    BytebinCapture,
    ProtoDecodeError,
    _fields,
    _last_message,
    _messages,
    decode_world_info_samples,
)
from controller.bot_validation import patch_server_properties
from controller.cross_platform_fleet_validation import CrossPlatformFleetSparkValidation
from controller.fleet_spark_validation import set_server_property
from controller.gamerule_fallback_validation import (
    _optional_text_field,
    decode_gamerules,
)
from controller.run_test import now_iso

WORLD_NAME = "SparkCombinedValidation"
BOT_COUNT = 20
BOT_SCENARIO = "chunk-walk"

BEHAVIOR_PACKS = (
    {
        "directory": "spark_probe_alpha",
        "name": "Spark Probe Alpha",
        "description": "Real BDS behavior-pack probe alpha",
        "pack_id": "11111111-aaaa-4aaa-8aaa-111111111111",
        "module_id": "11111111-bbbb-4bbb-8bbb-111111111111",
        "function": "spark_probe_alpha",
        "marker": "SparkPackAlphaActive",
    },
    {
        "directory": "spark_probe_beta",
        "name": "Spark Probe Beta",
        "description": "Real BDS behavior-pack probe beta",
        "pack_id": "22222222-aaaa-4aaa-8aaa-222222222222",
        "module_id": "22222222-bbbb-4bbb-8bbb-222222222222",
        "function": "spark_probe_beta",
        "marker": "SparkPackBetaActive",
    },
    {
        "directory": "spark_probe_gamma",
        "name": "Spark Probe Gamma",
        "description": "Real BDS behavior-pack probe gamma",
        "pack_id": "33333333-aaaa-4aaa-8aaa-333333333333",
        "module_id": "33333333-bbbb-4bbb-8bbb-333333333333",
        "function": "spark_probe_gamma",
        "marker": "SparkPackGammaActive",
    },
)

MODIFIED_GAMERULES = {
    "keepinventory": "true",
    "showcoordinates": "true",
    "domobspawning": "false",
    "randomtickspeed": "8",
    "spawnradius": "10",
    "playerwaypoints": "off",
}


def decode_data_packs(health_data: bytes) -> list[dict[str, Any]]:
    metadata = _last_message(health_data, 1)  # HealthData.metadata
    platform_statistics = _last_message(metadata, 3)  # HealthMetadata.platform_statistics
    world_statistics = _last_message(platform_statistics, 8)  # PlatformStatistics.world
    decoded: list[dict[str, Any]] = []
    for encoded in _messages(world_statistics, 5):  # WorldStatistics.data_packs
        name = _optional_text_field(encoded, 1)
        if not name:
            raise ProtoDecodeError("data-pack entry is missing its name")
        builtin_values = [
            value
            for number, wire, value in _fields(encoded)
            if number == 4 and wire == 0 and isinstance(value, int)
        ]
        decoded.append(
            {
                "name": name,
                "description": _optional_text_field(encoded, 2) or "",
                "source": _optional_text_field(encoded, 3) or "",
                "builtin": bool(builtin_values[-1]) if builtin_values else False,
            }
        )
    return decoded


class CombinedPackGameruleFleetValidation(CrossPlatformFleetSparkValidation):
    disable_bstats = True

    def __init__(self, platform_name: str, bot_binary: pathlib.Path, profile_seconds: int) -> None:
        super().__init__(platform_name, bot_binary, BOT_COUNT, BOT_SCENARIO, profile_seconds)
        self.capture = BytebinCapture(self.root / f"combined-health-capture-{platform_name}")
        self.local_bot_log = self.root / f"fleet-{platform_name}-{BOT_COUNT}-{BOT_SCENARIO}-local.log"
        self.public_bot_log = self.root / f"fleet-{platform_name}-{BOT_COUNT}-{BOT_SCENARIO}-public.log"
        self._shutdown_phase_ordinal = 0
        self.result.update(
            {
                "test_kind": "spark-combined-real-packs-gamerules-20-player",
                "spark_sha": os.environ.get("EXPECTED_SPARK_SHA", ""),
                "behavior_packs": [],
                "modified_gamerules": dict(MODIFIED_GAMERULES),
                "local_metadata_players": None,
                "local_metadata_validated": False,
                "health_upload_viewer_url": None,
                "execution_profile_viewer_url": None,
                "allocation_profile_viewer_url": None,
            }
        )
        self._write_results()

    def _set_phase_shutdown_context(self, phase_name: str, phase_ordinal: int | None = None) -> None:
        if self.server is None:
            return
        if phase_ordinal is None:
            self._shutdown_phase_ordinal += 1
        else:
            self._shutdown_phase_ordinal = phase_ordinal
        self.server.lifecycle_phase = {
            "ordinal": self._shutdown_phase_ordinal,
            "name": phase_name,
        }

    def _record_phase_lifecycle(self) -> None:
        diagnostic = getattr(self.server, "lifecycle_diagnostic", None) if self.server is not None else None
        if isinstance(diagnostic, dict):
            after_force = diagnostic.get("process_tree_after_force")
            verify_tree = getattr(type(self.server), "_process_tree_cleanup_outcome", None)
            if isinstance(after_force, list) and callable(verify_tree):
                diagnostic["process_tree_verification"] = verify_tree(self.server, after_force)
        self.record_server_lifecycle()
        if not isinstance(diagnostic, dict):
            return
        identity = (diagnostic.get("phase_ordinal"), diagnostic.get("phase_name"))
        if identity[0] is None or identity[1] is None:
            return
        events = self.result.setdefault("shutdown_lifecycle_events", [])
        matches = [index for index, event in enumerate(events) if (event.get("phase_ordinal"), event.get("phase_name")) == identity]
        if not matches:
            return
        events[matches[0]] = copy.deepcopy(diagnostic)
        for index in reversed(matches[1:]):
            del events[index]
        self.result["shutdown_lifecycle"] = copy.deepcopy(diagnostic)
        self._write_results()

    def stop_server_for_phase_change(self, phase_name: str = "phase-change") -> None:
        if self.server is None:
            return
        self._set_phase_shutdown_context(phase_name)
        try:
            graceful = self.server.graceful_stop(60)
        except Exception:
            self._record_phase_lifecycle()
            try:
                self.server.force_kill_tree()
            finally:
                self._record_phase_lifecycle()
            raise
        if graceful:
            self._record_phase_lifecycle()
            self.server.close()
            self.server = None
            return
        self._record_phase_lifecycle()
        try:
            self.server.force_kill_tree()
        finally:
            self._record_phase_lifecycle()
        raise RuntimeError("BDS did not stop gracefully during combined-test phase change")

    def bootstrap_scenario_world(self) -> None:
        # First boot lets Endstone provision BDS and server.properties.
        self.start_server()
        self.wait_post_start_initialization()
        self.stop_server_for_phase_change("bootstrap-provisioning")

        properties = self.server_dir / "server.properties"
        patch_server_properties(properties)
        set_server_property(properties, "max-players", "30")
        set_server_property(properties, "level-name", WORLD_NAME)
        set_server_property(properties, "allow-cheats", "true")
        set_server_property(properties, "player-idle-timeout", "0")

        world_dir = self.server_dir / "worlds" / WORLD_NAME
        if world_dir.exists():
            shutil.rmtree(world_dir)

        # Create a valid fresh world before installing its behavior-pack stack.
        self.start_server()
        self.wait_post_start_initialization()
        self.stop_server_for_phase_change("bootstrap-world")
        if not world_dir.exists():
            raise RuntimeError(f"BDS did not create expected world directory: {world_dir}")
        self.install_behavior_packs(world_dir)
        self.check(
            "combined-world-bootstrap",
            "PASS",
            "fresh world created; offline mode, cheats, max-players=30, and three behavior packs configured",
        )

    @staticmethod
    def manifest(pack: dict[str, str]) -> dict[str, Any]:
        return {
            "format_version": 2,
            "header": {
                "name": pack["name"],
                "description": pack["description"],
                "uuid": pack["pack_id"],
                "version": [1, 0, 0],
                "min_engine_version": [1, 21, 0],
            },
            "modules": [
                {
                    "type": "data",
                    "uuid": pack["module_id"],
                    "version": [1, 0, 0],
                }
            ],
        }

    def install_behavior_packs(self, world_dir: pathlib.Path) -> None:
        pack_root = self.server_dir / "behavior_packs"
        pack_root.mkdir(parents=True, exist_ok=True)
        active_stack: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []

        for pack in BEHAVIOR_PACKS:
            target = pack_root / pack["directory"]
            if target.exists():
                shutil.rmtree(target)
            functions = target / "functions"
            functions.mkdir(parents=True, exist_ok=True)
            (target / "manifest.json").write_text(
                json.dumps(self.manifest(pack), indent=2) + "\n",
                encoding="utf-8",
            )
            (functions / f"{pack['function']}.mcfunction").write_text(
                f"say {pack['marker']}\n",
                encoding="utf-8",
            )
            active_stack.append({"pack_id": pack["pack_id"], "version": [1, 0, 0]})
            evidence.append(
                {
                    "name": pack["name"],
                    "description": pack["description"],
                    "pack_id": pack["pack_id"],
                    "directory": str(target.relative_to(self.server_dir)),
                    "function": pack["function"],
                }
            )

        (world_dir / "world_behavior_packs.json").write_text(
            json.dumps(active_stack, indent=2) + "\n",
            encoding="utf-8",
        )
        self.result["behavior_packs"] = evidence
        self._write_results()

    def verify_behavior_pack_functions(self) -> None:
        for pack in BEHAVIOR_PACKS:
            output = self.command_check(
                f"behavior-pack-function-{pack['function']}",
                f"execute run function {pack['function']}",
            )
            joined = "\n".join(output).casefold()
            rejected = ("unknown function", "function not found", "failed to execute", "syntax error")
            if any(marker in joined for marker in rejected):
                raise RuntimeError(
                    f"BDS rejected behavior-pack function {pack['function']!r}: " + " | ".join(output[-30:])
                )
            if pack["marker"].casefold() not in joined:
                raise RuntimeError(
                    f"behavior-pack function {pack['function']!r} executed without expected marker "
                    f"{pack['marker']!r}: " + " | ".join(output[-30:])
                )
        self.check(
            "behavior-packs-real-load",
            "PASS",
            "all three behavior-pack functions executed inside real BDS",
            count=len(BEHAVIOR_PACKS),
        )

    def apply_modified_gamerules(self) -> None:
        for name, value in MODIFIED_GAMERULES.items():
            output = self.command_check(f"gamerule-set-{name}", f"gamerule {name} {value}")
            joined = "\n".join(output).casefold()
            if any(marker in joined for marker in ("syntax error", "invalid", "cannot set", "failed")):
                raise RuntimeError(f"BDS rejected gamerule {name}={value}: " + " | ".join(output[-30:]))
        self.check("combined-gamerules-applied", "PASS", values=dict(MODIFIED_GAMERULES))

    @staticmethod
    def single_world_value(rules: dict[str, dict[str, Any]], name: str) -> str:
        rule = rules.get(name)
        if rule is None:
            raise RuntimeError(f"expected gamerule {name!r} is absent from Spark metadata")
        values = set(rule["world_values"].values())
        if len(values) != 1:
            raise RuntimeError(f"expected one world value for {name!r}, got {sorted(values)!r}")
        return next(iter(values))

    def capture_health_payload(self) -> bytes:
        assert self.server is not None
        before = self.capture.count()
        start = self.server.command("spark health upload")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            lines = self.server.snapshot()[start:]
            if any("health report upload failed" in line.casefold() for line in lines):
                raise RuntimeError("Spark local health upload failed: " + " | ".join(lines[-40:]))
            if any("health report uploaded!" in line.casefold() for line in lines) and self.capture.count() > before:
                return self.capture.latest()
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during local combined health capture")
            time.sleep(0.25)
        raise RuntimeError("timed out waiting for locally captured combined health payload")

    def validate_local_metadata_with_20_players(self) -> None:
        deadline = time.monotonic() + 75
        last_players: int | None = None
        last_packs: list[dict[str, Any]] = []
        last_rules: dict[str, dict[str, Any]] = {}

        while time.monotonic() < deadline:
            payload = self.capture_health_payload()
            samples = decode_world_info_samples(payload)
            last_players = int(samples[-1]["players"])
            last_packs = decode_data_packs(payload)
            last_rules = decode_gamerules(payload)

            packs_by_name = {pack["name"]: pack for pack in last_packs}
            packs_ok = all(
                pack["name"] in packs_by_name
                and packs_by_name[pack["name"]]["description"] == pack["description"]
                and packs_by_name[pack["name"]]["source"] == "server"
                and packs_by_name[pack["name"]]["builtin"] is False
                for pack in BEHAVIOR_PACKS
            )
            rules_ok = all(
                self.single_world_value(last_rules, name).casefold() == expected.casefold()
                for name, expected in MODIFIED_GAMERULES.items()
            )
            if last_players == BOT_COUNT and packs_ok and rules_ok:
                self.result["local_metadata_players"] = last_players
                self.result["local_metadata_validated"] = True
                self.result["behavior_pack_metadata"] = [packs_by_name[pack["name"]] for pack in BEHAVIOR_PACKS]
                self.result["validated_gamerules"] = {
                    name: self.single_world_value(last_rules, name) for name in MODIFIED_GAMERULES
                }
                self._write_results()
                self.check(
                    "combined-health-metadata",
                    "PASS",
                    "health protobuf simultaneously contains 20 players, three active real behavior packs, and all modified gamerules",
                    players=last_players,
                    data_packs=self.result["behavior_pack_metadata"],
                    gamerules=self.result["validated_gamerules"],
                )
                return
            time.sleep(5)

        raise RuntimeError(
            "combined health metadata did not converge: "
            f"players={last_players}, packs={last_packs}, "
            f"gamerules={{k: self.single_world_value(last_rules, k) for k in MODIFIED_GAMERULES if k in last_rules}}"
        )

    def run_public_health_upload(self) -> str:
        assert self.server is not None
        start = self.server.command("spark health upload")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            lines = self.server.snapshot()
            url = self._viewer_url(lines, start)
            recent = lines[start:]
            if url and any("health report uploaded!" in line.casefold() for line in recent):
                self.result["health_upload_viewer_url"] = url
                self._write_results()
                self.check("combined-public-health-upload", "PASS", viewer_url=url)
                return url
            if any("health report upload failed" in line.casefold() for line in recent):
                raise RuntimeError("Spark public health upload failed: " + " | ".join(recent[-40:]))
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during public combined health upload")
            time.sleep(0.5)
        raise RuntimeError("public combined health upload produced no viewer URL")

    def assert_20_players(self, phase: str) -> None:
        output, _ = self.wait_player_count(BOT_COUNT, timeout=20)
        self.check(
            f"combined-20-players-{phase}",
            "PASS",
            f"{BOT_COUNT} players remain visible in BDS",
            output=" | ".join(output[-30:]),
        )

    def run_local_metadata_phase(self) -> None:
        previous_bytebin = os.environ.get("SPARK_BYTEBINURL")
        try:
            self.capture.start()
            os.environ["SPARK_BYTEBINURL"] = self.capture.base_url
            self.start_server()
            self.wait_post_start_initialization()
            self.verify_behavior_pack_functions()
            self.apply_modified_gamerules()
            self.bot_log = self.local_bot_log
            self.start_fleet()
            time.sleep(20)
            self.assert_20_players("local-metadata")
            self.validate_local_metadata_with_20_players()
            self.stop_fleet()
            self.stop_server_for_phase_change("local-metadata")
        finally:
            self.capture.stop()
            if previous_bytebin is None:
                os.environ.pop("SPARK_BYTEBINURL", None)
            else:
                os.environ["SPARK_BYTEBINURL"] = previous_bytebin

    def run_public_profile_phase(self) -> None:
        self.start_server()
        self.wait_post_start_initialization()
        self.verify_behavior_pack_functions()
        self.apply_modified_gamerules()
        self.bot_log = self.public_bot_log
        self.start_fleet()
        time.sleep(20)
        self.assert_20_players("before-public-reports")

        self.command_check("combined-health-show", "spark health show")
        self.run_public_health_upload()

        execution_url, rss_samples = self.profile_execution()
        self.result["spark_profile_viewer_url"] = execution_url
        self.result["execution_profile_viewer_url"] = execution_url
        self._write_results()
        self.check("combined-execution-profile", "PASS", viewer_url=execution_url)
        self.assert_20_players("after-execution-profile")

        allocation_url = self.run_profiler(allocation=True)
        self.result["allocation_profile_viewer_url"] = allocation_url
        self._write_results()
        self.assert_20_players("after-allocation-profile")

        assert self.server is not None
        start = self.server.command("spark tps")
        spark_output = self.server.wait_command_output(start, 8)
        metrics = self.parse_spark_metrics(spark_output, rss_samples)
        self.result["metrics"] = metrics
        self.check(
            "combined-load-metrics",
            "PASS",
            execution_viewer_url=execution_url,
            allocation_viewer_url=allocation_url,
            health_viewer_url=self.result["health_upload_viewer_url"],
            **metrics,
        )

        self.stop_fleet()
        self._set_phase_shutdown_context("public-profile-final", phase_ordinal=3)
        self.shutdown()
        self.server = None

    def execute_combined(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            self._write_results()

            stage = "world-bootstrap"
            self.bootstrap_scenario_world()

            stage = "local-20-player-metadata"
            self.run_local_metadata_phase()

            stage = "public-20-player-profiles"
            self.run_public_profile_phase()

            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self.result["completed_at"] = now_iso()
            self._write_results()
            return 0
        except Exception as exc:  # noqa: BLE001
            self.result["status"] = "FAIL"
            self.result["state"] = "failed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"
            self.result["completed_at"] = now_iso()
            self._write_results()
            diagnostic = traceback.format_exc()
            traceback.print_exc()
            try:
                if self.bot is not None and self.bot.is_alive():
                    self.bot.force_close()
            except Exception:  # noqa: BLE001
                diagnostic += "\n\nBot cleanup failure:\n" + traceback.format_exc()
            try:
                if self.server is not None:
                    if self.server.is_alive():
                        self.server.force_kill_tree()
                        self.result["shutdown_status"] = "forced_after_failure"
                    self._record_phase_lifecycle()
                    self.server.close()
            except Exception:  # noqa: BLE001
                diagnostic += "\n\nServer cleanup failure:\n" + traceback.format_exc()
            last_lines = self.server.snapshot()[-400:] if self.server is not None else []
            self.diagnostics.write_text(
                diagnostic
                + "\n\nLast BDS log lines:\n"
                + "\n".join(last_lines)
                + "\n\nShutdown lifecycle evidence:\n"
                + json.dumps(self.result.get("shutdown_lifecycle_events", []), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            self._write_results()
            return 1
        finally:
            self.capture.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()
    validator = CombinedPackGameruleFleetValidation(
        args.platform,
        pathlib.Path(args.bot),
        args.profile_seconds,
    )
    code = validator.execute_combined()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
