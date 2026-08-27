#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import pathlib
import struct
import time
from typing import Any

from controller.block_actor_validation import _fields, _last_message
from controller.combined_pack_gamerule_fleet_validation import (
    BEHAVIOR_PACKS,
    CombinedPackGameruleFleetValidation,
)
from controller.run_test import write_json


def _decode_packed_varints(data: bytes) -> list[int]:
    values: list[int] = []
    offset = 0
    while offset < len(data):
        value = 0
        shift = 0
        while True:
            if offset >= len(data):
                raise RuntimeError("truncated packed varint in metric timestamp deltas")
            byte = data[offset]
            offset += 1
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift >= 70:
                raise RuntimeError("invalid packed varint in metric timestamp deltas")
        values.append(value)
    return values


def _double_fields(data: bytes, field_number: int) -> list[float]:
    values: list[float] = []
    for number, wire, value in _fields(data):
        if number != field_number:
            continue
        if wire == 1 and isinstance(value, bytes):
            if len(value) != 8:
                raise RuntimeError(f"invalid fixed64 size for field {field_number}: {len(value)}")
            values.append(struct.unpack("<d", value)[0])
        elif wire == 2 and isinstance(value, bytes):
            if len(value) % 8 != 0:
                raise RuntimeError(f"invalid packed double size for field {field_number}: {len(value)}")
            values.extend(struct.unpack("<" + "d" * (len(value) // 8), value))
    return values


def _varint_fields(data: bytes, field_number: int) -> list[int]:
    values: list[int] = []
    for number, wire, value in _fields(data):
        if number != field_number:
            continue
        if wire == 0 and isinstance(value, int):
            values.append(value)
        elif wire == 2 and isinstance(value, bytes):
            values.extend(_decode_packed_varints(value))
    return values


def decode_allocation_evidence(health_data: bytes) -> dict[str, Any]:
    metadata = _last_message(health_data, 1)  # HealthData.metadata
    platform_statistics = _last_message(metadata, 3)  # HealthMetadata.platform_statistics
    metrics = _last_message(metadata, 9)  # HealthMetadata.metrics
    memory = _last_message(platform_statistics, 1)  # PlatformStatistics.memory
    allocation_series = _last_message(metrics, 7)  # Metrics.memory_allocation

    starts = _varint_fields(allocation_series, 1)
    deltas = _varint_fields(allocation_series, 2)
    values = _double_fields(allocation_series, 3)
    if len(values) < 2:
        raise RuntimeError(f"Metrics.memory_allocation field 7 has only {len(values)} samples")
    if not starts or starts[-1] <= 0:
        raise RuntimeError(f"invalid allocation series start timestamp: {starts[-1] if starts else None}")
    # spark's MetricSeries encoding mirrors upstream: one delta per value,
    # with a zero delta for the first value because start_timestamp_ms carries
    # the absolute first timestamp.
    if len(deltas) != len(values):
        raise RuntimeError(f"timestamp delta count mismatch: values={len(values)} deltas={len(deltas)}")
    if deltas[0] != 0 or any(delta <= 0 for delta in deltas[1:]):
        raise RuntimeError(f"invalid metric timestamp deltas: {deltas}")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise RuntimeError(f"invalid allocation-rate values: {values}")
    if max(values) <= 0:
        raise RuntimeError("real active BDS produced no positive allocation-rate sample")

    rolling: dict[str, dict[str, float]] = {}
    for number, name in (
        (4, "alloc_bps_last1m"),
        (5, "alloc_bps_last5m"),
        (6, "alloc_bps_last15m"),
    ):
        encoded = _last_message(memory, number)
        field_values: dict[int, float] = {}
        for field_number, wire, raw in _fields(encoded):
            if wire == 1 and isinstance(raw, bytes) and len(raw) == 8:
                field_values[field_number] = struct.unpack("<d", raw)[0]
        stats = {
            "mean": field_values.get(1, 0.0),
            "max": field_values.get(2, 0.0),
            "min": field_values.get(3, 0.0),
            "median": field_values.get(4, 0.0),
            "p95": field_values.get(5, 0.0),
        }
        if any(not math.isfinite(value) or value < 0 for value in stats.values()):
            raise RuntimeError(f"{name} contains invalid values: {stats}")
        if stats["mean"] <= 0 or stats["max"] <= 0:
            raise RuntimeError(f"{name} is not positive under real active load: {stats}")
        if not (stats["min"] <= stats["median"] <= stats["p95"] <= stats["max"]):
            raise RuntimeError(f"{name} distribution ordering invalid: {stats}")
        rolling[name] = stats

    return {
        "start_timestamp_ms": starts[-1],
        "memory_allocation_samples": len(values),
        "memory_allocation_values": values,
        "timestamp_deltas_ms": deltas,
        "rolling": rolling,
    }


class AllocationRateCombinedValidation(CombinedPackGameruleFleetValidation):
    def verify_behavior_pack_functions(self) -> None:
        assert self.server is not None
        for pack in BEHAVIOR_PACKS:
            command = f"function {pack['function']}"
            start = self.server.command(command)
            marker = pack["marker"].casefold()
            rejected = ("unknown function", "function not found", "failed to execute", "syntax error")
            deadline = time.monotonic() + 12.0
            output: list[str] = []
            while time.monotonic() < deadline:
                output = self.server.snapshot()[start:]
                joined = "\n".join(output).casefold()
                if any(text in joined for text in rejected):
                    raise RuntimeError(
                        f"BDS rejected behavior-pack function {pack['function']!r}: " + " | ".join(output[-30:])
                    )
                if marker in joined:
                    self.check(
                        f"behavior-pack-function-{pack['function']}",
                        "PASS",
                        command,
                        marker=pack["marker"],
                    )
                    break
                if not self.server.is_alive():
                    raise RuntimeError(f"BDS exited while executing behavior-pack function {pack['function']!r}")
                time.sleep(0.25)
            else:
                raise RuntimeError(
                    f"behavior-pack function {pack['function']!r} produced no expected marker "
                    f"{pack['marker']!r}: " + " | ".join(output[-30:])
                )
        self.check(
            "behavior-packs-real-load",
            "PASS",
            "all three behavior-pack functions executed inside real BDS",
            count=len(BEHAVIOR_PACKS),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()

    validator = AllocationRateCombinedValidation(
        args.platform,
        pathlib.Path(args.bot),
        args.profile_seconds,
    )
    code = validator.execute()
    if code != 0:
        print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
        return code

    health_payload = validator.capture.latest()
    evidence = decode_allocation_evidence(health_payload)
    evidence.update(
        {
            "platform": args.platform,
            "spark_sha": validator.result.get("spark_sha"),
            "players": validator.result.get("local_metadata_players"),
            "behavior_packs": [
                item.get("name") for item in (validator.result.get("behavior_pack_metadata") or [])
            ],
            "gamerules": validator.result.get("validated_gamerules") or {},
            "health_viewer_url": validator.result.get("health_upload_viewer_url"),
            "execution_profile_viewer_url": validator.result.get("execution_profile_viewer_url"),
            "allocation_profile_viewer_url": validator.result.get("allocation_profile_viewer_url"),
            "metrics": validator.result.get("metrics") or {},
        }
    )
    if evidence["players"] != 20:
        raise RuntimeError(f"expected 20 metadata players, got {evidence['players']}")
    if len(evidence["behavior_packs"]) != 3:
        raise RuntimeError(f"expected three behavior packs, got {evidence['behavior_packs']}")
    if len(evidence["gamerules"]) < 6:
        raise RuntimeError(f"expected modified gamerule evidence, got {evidence['gamerules']}")
    for key in ("health_viewer_url", "execution_profile_viewer_url", "allocation_profile_viewer_url"):
        if not evidence.get(key):
            raise RuntimeError(f"missing public viewer URL: {key}")
    write_json(pathlib.Path("allocation-rate-evidence.json"), evidence)
    print(json.dumps(evidence, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
