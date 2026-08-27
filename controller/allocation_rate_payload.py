#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import math
import pathlib
import struct
import urllib.request
from typing import Any

BYTEBIN_URL = "https://spark-usercontent.lucko.me"
HEALTH_CONTENT_TYPE = "application/x-spark-health"
VIEWER_ORIGIN = "https://spark.lucko.me"


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _parse_fields(data: bytes) -> dict[int, list[tuple[int, Any]]]:
    fields: dict[int, list[tuple[int, Any]]] = {}
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field = key >> 3
        wire = key & 7
        if field <= 0:
            raise ValueError("invalid protobuf field number")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire == 1:
            if offset + 8 > len(data):
                raise ValueError("truncated fixed64 field")
            value = data[offset : offset + 8]
            offset += 8
        elif wire == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire == 5:
            if offset + 4 > len(data):
                raise ValueError("truncated fixed32 field")
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        fields.setdefault(field, []).append((wire, value))
    return fields


def _message(fields: dict[int, list[tuple[int, Any]]], field: int, label: str) -> bytes:
    values = fields.get(field, [])
    for wire, value in values:
        if wire == 2:
            return bytes(value)
    raise ValueError(f"missing {label} (field {field})")


def _doubles(fields: dict[int, list[tuple[int, Any]]], field: int) -> list[float]:
    result: list[float] = []
    for wire, value in fields.get(field, []):
        if wire == 1:
            result.append(struct.unpack("<d", bytes(value))[0])
        elif wire == 2:
            raw = bytes(value)
            if len(raw) % 8 != 0:
                raise ValueError(f"packed double field {field} has invalid length {len(raw)}")
            result.extend(struct.unpack(f"<{len(raw) // 8}d", raw))
    return result


def _positive_finite(values: list[float], label: str) -> list[float]:
    positive = [value for value in values if math.isfinite(value) and value > 0.0]
    if not positive:
        raise ValueError(f"{label} has no positive finite values: {values!r}")
    return positive


def fetch_health_payload(viewer_url: str) -> bytes:
    key = viewer_url.rstrip("/").rsplit("/", 1)[-1]
    if not key or key == viewer_url:
        raise ValueError(f"invalid Spark viewer URL: {viewer_url!r}")
    request = urllib.request.Request(
        f"{BYTEBIN_URL}/{key}",
        headers={
            "Accept": HEALTH_CONTENT_TYPE,
            "Origin": VIEWER_ORIGIN,
            "User-Agent": "bds-test-lab/allocation-rate-validation",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != HEALTH_CONTENT_TYPE:
            raise ValueError(f"unexpected bytebin content type: {content_type!r}")
        body = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
    if not body:
        raise ValueError("bytebin returned an empty health payload")
    return body


def validate_health_payload(payload: bytes) -> dict[str, Any]:
    health = _parse_fields(payload)
    metadata = _parse_fields(_message(health, 1, "HealthData.metadata"))

    metrics = _parse_fields(_message(metadata, 9, "HealthMetadata.metrics"))
    allocation_series = _parse_fields(_message(metrics, 7, "Metrics.memory_allocation"))
    allocation_values = _doubles(allocation_series, 3)
    positive_allocation_values = _positive_finite(allocation_values, "Metrics.memory_allocation.values")

    platform_statistics = _parse_fields(_message(metadata, 3, "HealthMetadata.platform_statistics"))
    memory = _parse_fields(_message(platform_statistics, 1, "PlatformStatistics.memory"))
    rolling: dict[str, float] = {}
    for field, label in ((4, "1m"), (5, "5m"), (6, "15m")):
        distribution = _parse_fields(_message(memory, field, f"PlatformStatistics.Memory.alloc_bps_last{label}"))
        means = _positive_finite(_doubles(distribution, 1), f"alloc_bps_last{label}.mean")
        rolling[label] = means[-1]

    return {
        "allocation_metric_samples": len(allocation_values),
        "allocation_metric_positive_samples": len(positive_allocation_values),
        "allocation_metric_latest_bps": allocation_values[-1],
        "alloc_bps_last1m_mean": rolling["1m"],
        "alloc_bps_last5m_mean": rolling["5m"],
        "alloc_bps_last15m_mean": rolling["15m"],
    }


def validate_results(results_path: pathlib.Path) -> dict[str, Any]:
    results = json.loads(results_path.read_text(encoding="utf-8"))
    viewer_url = str(results.get("post_soak_health_upload_viewer_url") or "").strip()
    if not viewer_url:
        raise ValueError("test-results.json has no post_soak_health_upload_viewer_url")
    evidence = validate_health_payload(fetch_health_payload(viewer_url))
    evidence["viewer_url"] = viewer_url
    evidence["platform"] = results.get("platform")
    evidence["spark_sha"] = results.get("spark_sha")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="test-results.json")
    parser.add_argument("--output", default="allocation-rate-protobuf.json")
    args = parser.parse_args()
    evidence = validate_results(pathlib.Path(args.results))
    pathlib.Path(args.output).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
