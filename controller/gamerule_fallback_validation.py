#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import traceback
from typing import Any

from controller.block_actor_validation import BytebinCapture, ProtoDecodeError, _fields, _last_message, _messages
from controller.run_test import IntegrationTest, now_iso, write_json


def _string_field(data: bytes, number: int) -> str | None:
    for field_number, wire, value in _fields(data):
        if field_number == number and wire == 2 and isinstance(value, bytes):
            return value.decode("utf-8")
    return None


def decode_gamerules(health_data: bytes) -> dict[str, dict[str, Any]]:
    metadata = _last_message(health_data, 1)  # HealthData.metadata
    platform_statistics = _last_message(metadata, 3)  # HealthMetadata.platform_statistics
    world = _last_message(platform_statistics, 8)  # PlatformStatistics.world
    result: dict[str, dict[str, Any]] = {}
    for encoded in _messages(world, 4):  # WorldStatistics.game_rules
        name = _string_field(encoded, 1)
        if not name:
            raise ProtoDecodeError("gamerule entry is missing name")
        default_value = _string_field(encoded, 2)
        world_values: dict[str, str] = {}
        for map_entry in _messages(encoded, 3):
            key = _string_field(map_entry, 1)
            value = _string_field(map_entry, 2)
            if key is not None and value is not None:
                world_values[key] = value
        result[name.lower()] = {
            "default_present": default_value is not None,
            "default": default_value,
            "world_values": world_values,
        }
    if not result:
        raise ProtoDecodeError("health payload contains no gamerules")
    return result


class GameruleFallbackValidation(IntegrationTest):
    def __init__(self) -> None:
        super().__init__("linux")
        self.capture = BytebinCapture(self.root / "health-captures")
        self.result.update(
            {
                "test_kind": "spark-gamerule-default-fallback-real-bds",
                "spark_sha": os.environ.get("EXPECTED_SPARK_SHA", ""),
                "gamerules": {},
            }
        )
        write_json(self.result_path, self.result)

    def capture_health(self) -> bytes:
        assert self.server is not None
        before = self.capture.count()
        start = self.server.command("spark health upload")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            lines = self.server.snapshot()[start:]
            if any("health report upload failed" in line.lower() for line in lines):
                raise RuntimeError("Spark health upload failed: " + " | ".join(lines[-30:]))
            if any("health report uploaded!" in line.lower() for line in lines) and self.capture.count() > before:
                return self.capture.latest()
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during local health capture")
            time.sleep(0.25)
        raise RuntimeError("timed out waiting for locally captured Spark health payload")

    @staticmethod
    def _only_world_value(entry: dict[str, Any]) -> str:
        values = entry["world_values"]
        if len(values) != 1:
            raise RuntimeError(f"expected exactly one world gamerule value, got {values}")
        return next(iter(values.values()))

    def validate_rules(self, rules: dict[str, dict[str, Any]]) -> None:
        expected = {
            "spawnradius": "10",
            "recipesunlock": "true",
            "maxcommandchainlength": "65535",
            "randomtickspeed": "1",
        }
        for name, expected_value in expected.items():
            entry = rules.get(name)
            if entry is None:
                raise RuntimeError(f"expected gamerule {name} is missing from health metadata")
            if not entry["default_present"] or entry["default"] != expected_value:
                raise RuntimeError(
                    f"gamerule {name} fallback default mismatch: expected={expected_value!r} entry={entry}"
                )
            effective = self._only_world_value(entry)
            if effective != expected_value:
                raise RuntimeError(
                    f"fresh-world effective value for {name} diverged from probed Bedrock default: "
                    f"expected={expected_value!r} actual={effective!r}"
                )

        if "locatorbar" in rules:
            raise RuntimeError(f"removed locatorbar unexpectedly appeared in current BDS metadata: {rules['locatorbar']}")

        player_waypoints = rules.get("playerwaypoints")
        if player_waypoints is not None and player_waypoints["default_present"]:
            raise RuntimeError(
                "playerwaypoints default must stay unknown until the runtime/API exposes it or an authoritative current "
                f"default is available: {player_waypoints}"
            )

        self.result["gamerules"] = rules
        write_json(self.result_path, self.result)
        self.check(
            "gamerule-fallback-health-metadata",
            "PASS",
            "fallback defaults matched fresh BDS effective values and migration endpoints did not receive guessed defaults",
        )

    def execute_validation(self) -> int:
        stage = "initialization"
        previous_bytebin = os.environ.get("SPARK_BYTEBINURL")
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            self.capture.start()
            os.environ["SPARK_BYTEBINURL"] = self.capture.base_url

            stage = "bds-start"
            self.start_server()
            assert self.server is not None
            self.run_basic_commands()

            stage = "health-metadata"
            self.validate_rules(decode_gamerules(self.capture_health()))

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
            last_lines = self.server.snapshot()[-300:] if self.server is not None else []
            self.diagnostics.write_text(
                diagnostic + "\n\nLast BDS log lines:\n" + "\n".join(last_lines), encoding="utf-8"
            )
            return 1
        finally:
            try:
                self.capture.stop()
            except Exception:
                pass
            if previous_bytebin is None:
                os.environ.pop("SPARK_BYTEBINURL", None)
            else:
                os.environ["SPARK_BYTEBINURL"] = previous_bytebin
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    return GameruleFallbackValidation().execute_validation()


if __name__ == "__main__":
    raise SystemExit(main())
