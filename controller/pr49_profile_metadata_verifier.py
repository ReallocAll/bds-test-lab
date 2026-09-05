from __future__ import annotations

import gzip
import json
from pathlib import Path
import sys

import requests
from google.protobuf.message import DecodeError

EXPECTED_CYCLES = 3
EXPECTED_ENGINE = "native-ucrt/permanent-iat"
EXPECTED_BACKEND = "Windows UCRT/permanent IAT gateway"


def _load_sampler(viewer_url: str):
    sys.path.insert(0, str(Path("generated").resolve()))
    from spark import spark_sampler_pb2

    key = viewer_url.rstrip("/").rsplit("/", 1)[-1]
    response = requests.get(f"https://spark-usercontent.lucko.me/{key}", timeout=30)
    response.raise_for_status()
    payload = response.content
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    sampler = spark_sampler_pb2.SamplerData()
    try:
        sampler.ParseFromString(payload)
    except DecodeError as exc:
        raise RuntimeError(f"unable to decode SamplerData for {viewer_url}") from exc
    return sampler


def _backend_value(raw: str) -> str:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return value if isinstance(value, str) else raw


def main() -> int:
    data = json.loads(Path("fleet-spark-result.json").read_text(encoding="utf-8"))
    if data.get("status") != "PASS":
        raise RuntimeError(f"real-BDS scenario is not PASS: {data.get('error_summary')}")

    cycles = data.get("hot_reload_cycles") or []
    if len(cycles) != EXPECTED_CYCLES:
        raise RuntimeError(f"expected {EXPECTED_CYCLES} true hot reload cycles, got {len(cycles)}")
    if not all(row.get("same_bds_identity") is True for row in cycles):
        raise RuntimeError(f"hot reload changed BDS identity: {cycles}")
    identities = {(row.get("bds_pid"), row.get("bds_create_time")) for row in cycles}
    if len(identities) != 1:
        raise RuntimeError(f"hot reload cycles disagree on BDS process identity: {identities}")

    profiles = [
        ("main-allocation", data.get("allocation_profile_viewer_url")),
        ("live-only", data.get("allocation_live_only_profile_viewer_url")),
    ]
    profiles.extend(
        (f"hot-reload-{row.get('cycle')}", row.get("allocation_profile_viewer_url")) for row in cycles
    )

    evidence = []
    for label, raw_url in profiles:
        viewer_url = str(raw_url or "").strip()
        if not viewer_url:
            raise RuntimeError(f"missing allocation profile viewer URL for {label}")
        sampler = _load_sampler(viewer_url)
        metadata = sampler.metadata
        if metadata.sampler_mode != 1:
            raise RuntimeError(f"{label}: expected ALLOCATION sampler_mode=1, got {metadata.sampler_mode}")
        engine = metadata.sampler_engine_version
        if EXPECTED_ENGINE not in engine or "funchook" in engine.casefold():
            raise RuntimeError(f"{label}: incorrect no-shim sampler engine version: {engine!r}")
        backend = _backend_value(metadata.extra_platform_metadata.get("Allocation backend", ""))
        if backend != EXPECTED_BACKEND:
            raise RuntimeError(f"{label}: incorrect Allocation backend metadata: {backend!r}")
        evidence.append(
            {
                "label": label,
                "viewer_url": viewer_url,
                "sampler_mode": metadata.sampler_mode,
                "sampler_engine_version": engine,
                "allocation_backend": backend,
            }
        )

    Path("allocation-backend-metadata-evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
