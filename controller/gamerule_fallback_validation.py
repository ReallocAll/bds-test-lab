#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import traceback
from typing import Any

from controller.block_actor_validation import BytebinCapture, ProtoDecodeError, _fields, _last_message, _messages
from controller.run_test import IntegrationTest, now_iso, write_json


EXPECTED_DEFAULTS = {
    "spawnradius": "10",
    "maxcommandchainlength": "65535",
    "recipesunlock": "true",
    "randomtickspeed": "1",
}
DEFAULT_UNKNOWN_RULES = {"playerwaypoints"}


def _optional_text_field(data: bytes, field_number: int) -> str | None:
    values = [
        value.decode("utf-8")
        for number, wire, value in _fields(data)
        if number == field_number and wire == 2 and isinstance(value, bytes)
    ]
    return values[-1] if values else None


def decode_gamerules(health_data: bytes) -> dict[str, dict[str, Any]]:
    metadata = _last_message(health_data, 1)  # HealthData.metadata
    platform_statistics = _last_message(metadata, 3)  # HealthMetadata.platform_statistics
    world_statistics = _last_message(platform_statistics, 8)  # PlatformStatistics.world

    rules: dict[str, dict[str, Any]] = {}
    for encoded in _messages(world_statistics, 4):  # WorldStatistics.game_rules
        name = _optional_text_field(encoded, 1)
        if not name:
            raise ProtoDecodeError("gamerule entry is missing its name")
        default_value = _optional_text_field(encoded, 2)
        world_values: dict[str, str] = {}
        for encoded_world_value in _messages(encoded, 3):
            world_name = _optional_text_field(encoded_world_value, 1)
            value = _optional_text_field(encoded_world_value, 2)
            if world_name is not None and value is not None:
                world_values[world_name] = value
        rules[name.lower()] = {
            "default": default_value,
            "default_present": default_value is not None,
            "world_values": world_values,
        }
    if not rules:
        raise ProtoDecodeError("health payload contains no gamerule metadata")
    return rules


class GameruleFallbackValidation(IntegrationTest):
    def __init__(self) -> None:
        super().__init__("linux")
        self.capture = BytebinCapture(self.root / "gamerule-health-capture")
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
                raise RuntimeError("BDS exited during gamerule health capture")
            time.sleep(0.25)
        raise RuntimeError("timed out waiting for locally captured Spark health payload")

    def validate_gamerules(self) -> None:
        rules = decode_gamerules(self.capture_health())
        self.result["gamerules"] = rules
        write_json(self.result_path, self.result)

        for name, expected in EXPECTED_DEFAULTS.items():
            actual = rules.get(name)
            if actual is None:
                raise RuntimeError(f"expected gamerule {name!r} is absent from Spark metadata")
            if actual["default"] != expected:
                raise RuntimeError(
                    f"gamerule {name!r} default mismatch: expected {expected!r}, got {actual['default']!r}"
                )
            self.check(
                "gamerule-current-default",
                "PASS",
                name=name,
                expected=expected,
                actual=actual["default"],
            )

        for name in DEFAULT_UNKNOWN_RULES:
            actual = rules.get(name)
            if actual is None:
                raise RuntimeError(f"expected runtime gamerule {name!r} is absent from Spark metadata")
            if actual["default_present"]:
                raise RuntimeError(
                    f"gamerule {name!r} must remain default-unknown, but Spark serialized {actual['default']!r}"
                )
            self.check(
                "gamerule-default-unknown",
                "PASS",
                name=name,
                detail="default field omitted rather than guessed",
            )

        self.check(
            "gamerule-fallback-metadata",
            "PASS",
            "current Bedrock defaults were serialized and playerWaypoints remained default-unknown",
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

            stage = "spark-sanity"
            self.run_basic_commands()

            stage = "gamerule-metadata"
            self.validate_gamerules()

            stage = "shutdown"
            self.shutdown()
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
            traceback.print_exc()
            try:
                self.shutdown_after_failure()
            except Exception:
                traceback.print_exc()
            return 1
        finally:
            self.capture.stop()
            if previous_bytebin is None:
                os.environ.pop("SPARK_BYTEBINURL", None)
            else:
                os.environ["SPARK_BYTEBINURL"] = previous_bytebin


def main() -> int:
    return GameruleFallbackValidation().execute_validation()


if __name__ == "__main__":
    raise SystemExit(main())
