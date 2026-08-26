#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import os
import pathlib
import re
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from controller.bot_validation import patch_server_properties
from controller.fleet_spark_validation import set_server_property
from controller.run_test import IntegrationTest, now_iso, write_json

MSPT_RE = re.compile(
    r"MSPT 10s \(mean/min/median/p95/max\):\s*"
    r"([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)",
    re.IGNORECASE,
)
TARGET_X = 4096
TARGET_Y = 64
TARGET_Z = 4096
TICKING_AREA_NAME = "spark_blockactor"


class ProtoDecodeError(RuntimeError):
    pass


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, offset
        shift += 7
    raise ProtoDecodeError("invalid or truncated protobuf varint")


def _fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    fields: list[tuple[int, int, int | bytes]] = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number = key >> 3
        wire = key & 7
        if number == 0:
            raise ProtoDecodeError("protobuf field number 0 is invalid")
        if wire == 0:
            value, offset = _read_varint(data, offset)
            fields.append((number, wire, value))
        elif wire == 1:
            end = offset + 8
            if end > len(data):
                raise ProtoDecodeError("truncated fixed64 field")
            fields.append((number, wire, data[offset:end]))
            offset = end
        elif wire == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ProtoDecodeError("truncated length-delimited field")
            fields.append((number, wire, data[offset:end]))
            offset = end
        elif wire == 5:
            end = offset + 4
            if end > len(data):
                raise ProtoDecodeError("truncated fixed32 field")
            fields.append((number, wire, data[offset:end]))
            offset = end
        else:
            raise ProtoDecodeError(f"unsupported protobuf wire type {wire}")
    return fields


def _messages(data: bytes, field_number: int) -> list[bytes]:
    return [value for number, wire, value in _fields(data) if number == field_number and wire == 2 and isinstance(value, bytes)]


def _last_message(data: bytes, field_number: int) -> bytes:
    values = _messages(data, field_number)
    if not values:
        raise ProtoDecodeError(f"missing protobuf message field {field_number}")
    return values[-1]


def decode_world_info_samples(health_data: bytes) -> list[dict[str, Any]]:
    """Extract Metrics.world_info.Values while preserving tile_entities presence."""
    metadata = _last_message(health_data, 1)  # HealthData.metadata
    metrics = _last_message(metadata, 9)  # HealthMetadata.metrics
    world_info = _last_message(metrics, 8)  # Metrics.world_info
    samples: list[dict[str, Any]] = []
    for encoded in _messages(world_info, 3):  # WorldInfoMetricSeries.values
        parsed = _fields(encoded)
        values = {number: value for number, wire, value in parsed if wire == 0 and isinstance(value, int)}
        samples.append(
            {
                "players": int(values.get(1, 0)),
                "entities": int(values.get(2, 0)),
                "tile_entities": int(values.get(3, 0)),
                "tile_entities_present": 3 in values,
                "chunks": int(values.get(4, 0)),
            }
        )
    if not samples:
        raise ProtoDecodeError("health payload contains no world-info metric samples")
    return samples


