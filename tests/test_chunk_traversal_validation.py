from __future__ import annotations

import pathlib
import tempfile
import unittest

from controller.chunk_traversal_validation import (
    DETERMINISTIC_LEVEL_SEED,
    authoritative_failures,
    collect_publisher_evidence,
    configure_deterministic_world,
)


class ChunkTraversalEvidenceTest(unittest.TestCase):
    def test_configures_flat_world_before_real_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server_dir = pathlib.Path(temp)
            properties = server_dir / "server.properties"
            properties.write_text(
                "online-mode=true\nlevel-type=DEFAULT\nlevel-seed=\nmax-players=10\n",
                encoding="utf-8",
            )
            old_world = server_dir / "worlds" / "Bedrock level"
            old_world.mkdir(parents=True)
            (old_world / "level.dat").write_bytes(b"old-random-world")

            configured = configure_deterministic_world(server_dir)

            self.assertEqual(configured, properties)
            text = properties.read_text(encoding="utf-8")
            self.assertIn("online-mode=false", text)
            self.assertIn("gamemode=creative", text)
            self.assertIn("force-gamemode=true", text)
            self.assertIn("max-players=30", text)
            self.assertIn("level-type=FLAT", text)
            self.assertIn(f"level-seed={DETERMINISTIC_LEVEL_SEED}", text)
            self.assertFalse((server_dir / "worlds").exists())

    def test_collects_server_publisher_displacement(self) -> None:
        events = [
            {"event": "chunk_publisher", "bot": "TestBot", "x": 0, "y": 0, "z": 0},
            {"event": "chunk_publisher", "bot": "TestBot", "x": 10, "y": 100, "z": 20, "chunk_x": 0, "chunk_z": 1, "updates": 2},
            {"event": "chunk_publisher", "bot": "TestBot", "x": 40, "y": 100, "z": 60, "chunk_x": 2, "chunk_z": 3, "updates": 5},
        ]
        evidence = collect_publisher_evidence(events, ["TestBot"])
        self.assertEqual(evidence["moving_bots_ge_32"], 1)
        self.assertAlmostEqual(evidence["bots"]["TestBot"]["horizontal_distance"], 50.0)
        self.assertEqual(authoritative_failures(evidence, "chunk-fly", 1), [])

    def test_collects_authoritative_publisher_position_from_progress(self) -> None:
        events = [
            {
                "event": "bot_progress",
                "bot": "TestBot",
                "position": [900.0, 80.0, 900.0],
                "horizontal_distance": 900.0,
                "publisher_position": [149, 66, 561],
                "publisher_chunk": [9, 35],
                "publisher_updates": 2,
            },
            {
                "event": "bot_progress",
                "bot": "TestBot",
                "position": [1800.0, 80.0, 1800.0],
                "horizontal_distance": 1800.0,
                "publisher_position": [149, 60, 641],
                "publisher_chunk": [9, 40],
                "publisher_updates": 7,
            },
        ]
        evidence = collect_publisher_evidence(events, ["TestBot"])
        bot = evidence["bots"]["TestBot"]
        self.assertEqual(bot["samples"], 2)
        self.assertAlmostEqual(bot["horizontal_distance"], 80.0)
        self.assertEqual(bot["first"]["source"], "bot_progress.publisher_position")
        self.assertEqual(bot["last"]["source"], "bot_progress.publisher_position")
        self.assertEqual(authoritative_failures(evidence, "chunk-walk", 1), [])

    def test_chunk_fly_rejects_client_only_or_single_publisher_sample(self) -> None:
        events = [
            {"event": "bot_stats", "bot": "TestBot", "horizontal_distance": 900.0},
            {
                "event": "bot_progress",
                "bot": "TestBot",
                "position": [900.0, 80.0, 900.0],
                "horizontal_distance": 900.0,
            },
            {"event": "chunk_publisher", "bot": "TestBot", "x": 4, "y": 80, "z": 4},
        ]
        evidence = collect_publisher_evidence(events, ["TestBot"])
        self.assertEqual(authoritative_failures(evidence, "chunk-fly", 1), ["TestBot"])

    def test_chunk_walk_requires_a_meaningful_authoritative_comparator(self) -> None:
        names = [f"TestBot-{index:02d}" for index in range(1, 5)]
        events = []
        for index, name in enumerate(names):
            events.append({"event": "chunk_publisher", "bot": name, "x": 0, "y": 80, "z": 0})
            if index < 2:
                events.append({"event": "chunk_publisher", "bot": name, "x": 0, "y": 80, "z": 12 + index})
        evidence = collect_publisher_evidence(events, names)
        self.assertEqual(authoritative_failures(evidence, "chunk-walk", 4), [])

    def test_progress_requires_real_publisher_state(self) -> None:
        events = [
            {
                "event": "bot_progress",
                "bot": "TestBot",
                "position": [1000.0, 80.0, 1000.0],
                "horizontal_distance": 1000.0,
                "publisher_position": [0, 0, 0],
                "publisher_chunk": [0, 0],
                "publisher_updates": 0,
            }
        ]
        evidence = collect_publisher_evidence(events, ["TestBot"])
        self.assertEqual(evidence["bots"]["TestBot"]["samples"], 0)
        self.assertEqual(authoritative_failures(evidence, "chunk-fly", 1), ["TestBot"])


if __name__ == "__main__":
    unittest.main()
