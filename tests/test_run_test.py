from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from controller.run_test import IntegrationTest


class _FakeServer:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, command: str) -> int:
        self.commands.append(command)
        return 0

    def snapshot(self) -> list[str]:
        return ["Profile complete: https://spark.lucko.me/windows-allocation-test"]

    def is_alive(self) -> bool:
        return True


class WindowsAllocationValidationTest(unittest.TestCase):
    def test_windows_allocation_requires_normal_profiler_success_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = IntegrationTest.__new__(IntegrationTest)
            fixture.platform = "windows"
            fixture.server = _FakeServer()
            fixture.result = {}
            fixture.result_path = Path(tmp) / "test-results.json"
            checks: list[tuple[str, str, dict[str, object]]] = []
            fixture.check = lambda name, status, detail=None, **extra: checks.append(
                (name, status, {"detail": detail, **extra})
            )

            url = fixture.run_profiler(allocation=True)

        self.assertEqual(url, "https://spark.lucko.me/windows-allocation-test")
        self.assertEqual(fixture.server.commands, ["spark profiler start --timeout 12 --alloc"])
        self.assertEqual(fixture.result["allocation_profile_viewer_url"], url)
        self.assertEqual(checks[-1][0:2], ("allocation-profiler", "PASS"))
        self.assertEqual(checks[-1][2]["viewer_url"], url)
        self.assertNotIn("expected_disabled", checks[-1][2])


if __name__ == "__main__":
    unittest.main()