class BytebinCapture:
    def __init__(self, output_dir: pathlib.Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._payloads: list[bytes] = []
        capture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                if self.path != "/post":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(length)
                    if self.headers.get("Content-Encoding", "").lower() == "gzip":
                        body = gzip.decompress(body)
                    with capture._lock:
                        capture._payloads.append(body)
                        index = len(capture._payloads)
                    (capture.output_dir / f"health-{index:03d}.bin").write_bytes(body)
                    self.send_response(201)
                    self.send_header("Location", f"/capture/{index}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except Exception as exc:
                    self.send_error(500, str(exc))

            def log_message(self, fmt: str, *args: object) -> None:
                print("[bytebin-capture] " + (fmt % args), flush=True)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="bytebin-capture", daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def count(self) -> int:
        with self._lock:
            return len(self._payloads)

    def latest(self) -> bytes:
        with self._lock:
            if not self._payloads:
                raise RuntimeError("no health payload has been captured")
            return self._payloads[-1]


class BlockActorValidation(IntegrationTest):
    def __init__(self) -> None:
        super().__init__("linux")
        self.capture = BytebinCapture(self.root / "health-captures")
        self.result.update(
            {
                "test_kind": "spark-block-actor-real-bds",
                "target": {"x": TARGET_X, "y": TARGET_Y, "z": TARGET_Z},
                "world_info_samples": [],
                "reconcile_mspt_max": [],
                "spark_sha": os.environ.get("EXPECTED_SPARK_SHA", ""),
            }
        )
        write_json(self.result_path, self.result)

    def _record_world_sample(self, phase: str, sample: dict[str, Any], sample_count: int) -> None:
        row = {"phase": phase, "sample_count": sample_count, **sample}
        self.result["world_info_samples"].append(row)
        write_json(self.result_path, self.result)
        self.check(
            f"world-info-{phase}",
            "PASS",
            json.dumps(row, sort_keys=True),
            **row,
        )

    def bootstrap_flat_world(self) -> None:
        self.start_server()
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop after initial server.properties bootstrap")
        self.server.close()
        self.server = None

        properties = self.server_dir / "server.properties"
        patch_server_properties(properties)
        set_server_property(properties, "level-name", "BlockActorValidation")
        set_server_property(properties, "level-type", "FLAT")
        set_server_property(properties, "level-seed", "1")
        set_server_property(properties, "allow-cheats", "true")
        set_server_property(properties, "player-idle-timeout", "0")
        world = self.server_dir / "worlds" / "BlockActorValidation"
        if world.exists():
            shutil.rmtree(world)
        self.check("flat-world-config", "PASS", "fresh deterministic flat world configured")
        self.start_server()

    def capture_world_info(self) -> tuple[dict[str, Any], int]:
        assert self.server is not None
        before = self.capture.count()
        start = self.server.command("spark health upload")
        deadline = time.monotonic() + 45
        success = False
        while time.monotonic() < deadline:
            lines = self.server.snapshot()[start:]
            if any("health report upload failed" in line.lower() for line in lines):
                raise RuntimeError("Spark health upload failed: " + " | ".join(lines[-30:]))
            if any("health report uploaded!" in line.lower() for line in lines) and self.capture.count() > before:
                success = True
                break
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during local health capture")
            time.sleep(0.25)
        if not success:
            raise RuntimeError("timed out waiting for locally captured Spark health payload")
        samples = decode_world_info_samples(self.capture.latest())
        return samples[-1], len(samples)

    def wait_world_info(self, phase: str, predicate, *, timeout: float, min_sample_count: int = 0) -> tuple[dict[str, Any], int]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        last_count = 0
        while time.monotonic() < deadline:
            sample, count = self.capture_world_info()
            last, last_count = sample, count
            if count > min_sample_count and predicate(sample):
                self._record_world_sample(phase, sample, count)
                return sample, count
            time.sleep(5)
        raise RuntimeError(
            f"timed out waiting for world-info phase {phase}; last_sample={last}, sample_count={last_count}, "
            f"minimum_sample_count={min_sample_count}"
        )

    def command(self, command: str) -> list[str]:
        assert self.server is not None
        start = self.server.command(command)
        output = self.server.wait_command_output(start, 8)
        joined = "\n".join(output).lower()
        if not self.server.is_alive():
            raise RuntimeError(f"BDS exited while executing {command}")
        if "unknown command" in joined or "syntax error" in joined:
            raise RuntimeError(f"BDS rejected command {command}: " + " | ".join(output[-20:]))
        return output

    def record_reconcile_mspt(self, phase: str) -> None:
        output = self.command("spark tps")
        match = MSPT_RE.search("\n".join(output))
        if match is None:
            raise RuntimeError("unable to parse MSPT after BlockActor reconciliation: " + " | ".join(output[-30:]))
        maximum = float(match.group(5))
        entry = {"phase": phase, "mspt_10s_max": maximum}
        self.result["reconcile_mspt_max"].append(entry)
        write_json(self.result_path, self.result)
        if maximum >= 1000.0:
            raise RuntimeError(f"BlockActor reconciliation caused pathological 10s max MSPT: {maximum:.3f} ms")
        self.check("block-actor-reconcile-mspt", "PASS", phase=phase, mspt_10s_max=maximum)

    def run_block_actor_lifecycle(self) -> None:
        baseline, baseline_samples = self.wait_world_info(
            "baseline-zero-present",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] == 0,
            timeout=90,
        )
        if baseline["tile_entities"] != 0:
            raise RuntimeError(f"fresh flat-world baseline was not zero: {baseline}")

        self.command(
            f"tickingarea add circle {TARGET_X} {TARGET_Y} {TARGET_Z} 1 {TICKING_AREA_NAME}"
        )
        time.sleep(3)
        self.command(f"setblock {TARGET_X} {TARGET_Y} {TARGET_Z} chest")
        verify = self.command(f"testforblock {TARGET_X} {TARGET_Y} {TARGET_Z} chest")
        if any("failed" in line.lower() for line in verify):
            raise RuntimeError("BDS did not confirm the BlockActor chest: " + " | ".join(verify[-20:]))

        placed, placed_samples = self.wait_world_info(
            "placed-nonzero",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] >= 1,
            timeout=90,
            min_sample_count=baseline_samples,
        )
        self.record_reconcile_mspt("placed")

        self.command(f"tickingarea remove {TICKING_AREA_NAME}")
        unloaded, unloaded_samples = self.wait_world_info(
            "unloaded-zero-present",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] == 0,
            timeout=60,
            min_sample_count=placed_samples,
        )
        if unloaded["tile_entities"] >= placed["tile_entities"]:
            raise RuntimeError(f"BlockActor count did not decrease after chunk unload: placed={placed}, unloaded={unloaded}")

        self.command(
            f"tickingarea add circle {TARGET_X} {TARGET_Y} {TARGET_Z} 1 {TICKING_AREA_NAME}"
        )
        incomplete, incomplete_samples = self.wait_world_info(
            "reload-presence-cleared",
            lambda sample: not sample["tile_entities_present"],
            timeout=35,
            min_sample_count=unloaded_samples,
        )
        if incomplete["tile_entities_present"]:
            raise RuntimeError(f"tile-entity presence did not clear while a newly loaded chunk awaited reconciliation: {incomplete}")

        reloaded, _ = self.wait_world_info(
            "reload-reconciled-nonzero",
            lambda sample: sample["tile_entities_present"] and sample["tile_entities"] >= 1,
            timeout=90,
            min_sample_count=incomplete_samples,
        )
        if reloaded["tile_entities"] < placed["tile_entities"]:
            raise RuntimeError(f"BlockActor count did not recover after reload: placed={placed}, reloaded={reloaded}")
        self.record_reconcile_mspt("reloaded")

        self.command(f"setblock {TARGET_X} {TARGET_Y} {TARGET_Z} air")
        self.command(f"tickingarea remove {TICKING_AREA_NAME}")
        self.check(
            "block-actor-lifecycle",
            "PASS",
            "real BDS reported explicit zero, nonzero BlockActor count, unload removal, reload unknown-presence, and reconciled restoration",
        )

    def execute_validation(self) -> int:
        stage = "initialization"
        previous_bytebin = os.environ.get("SPARK_BYTEBINURL")
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            self.capture.start()
            os.environ["SPARK_BYTEBINURL"] = self.capture.base_url

            stage = "bds-bootstrap"
            self.bootstrap_flat_world()
            assert self.server is not None

            stage = "spark-sanity"
            self.run_basic_commands()

            stage = "block-actor-lifecycle"
            self.run_block_actor_lifecycle()

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
                    try:
                        self.server.command(f"tickingarea remove {TICKING_AREA_NAME}")
                    except Exception:
                        pass
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
    parser = argparse.ArgumentParser()
    parser.parse_args()
    return BlockActorValidation().execute_validation()


if __name__ == "__main__":
    raise SystemExit(main())
