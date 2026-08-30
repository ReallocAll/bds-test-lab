from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from controller.python_performance_trigger import (
    TriggerConfigurationError,
    parse_trigger_file,
    resolve_trigger,
)


class PythonPerformanceTriggerTest(unittest.TestCase):
    def test_push_reads_exact_trigger_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trigger.txt"
            path.write_text(f"spark_sha={'a' * 40}\nduration_seconds=180\n", encoding="utf-8")
            resolved = resolve_trigger(
                event_name="push",
                trigger_path=path,
                requested_spark_sha="b" * 40,
                requested_duration_seconds="900",
            )
        self.assertEqual(resolved.spark_sha, "a" * 40)
        self.assertEqual(resolved.duration_seconds, 180)

    def test_dispatch_uses_requested_values(self) -> None:
        resolved = resolve_trigger(
            event_name="workflow_dispatch",
            trigger_path=Path("unused"),
            requested_spark_sha="c" * 40,
            requested_duration_seconds="240",
        )
        self.assertEqual(resolved.spark_sha, "c" * 40)
        self.assertEqual(resolved.duration_seconds, 240)

    def test_trigger_rejects_duplicate_unknown_missing_and_malformed_lines(self) -> None:
        cases = (
            (f"spark_sha={'a' * 40}\nspark_sha={'a' * 40}\nduration_seconds=180\n", "duplicates key"),
            (f"spark_sha={'a' * 40}\nduration_seconds=180\nother=x\n", "unknown key"),
            (f"spark_sha={'a' * 40}\n", "missing keys"),
            (f"spark_sha={'a' * 40}\nduration_seconds\n", "not key=value"),
        )
        for content, pattern in cases:
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "trigger.txt"
                path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(TriggerConfigurationError, pattern):
                    parse_trigger_file(path)

    def test_trigger_rejects_non_exact_sha_and_out_of_range_duration(self) -> None:
        cases = (
            ("ABC", "180", "40-character SHA"),
            ("a" * 40, "179", "180..900"),
            ("a" * 40, "901", "180..900"),
            ("a" * 40, "abc", "must be an integer"),
        )
        for sha, duration, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(TriggerConfigurationError, pattern):
                resolve_trigger(
                    event_name="workflow_dispatch",
                    trigger_path=Path("unused"),
                    requested_spark_sha=sha,
                    requested_duration_seconds=duration,
                )

    def test_trigger_rejects_unsupported_event(self) -> None:
        with self.assertRaisesRegex(TriggerConfigurationError, "unsupported GitHub event"):
            resolve_trigger(
                event_name="schedule",
                trigger_path=Path("unused"),
                requested_spark_sha="a" * 40,
                requested_duration_seconds="180",
            )


if __name__ == "__main__":
    unittest.main()
