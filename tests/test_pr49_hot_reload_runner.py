from __future__ import annotations

import unittest

from controller import pr49_hot_reload_runner as runner


class Pr49HotReloadRunnerTest(unittest.TestCase):
    def test_select_live_bds_identity_requires_one_verified_bds_process(self) -> None:
        records = [
            {"pid": 100, "name": "python.exe", "alive": True, "identity_match": True, "create_time": 10.0},
            {
                "pid": 200,
                "name": "bedrock_server.exe",
                "alive": True,
                "identity_match": True,
                "create_time": 20.0,
            },
        ]
        self.assertEqual(runner._select_live_bds_identity(records), (200, 20.0))
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            runner._select_live_bds_identity(records[:1])
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            runner._select_live_bds_identity(records + [dict(records[1], pid=201)])

    def test_reload_output_requires_completion_and_rejects_spark_load_failure(self) -> None:
        runner._validate_reload_output(["Reloading...", "Reload complete."])
        with self.assertRaisesRegex(RuntimeError, "did not report completion"):
            runner._validate_reload_output(["Reloading..."])
        with self.assertRaisesRegex(RuntimeError, "failed to reload"):
            runner._validate_reload_output(
                ["Failed to load c++ plugin from plugins/endstone_spark.dll", "Reload complete."]
            )
        with self.assertRaisesRegex(RuntimeError, "enable failure"):
            runner._validate_reload_output(
                ["An error occurred when enabling Spark v0.5.1", "Reload complete."]
            )

    def test_post_reload_profiler_info_requires_auto_resumed_background_profiler(self) -> None:
        runner._validate_post_reload_profiler_info(
            [
                "Profiler is already running!",
                "It was started automatically when spark enabled and has been running in the background for 1s.",
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "restore its background profiler"):
            runner._validate_post_reload_profiler_info(["The profiler isn't running."])


if __name__ == "__main__":
    unittest.main()
