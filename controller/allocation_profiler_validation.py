#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import math
import pathlib
import re
import struct
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

from controller.cross_platform_fleet_validation import CrossPlatformFleetSparkValidation
from controller.run_test import now_iso, write_json

DEFAULT_INTERVAL = 524287
BYTEBIN_BASE = "https://spark-usercontent.lucko.me/"
USER_AGENT = "bds-test-lab/pr36-allocation-validation"


@dataclass(frozen=True)
class Field:
    number: int
    wire_type: int
    value: int | memoryview


def _read_varint(data: memoryview, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(10):
        if offset >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("protobuf varint exceeds 64 bits")


def _iter_fields(data: bytes | memoryview) -> Iterator[Field]:
    view = data if isinstance(data, memoryview) else memoryview(data)
    offset = 0
    while offset < len(view):
        tag, offset = _read_varint(view, offset)
        number = tag >> 3
        wire = tag & 7
        if number == 0:
            raise ValueError("invalid protobuf field zero")
        if wire == 0:
            value, offset = _read_varint(view, offset)
            yield Field(number, wire, value)
        elif wire == 1:
            if offset + 8 > len(view):
                raise ValueError("truncated fixed64")
            yield Field(number, wire, int.from_bytes(view[offset : offset + 8], "little"))
            offset += 8
        elif wire == 2:
            size, offset = _read_varint(view, offset)
            if size > len(view) - offset:
                raise ValueError("length-delimited field exceeds message")
            yield Field(number, wire, view[offset : offset + size])
            offset += size
        elif wire == 5:
            if offset + 4 > len(view):
                raise ValueError("truncated fixed32")
            yield Field(number, wire, int.from_bytes(view[offset : offset + 4], "little"))
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _message(field: Field) -> memoryview:
    if field.wire_type != 2 or not isinstance(field.value, memoryview):
        raise ValueError(f"field {field.number} is not a message")
    return field.value


def _text(field: Field) -> str:
    return _message(field).tobytes().decode("utf-8", "replace")


def _packed_doubles(field: Field) -> list[float]:
    if field.wire_type == 1 and isinstance(field.value, int):
        return [struct.unpack("<d", field.value.to_bytes(8, "little"))[0]]
    payload = _message(field)
    if len(payload) % 8:
        raise ValueError("misaligned packed double field")
    return [value[0] for value in struct.iter_unpack("<d", payload)]


def _packed_uint32(field: Field) -> list[int]:
    if field.wire_type == 0 and isinstance(field.value, int):
        return [field.value & 0xFFFFFFFF]
    payload = _message(field)
    values: list[int] = []
    offset = 0
    while offset < len(payload):
        value, offset = _read_varint(payload, offset)
        values.append(value & 0xFFFFFFFF)
    return values


def _map_entry(data: memoryview) -> tuple[str, str]:
    key = ""
    value = ""
    for field in _iter_fields(data):
        if field.number == 1:
            key = _text(field)
        elif field.number == 2:
            value = _text(field)
    return key, value


def _world_summary(data: memoryview) -> dict[str, int]:
    entities = 0
    worlds = 0
    regions = 0
    chunks = 0
    for field in _iter_fields(data):
        if field.number == 1 and isinstance(field.value, int):
            entities = int(field.value)
        elif field.number == 3:
            worlds += 1
            for world_field in _iter_fields(_message(field)):
                if world_field.number != 3:
                    continue
                regions += 1
                for region_field in _iter_fields(_message(world_field)):
                    if region_field.number == 2:
                        chunks += 1
    return {"entities": entities, "worlds": worlds, "regions": regions, "chunks": chunks}


def _allocation_averages(platform_statistics: memoryview) -> dict[str, float]:
    result: dict[str, float] = {}
    for field in _iter_fields(platform_statistics):
        if field.number != 1:
            continue
        for memory_field in _iter_fields(_message(field)):
            if memory_field.number not in {4, 5, 6}:
                continue
            window = {4: "1m", 5: "5m", 6: "15m"}[memory_field.number]
            mean = 0.0
            for value_field in _iter_fields(_message(memory_field)):
                if value_field.number == 1:
                    mean_values = _packed_doubles(value_field)
                    if mean_values:
                        mean = mean_values[0]
            result[window] = mean
    return result


def _node(data: memoryview) -> dict[str, Any]:
    item = {"class": "", "method": "", "weight": 0.0, "children": 0}
    for field in _iter_fields(data):
        if field.number == 3:
            item["class"] = _text(field)
        elif field.number == 4:
            item["method"] = _text(field)
        elif field.number == 8:
            item["weight"] += math.fsum(_packed_doubles(field))
        elif field.number == 9:
            item["children"] += len(_packed_uint32(field))
    return item


def parse_sampler(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sampler_mode": None,
        "start_time_ms": 0,
        "end_time_ms": 0,
        "duration_ms": 0,
        "interval": 0,
        "diagnostics": {},
        "allocation_rate_averages": {},
        "world": {},
        "thread_count": 0,
        "tree_node_count": 0,
        "root_ref_count": 0,
        "root_weight": 0.0,
        "hottest_nodes": [],
    }
    hottest: list[dict[str, Any]] = []
    for field in _iter_fields(data):
        if field.number == 1:
            metadata = _message(field)
            platform_stats: memoryview | None = None
            for meta_field in _iter_fields(metadata):
                if meta_field.number == 2 and isinstance(meta_field.value, int):
                    result["start_time_ms"] = int(meta_field.value)
                elif meta_field.number == 3 and isinstance(meta_field.value, int):
                    result["interval"] = int(meta_field.value)
                elif meta_field.number == 8:
                    platform_stats = _message(meta_field)
                    result["allocation_rate_averages"] = _allocation_averages(platform_stats)
                    for ps_field in _iter_fields(platform_stats):
                        if ps_field.number == 8:
                            result["world"] = _world_summary(_message(ps_field))
                elif meta_field.number == 11 and isinstance(meta_field.value, int):
                    result["end_time_ms"] = int(meta_field.value)
                elif meta_field.number == 14:
                    key, value = _map_entry(_message(meta_field))
                    if key:
                        result["diagnostics"][key] = value
                elif meta_field.number == 15 and isinstance(meta_field.value, int):
                    result["sampler_mode"] = int(meta_field.value)
            continue
        if field.number != 2:
            continue
        result["thread_count"] += 1
        for thread_field in _iter_fields(_message(field)):
            if thread_field.number == 3:
                node = _node(_message(thread_field))
                result["tree_node_count"] += 1
                hottest.append(node)
            elif thread_field.number == 4:
                result["root_weight"] += math.fsum(_packed_doubles(thread_field))
            elif thread_field.number == 5:
                result["root_ref_count"] += len(_packed_uint32(thread_field))
    result["duration_ms"] = max(0, int(result["end_time_ms"]) - int(result["start_time_ms"]))
    hottest.sort(key=lambda item: float(item["weight"]), reverse=True)
    result["hottest_nodes"] = hottest[:20]
    return result


def parse_health_allocation_series(data: bytes) -> dict[str, Any]:
    generated_time = 0
    start_time = 0
    deltas: list[int] = []
    values: list[float] = []
    for field in _iter_fields(data):
        if field.number != 1:
            continue
        for metadata_field in _iter_fields(_message(field)):
            if metadata_field.number == 5 and isinstance(metadata_field.value, int):
                generated_time = int(metadata_field.value)
            elif metadata_field.number == 9:
                for metrics_field in _iter_fields(_message(metadata_field)):
                    if metrics_field.number != 7:
                        continue
                    for series_field in _iter_fields(_message(metrics_field)):
                        if series_field.number == 1 and isinstance(series_field.value, int):
                            start_time = int(series_field.value)
                        elif series_field.number == 2:
                            deltas.extend(_packed_uint32(series_field))
                        elif series_field.number == 3:
                            values.extend(_packed_doubles(series_field))
    timestamps: list[int] = []
    current = start_time
    for index, delta in enumerate(deltas):
        if index == 0:
            current = start_time
        else:
            current += delta
        timestamps.append(current)
    points = [
        {"timestamp_ms": timestamps[index], "bytes_per_second": values[index]}
        for index in range(min(len(timestamps), len(values)))
    ]
    return {"generated_time_ms": generated_time, "allocation_points": points}


def _download(url: str, timeout: int = 45) -> tuple[bytes, dict[str, str], int]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(512 * 1024 * 1024 + 1)
            if len(data) > 512 * 1024 * 1024:
                raise RuntimeError(f"response too large: {url}")
            headers = {key.lower(): value for key, value in response.headers.items()}
            return data, headers, int(response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc


def _viewer_key(viewer_url: str) -> str:
    path = urllib.parse.urlparse(viewer_url).path.strip("/")
    if not path:
        raise RuntimeError(f"viewer URL has no profile key: {viewer_url}")
    return path.rsplit("/", 1)[-1]


def fetch_payload(viewer_url: str, destination: pathlib.Path) -> tuple[bytes, dict[str, Any]]:
    viewer_body, viewer_headers, viewer_status = _download(viewer_url)
    if viewer_status != 200 or not viewer_body:
        raise RuntimeError(f"viewer did not load: {viewer_url} status={viewer_status}")
    key = _viewer_key(viewer_url)
    raw_url = BYTEBIN_BASE + key
    raw_body, raw_headers, raw_status = _download(raw_url)
    if raw_status != 200 or not raw_body:
        raise RuntimeError(f"raw payload did not load: {raw_url} status={raw_status}")
    destination.write_bytes(raw_body)
    decoded = gzip.decompress(raw_body) if raw_body.startswith(b"\x1f\x8b") else raw_body
    return decoded, {
        "viewer_url": viewer_url,
        "viewer_status": viewer_status,
        "viewer_bytes": len(viewer_body),
        "viewer_content_type": viewer_headers.get("content-type"),
        "raw_url": raw_url,
        "raw_status": raw_status,
        "raw_bytes": len(raw_body),
        "decoded_bytes": len(decoded),
        "raw_content_type": raw_headers.get("content-type"),
        "raw_encoding": "gzip" if raw_body.startswith(b"\x1f\x8b") else "raw",
    }


def _as_int(diagnostics: dict[str, str], key: str) -> int:
    value = diagnostics.get(key, "0").strip().strip('"')
    try:
        return int(value)
    except ValueError:
        return 0


def _as_bool(diagnostics: dict[str, str], key: str) -> bool:
    return diagnostics.get(key, "false").strip().strip('"').lower() == "true"


class AllocationProfilerValidation(CrossPlatformFleetSparkValidation):
    def __init__(
        self,
        platform_name: str,
        bot_binary: pathlib.Path,
        count: int,
        scenario: str,
        profile_seconds: int,
        interval: int,
        cycles: int,
        require_nonempty: bool,
    ):
        super().__init__(platform_name, bot_binary, count, scenario, profile_seconds)
        self.interval = interval
        self.cycles = cycles
        self.require_nonempty = require_nonempty
        self.result.update(
            {
                "test_kind": "spark-pr36-real-allocation-profiler",
                "allocation_interval_bytes": interval,
                "profile_cycles": [],
                "mob_spawning": None,
                "random_tick_speed": None,
                "difficulty": None,
                "bot_ref": None,
                "spark_sha": None,
                "endstone_sha": None,
                "persistent_resume_evidence": [],
            }
        )
        self._write_results()

    def install_artifacts(self) -> None:
        super().install_artifacts()
        components = self.metadata.get("components", {})
        spark = components.get("spark", {})
        endstone = components.get("endstone", {})
        self.result["spark_sha"] = spark.get("sha")
        self.result["spark_workflow_run_id"] = spark.get("run_id")
        self.result["endstone_sha"] = endstone.get("sha")
        self.result["endstone_workflow_run_id"] = endstone.get("run_id")
        import os

        self.result["bot_ref"] = os.environ.get("BOT_REF")
        self._write_results()

    def verify_normal_world(self) -> None:
        assert self.server is not None
        mob_start = self.server.command("gamerule doMobSpawning")
        mob_output = self.server.wait_command_output(mob_start, 5)
        mob_text = "\n".join(mob_output).lower()
        if "false" in mob_text or "true" not in mob_text:
            raise RuntimeError("doMobSpawning is not confirmed enabled: " + " | ".join(mob_output[-20:]))
        self.result["mob_spawning"] = True

        tick_start = self.server.command("gamerule randomTickSpeed")
        tick_output = self.server.wait_command_output(tick_start, 5)
        tick_text = " ".join(tick_output)
        numbers = [int(value) for value in re.findall(r"\b\d+\b", tick_text)]
        if not numbers or numbers[-1] <= 0:
            raise RuntimeError("randomTickSpeed is not confirmed positive: " + " | ".join(tick_output[-20:]))
        self.result["random_tick_speed"] = numbers[-1]

        properties = self.server_dir / "server.properties"
        difficulty = ""
        if properties.exists():
            for line in properties.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("difficulty="):
                    difficulty = line.split("=", 1)[1].strip().lower()
                    break
        if difficulty == "peaceful":
            raise RuntimeError("server.properties uses peaceful difficulty")
        self.result["difficulty"] = difficulty or "unknown"
        self.check(
            "normal-world-behavior",
            "PASS",
            "mob spawning enabled; random ticks enabled; peaceful not configured",
            mob_spawning=True,
            random_tick_speed=self.result["random_tick_speed"],
            difficulty=self.result["difficulty"],
        )

    def profile_allocation(self, cycle: int) -> dict[str, Any]:
        assert self.server is not None
        command = f"spark profiler start --alloc --timeout {self.profile_seconds}"
        if self.interval != DEFAULT_INTERVAL:
            command += f" --interval {self.interval}"
        start_index = self.server.command(command)
        started_wall_ms = int(time.time() * 1000)
        info_at = self.server.command("spark profiler info")
        info_output = self.server.wait_command_output(info_at, 6)
        info_text = "\n".join(info_output).lower()
        if "allocation profiler" not in info_text:
            raise RuntimeError("allocation profiler did not report active after start: " + " | ".join(info_output[-30:]))

        deadline = time.monotonic() + self.profile_seconds + 90
        url: str | None = None
        while time.monotonic() < deadline:
            url = self._viewer_url(self.server.snapshot(), start_index)
            if url:
                break
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during allocation profile")
            time.sleep(1)
        if url is None:
            stop_at = self.server.command("spark profiler stop")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                url = self._viewer_url(self.server.snapshot(), min(start_index, stop_at))
                if url:
                    break
                if not self.server.is_alive():
                    raise RuntimeError("BDS exited while finalizing allocation profile")
                time.sleep(1)
        if url is None:
            raise RuntimeError("allocation profiler produced no viewer URL")

        raw_path = self.root / f"allocation-profile-cycle-{cycle}.sparkprofile"
        decoded, transport = fetch_payload(url, raw_path)
        parsed = parse_sampler(decoded)
        diagnostics = parsed["diagnostics"]
        if parsed["sampler_mode"] != 1:
            raise RuntimeError(f"profile mode is not ALLOCATION: {parsed['sampler_mode']}")
        if parsed["interval"] != self.interval:
            raise RuntimeError(f"serialized interval mismatch: {parsed['interval']} != {self.interval}")
        if parsed["duration_ms"] < max(1, self.profile_seconds - 3) * 1000:
            raise RuntimeError(f"allocation profile duration too short: {parsed['duration_ms']} ms")

        accepted = _as_int(diagnostics, "Allocation profile samples accepted")
        sampled_bytes = _as_int(diagnostics, "Allocation profile sampled bytes")
        sampling_points = _as_int(diagnostics, "Allocation sampling points hit (process-wide)")
        observed_bytes = _as_int(diagnostics, "Allocation observed request bytes (process-wide)")
        allocation_calls = _as_int(diagnostics, "Allocation successful allocation calls (process-wide)")
        hook_calls = _as_int(diagnostics, "Allocation hook calls (process-wide)")
        dropped = _as_int(diagnostics, "Allocation samples dropped")
        contention = _as_int(diagnostics, "Allocation lock contention records dropped")
        incomplete = _as_bool(diagnostics, "Allocation data incomplete")
        storage_exhausted = _as_bool(diagnostics, "Allocation profile storage exhausted")
        nonempty = (
            parsed["thread_count"] > 0
            and parsed["tree_node_count"] > 0
            and parsed["root_ref_count"] > 0
            and parsed["root_weight"] > 0.0
            and accepted > 0
            and sampled_bytes > 0
            and sampling_points > 0
        )
        if self.require_nonempty and not nonempty:
            raise RuntimeError(
                "required non-empty allocation tree is empty: "
                f"threads={parsed['thread_count']} nodes={parsed['tree_node_count']} roots={parsed['root_ref_count']} "
                f"weight={parsed['root_weight']} accepted={accepted} sampled_bytes={sampled_bytes} points={sampling_points}"
            )
        if incomplete or storage_exhausted:
            raise RuntimeError(f"allocation profile incomplete/storage exhausted: {incomplete}/{storage_exhausted}")

        profile = {
            "cycle": cycle,
            "profile_command": command,
            "started_wall_ms": started_wall_ms,
            "profile_url": url,
            "transport": transport,
            "sampler": parsed,
            "nonempty": nonempty,
            "hook_calls": hook_calls,
            "allocation_calls": allocation_calls,
            "observed_bytes": observed_bytes,
            "sampling_points": sampling_points,
            "accepted_samples": accepted,
            "sampled_bytes": sampled_bytes,
            "dropped_samples": dropped,
            "contention_dropped": contention,
            "incomplete": incomplete,
            "storage_exhausted": storage_exhausted,
        }
        self.result["profile_cycles"].append(profile)
        self._write_results()
        self.check(
            f"allocation-profile-cycle-{cycle}",
            "PASS",
            "raw SamplerData decoded and validated",
            profile_url=url,
            nonempty=nonempty,
            accepted_samples=accepted,
            sampled_bytes=sampled_bytes,
            observed_bytes=observed_bytes,
            sampling_points=sampling_points,
            threads=parsed["thread_count"],
            nodes=parsed["tree_node_count"],
            root_weight=parsed["root_weight"],
        )
        return profile

    def verify_persistent_resume(self, profile: dict[str, Any], cycle: int) -> None:
        assert self.server is not None
        profile_end_ms = int(profile["sampler"]["end_time_ms"])
        time.sleep(20)
        health_start = self.server.command("spark health upload")
        deadline = time.monotonic() + 75
        url: str | None = None
        while time.monotonic() < deadline:
            url = self._viewer_url(self.server.snapshot(), health_start)
            if url:
                break
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while checking post-export allocation-rate health data")
            time.sleep(1)
        if url is None:
            raise RuntimeError("post-export spark health upload produced no viewer URL")
        raw_path = self.root / f"post-export-health-cycle-{cycle}.sparkhealth"
        decoded, transport = fetch_payload(url, raw_path)
        health = parse_health_allocation_series(decoded)
        new_positive = [
            point
            for point in health["allocation_points"]
            if point["timestamp_ms"] >= profile_end_ms + 3000 and point["bytes_per_second"] > 0.0
        ]
        if not new_positive:
            tail = health["allocation_points"][-12:]
            raise RuntimeError(
                "no positive allocation-rate metric point was recorded after full profile export; "
                f"profile_end={profile_end_ms} tail={tail}"
            )
        evidence = {
            "cycle": cycle,
            "profile_end_ms": profile_end_ms,
            "health_url": url,
            "transport": transport,
            "generated_time_ms": health["generated_time_ms"],
            "post_export_positive_points": new_positive[-10:],
            "last_allocation_points": health["allocation_points"][-12:],
        }
        self.result["persistent_resume_evidence"].append(evidence)
        self._write_results()
        self.check(
            f"persistent-allocation-resumed-cycle-{cycle}",
            "PASS",
            "positive allocation-rate metrics were recorded after the full profile ended",
            profile_end_ms=profile_end_ms,
            newest_positive_point=new_positive[-1],
            health_url=url,
        )

    def execute(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            stage = "bds-bootstrap"
            self.bootstrap_offline_server()
            assert self.server is not None
            stage = "spark-sanity"
            self.run_basic_commands()
            stage = "world-behavior"
            self.verify_normal_world()
            stage = "fleet-connect"
            self.start_fleet()
            stage = "fleet-settle"
            time.sleep(20)
            output, _ = self.wait_player_count(self.count, timeout=10)
            self.check("fleet-stable-before-profile", "PASS", output=" | ".join(output[-30:]))

            for cycle in range(1, self.cycles + 1):
                stage = f"allocation-profile-{cycle}"
                profile = self.profile_allocation(cycle)
                stage = f"persistent-resume-{cycle}"
                self.verify_persistent_resume(profile, cycle)

            stage = "fleet-disconnect"
            self.stop_fleet()
            stage = "shutdown"
            self.shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self._write_results()
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:2000]
            diagnostic = traceback.format_exc()
            try:
                if self.bot is not None and self.bot.is_alive():
                    self.bot.force_close()
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:
                diagnostic += "\n\nCleanup failure:\n" + traceback.format_exc()
            last_lines = self.server.snapshot()[-400:] if self.server is not None else []
            self.diagnostics.write_text(
                diagnostic + "\n\nLast BDS log lines:\n" + "\n".join(last_lines), encoding="utf-8"
            )
            self._write_results()
            return 1
        finally:
            if self.bot is not None and self.bot.is_alive():
                self.bot.force_close()
            self.result["completed_at"] = now_iso()
            self.split_logs()
            self._write_results()
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--count", required=True, type=int, choices=[1, 5, 10, 20])
    parser.add_argument("--scenario", required=True, choices=["chunk-walk", "chunk-fly"])
    parser.add_argument("--profile-seconds", type=int, default=60)
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument("--cycles", type=int, default=1, choices=[1, 2])
    parser.add_argument("--require-nonempty", action="store_true")
    args = parser.parse_args()
    if args.profile_seconds < 60:
        raise SystemExit("allocation profiler validation requires at least 60 seconds")
    if args.interval <= 0:
        raise SystemExit("allocation interval must be positive")
    validator = AllocationProfilerValidation(
        args.platform,
        pathlib.Path(args.bot),
        args.count,
        args.scenario,
        args.profile_seconds,
        args.interval,
        args.cycles,
        args.require_nonempty,
    )
    return validator.execute()


if __name__ == "__main__":
    raise SystemExit(main())
