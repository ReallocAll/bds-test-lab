from __future__ import annotations

import pathlib
import signal
import sys
import unittest
from unittest import mock

from controller.cross_platform_fleet_validation import CrossPlatformFleetBotProcess
from controller.fleet_spark_validation import FleetBotProcess


class CrossPlatformFleetSignalExitTests(unittest.TestCase):
    def make_bot(self, *, count: int = 5) -> CrossPlatformFleetBotProcess:
        return CrossPlatformFleetBotProcess(
            pathlib.Path("fake-bot"),
            pathlib.Path("fake-bot.log"),
            count,
            "candidate-a-stationary",
        )

    @staticmethod
    def complete_events(count: int = 5) -> list[dict[str, object]]:
        stats = [
            {
                "event": "bot_stats",
                "index": index,
                "online": True,
                "auth_inputs_sent": 100 + index,
            }
            for index in range(1, count + 1)
        ]
        return [
            *stats,
            {
                "event": "fleet_shutdown",
                "graceful_shutdown": True,
                "reason": "signal",
                "launched": count,
                "online": count,
            },
        ]

    @unittest.skipUnless(sys.platform == "linux", "Linux SIGTERM semantics only")
    def test_complete_graceful_sigterm_is_normalized(self) -> None:
        bot = self.make_bot()
        bot.events = self.complete_events()
        with mock.patch.object(FleetBotProcess, "terminate", return_value=-int(signal.SIGTERM)):
            self.assertEqual(bot.terminate(20), 0)

    @unittest.skipUnless(sys.platform == "linux", "Linux SIGTERM semantics only")
    def test_sigterm_without_all_bot_stats_is_not_normalized(self) -> None:
        bot = self.make_bot()
        bot.events = self.complete_events()[:-2] + [self.complete_events()[-1]]
        with mock.patch.object(FleetBotProcess, "terminate", return_value=-int(signal.SIGTERM)):
            self.assertEqual(bot.terminate(20), -int(signal.SIGTERM))

    @unittest.skipUnless(sys.platform == "linux", "Linux SIGTERM semantics only")
    def test_sigterm_with_shutdown_error_is_not_normalized(self) -> None:
        bot = self.make_bot()
        events = self.complete_events()
        shutdown = dict(events[-1])
        shutdown["error"] = "instance failed"
        bot.events = [*events[:-1], shutdown]
        with mock.patch.object(FleetBotProcess, "terminate", return_value=-int(signal.SIGTERM)):
            self.assertEqual(bot.terminate(20), -int(signal.SIGTERM))

    @unittest.skipUnless(sys.platform == "linux", "Linux SIGTERM semantics only")
    def test_sigterm_with_duplicate_index_is_not_normalized(self) -> None:
        bot = self.make_bot()
        events = self.complete_events()
        duplicate = dict(events[1])
        duplicate["index"] = 1
        bot.events = [events[0], duplicate, *events[2:]]
        with mock.patch.object(FleetBotProcess, "terminate", return_value=-int(signal.SIGTERM)):
            self.assertEqual(bot.terminate(20), -int(signal.SIGTERM))

    def test_unrelated_nonzero_exit_is_preserved(self) -> None:
        bot = self.make_bot()
        bot.events = self.complete_events()
        with mock.patch.object(FleetBotProcess, "terminate", return_value=2):
            self.assertEqual(bot.terminate(20), 2)

    def test_zero_exit_is_preserved(self) -> None:
        bot = self.make_bot()
        with mock.patch.object(FleetBotProcess, "terminate", return_value=0):
            self.assertEqual(bot.terminate(20), 0)


if __name__ == "__main__":
    unittest.main()
