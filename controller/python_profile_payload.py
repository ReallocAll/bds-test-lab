from __future__ import annotations

import gzip
import json
import struct
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class Node:
    class_name: str = ""
    method_name: str = ""
    line_number: int = -1
    method_desc: str = ""
    times: list[float] = field(default_factory=list)
    children_refs: list[int] = field(default_factory=list)

    @property
    def weight(self) -> float:
        return float(sum(self.times))


@dataclass
class ThreadTree:
    name: str = ""
    nodes: list[Node] = field(default_factory=list)
    times: list[float] = field(default_factory=list)
    children_refs: list[int] = field(default_factory=list)

    @property
    def weight(self) -> float:
        return float(sum(self.times))


@dataclass
class ProfilePayload:
    start_time_ms: int = 0
    end_time_ms: int = 0
    sampler_mode: int = -1
    interval: int = 0
    extra_metadata: dict[str, str] = field(default_factory=dict)
    class_sources: dict[str, str] = field(default_factory=dict)
    threads: list[ThreadTree] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end_time_ms - self.start_time_ms) / 1000.0)


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data) or shift >= 70:
            raise ValueError("invalid protobuf varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7


def _fields(data: bytes):
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        field_number = key >> 3
        wire = key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
            yield field_number, wire, value
        elif wire == 1:
            if pos + 8 > len(data):
                raise ValueError("truncated fixed64")
            value = data[pos : pos + 8]
            pos += 8
            yield field_number, wire, value
        elif wire == 2:
            size, pos = _varint(data, pos)
            end = pos + size
            if end > len(data):
                raise ValueError("truncated length-delimited field")
            value = data[pos:end]
            pos = end
            yield field_number, wire, value
        elif wire == 5:
            if pos + 4 > len(data):
                raise ValueError("truncated fixed32")
            value = data[pos : pos + 4]
            pos += 4
            yield field_number, wire, value
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _text(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def _packed_varints(value: bytes) -> list[int]:
    result: list[int] = []
    pos = 0
    while pos < len(value):
        item, pos = _varint(value, pos)
        result.append(item)
    return result


def _packed_doubles(value: bytes) -> list[float]:
    if len(value) % 8:
        raise ValueError("invalid packed double field")
    return list(struct.unpack("<" + "d" * (len(value) // 8), value))


def _map_entry(value: bytes) -> tuple[str, str]:
    key = ""
    val = ""
    for number, wire, item in _fields(value):
        if number == 1 and wire == 2:
            key = _text(item)
        elif number == 2 and wire == 2:
            val = _text(item)
    return key, val


def _parse_metadata(value: bytes, profile: ProfilePayload) -> None:
    for number, wire, item in _fields(value):
        if number == 2 and wire == 0:
            profile.start_time_ms = int(item)
        elif number == 3 and wire == 0:
            profile.interval = int(item)
        elif number == 11 and wire == 0:
            profile.end_time_ms = int(item)
        elif number == 14 and wire == 2:
            key, val = _map_entry(item)
            if key:
                profile.extra_metadata[key] = val
        elif number == 15 and wire == 0:
            profile.sampler_mode = int(item)


def _parse_node(value: bytes) -> Node:
    node = Node()
    for number, wire, item in _fields(value):
        if number == 3 and wire == 2:
            node.class_name = _text(item)
        elif number == 4 and wire == 2:
            node.method_name = _text(item)
        elif number == 6 and wire == 0:
            node.line_number = int(item)
        elif number == 7 and wire == 2:
            node.method_desc = _text(item)
        elif number == 8 and wire == 2:
            node.times.extend(_packed_doubles(item))
        elif number == 9 and wire == 2:
            node.children_refs.extend(_packed_varints(item))
    return node


def _parse_thread(value: bytes) -> ThreadTree:
    thread = ThreadTree()
    for number, wire, item in _fields(value):
        if number == 1 and wire == 2:
            thread.name = _text(item)
        elif number == 3 and wire == 2:
            thread.nodes.append(_parse_node(item))
        elif number == 4 and wire == 2:
            thread.times.extend(_packed_doubles(item))
        elif number == 5 and wire == 2:
            thread.children_refs.extend(_packed_varints(item))
    return thread


def parse_sampler_data(data: bytes) -> ProfilePayload:
    profile = ProfilePayload()
    if not data:
        raise ValueError("empty Spark profile payload")
    for number, wire, item in _fields(data):
        if number == 1 and wire == 2:
            _parse_metadata(item, profile)
        elif number == 2 and wire == 2:
            profile.threads.append(_parse_thread(item))
        elif number == 3 and wire == 2:
            key, val = _map_entry(item)
            if key:
                profile.class_sources[key] = val
    return profile


def fetch_viewer_payload(viewer_url: str, timeout: float = 45.0) -> bytes:
    parsed = urlparse(viewer_url)
    key = parsed.path.strip("/").split("/", 1)[0]
    if not key:
        raise ValueError(f"viewer URL has no profile key: {viewer_url}")
    request = urllib.request.Request(
        f"https://spark-usercontent.lucko.me/{key}",
        headers={"User-Agent": "bds-test-lab/python-attribution-validation"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        content_encoding = (response.headers.get("Content-Encoding") or "").lower()
    if content_encoding == "gzip" or data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    if not data:
        raise ValueError("bytebin returned an empty profile payload")
    return data


def _python_projection(profile: ProfilePayload, thread: ThreadTree) -> list[list[str]]:
    paths: list[list[str]] = []

    def visit(index: int, current: list[str], active: set[int]) -> None:
        if index < 0 or index >= len(thread.nodes) or index in active:
            return
        node = thread.nodes[index]
        projected = current
        if node.class_name.startswith("[Python] "):
            projected = current + [node.method_name]
            paths.append(projected)
        next_active = active | {index}
        for child in node.children_refs:
            visit(child, projected, next_active)

    for root in thread.children_refs:
        visit(root, [], set())
    return paths


def contains_python_chain(profile: ProfilePayload, expected: list[str]) -> bool:
    for thread in profile.threads:
        for path in _python_projection(profile, thread):
            cursor = 0
            for method in path:
                if cursor < len(expected) and method == expected[cursor]:
                    cursor += 1
            if cursor == len(expected):
                return True
    return False


def python_nodes(profile: ProfilePayload) -> list[tuple[str, Node]]:
    result: list[tuple[str, Node]] = []
    for thread in profile.threads:
        for node in thread.nodes:
            if node.class_name.startswith("[Python] "):
                result.append((thread.name, node))
    return result


def profile_summary(profile: ProfilePayload) -> dict[str, object]:
    python = python_nodes(profile)
    methods: dict[str, float] = {}
    for _thread, node in python:
        methods[node.method_name] = methods.get(node.method_name, 0.0) + node.weight
    return {
        "duration_seconds": profile.duration_seconds,
        "sampler_mode": profile.sampler_mode,
        "interval": profile.interval,
        "thread_count": len(profile.threads),
        "root_weight": sum(thread.weight for thread in profile.threads),
        "python_node_count": len(python),
        "python_methods_ms": dict(sorted(methods.items(), key=lambda item: item[1], reverse=True)),
        "python_threads": sorted({thread for thread, _node in python}),
        "class_sources": profile.class_sources,
        "python_diagnostics": {
            key: value for key, value in profile.extra_metadata.items() if key.startswith("Python ")
        },
    }


def write_profile_summary(path, profile: ProfilePayload) -> None:
    path.write_text(json.dumps(profile_summary(profile), indent=2, sort_keys=True), encoding="utf-8")
