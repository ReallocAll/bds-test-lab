from __future__ import annotations

import gzip
import tempfile
import unittest
import urllib.request
from pathlib import Path

from controller.block_actor_validation import BytebinCapture, decode_world_info_samples


def varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def field_message(number: int, body: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(body)) + body


def health_payload(samples: list[tuple[bool, int]]) -> bytes:
    encoded_values = bytearray()
    for present, tile_entities in samples:
        value = field_varint(1, 0) + field_varint(2, 3) + field_varint(4, 5)
        if present:
            value += field_varint(3, tile_entities)
        encoded_values += field_message(3, value)
    world_info = bytes(encoded_values)
    metrics = field_message(8, world_info)
    metadata = field_message(9, metrics)
    return field_message(1, metadata)


class BlockActorProtoTest(unittest.TestCase):
    def test_decode_preserves_explicit_zero_presence(self) -> None:
        samples = decode_world_info_samples(health_payload([(False, 0), (True, 0), (True, 7)]))
        self.assertEqual(
            samples,
            [
                {
                    "players": 0,
                    "entities": 3,
                    "tile_entities": 0,
                    "tile_entities_present": False,
                    "chunks": 5,
                },
                {
                    "players": 0,
                    "entities": 3,
                    "tile_entities": 0,
                    "tile_entities_present": True,
                    "chunks": 5,
                },
                {
                    "players": 0,
                    "entities": 3,
                    "tile_entities": 7,
                    "tile_entities_present": True,
                    "chunks": 5,
                },
            ],
        )

    def test_local_bytebin_capture_decompresses_health_body(self) -> None:
        expected = health_payload([(True, 2)])
        with tempfile.TemporaryDirectory() as tmp:
            capture = BytebinCapture(Path(tmp))
            capture.start()
            try:
                request = urllib.request.Request(
                    capture.base_url + "/post",
                    data=gzip.compress(expected),
                    method="POST",
                    headers={"Content-Encoding": "gzip", "Content-Type": "application/x-spark-health"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 201)
                    self.assertEqual(response.headers.get("Location"), "/capture/1")
                self.assertEqual(capture.latest(), expected)
                self.assertTrue((Path(tmp) / "health-001.bin").is_file())
            finally:
                capture.stop()


if __name__ == "__main__":
    unittest.main()
