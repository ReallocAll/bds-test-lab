from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from controller.bot_validation import BotProcess
from controller.cross_platform_fleet_validation import CrossPlatformFleetBotProcess
from controller.final_profiler_matrix import (
    ALLOCATION_DIAGNOSTICS,
    ALLOCATION_LIVE_DIAGNOSTICS,
    DEFAULT_ALLOCATION_INTERVAL_BYTES,
    EXECUTION_DIAGNOSTICS,
    ONLY_TICKS_OVER_MS,
    PROFILER_MODES,
    ProfileValidationError,
    build_profiler_command,
    read_verified_profile_payload,
    validate_profile_payload,
)
from controller.fleet_spark_validation import FleetBotProcess
from controller.run_test import ServerProcess, child_process_env
from controller.scenario_validation import ScenarioFileBotProcess


def varint(value: int) -> bytes:
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def field_varint(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def field_message(number: int, value: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(value)) + value


def field_string(number: int, value: str) -> bytes:
    return field_message(number, value.encode())


def packed_doubles(number: int, values: list[float]) -> bytes:
    import struct

    body = struct.pack("<" + "d" * len(values), *values)
    return field_message(number, body)


def packed_varints(number: int, values: list[int]) -> bytes:
    return field_message(number, b"".join(varint(value) for value in values))


def map_entry(key: str, value: str) -> bytes:
    return field_message(14, field_string(1, key) + field_string(2, value))


def diagnostic_values(*, allocation: bool, live_only: bool = False) -> dict[str, str]:
    values: dict[str, str] = {}
    keys = ALLOCATION_DIAGNOSTICS if allocation else EXECUTION_DIAGNOSTICS
    for key in keys:
        if key == "Allocation backend":
            values[key] = '"Linux glibc/ELF import slots"'
        elif key == "Allocation coverage":
            values[key] = '"synthetic fixture"'
        elif key == "Allocation thread filter stage":
            values[key] = '"aggregation"'
        elif key == "Allocation thread selection":
            values[key] = '"all"'
        elif key == "Allocation hook capabilities":
            values[key] = '[]'
        elif key in {
            "Execution profile storage exhausted",
            "Execution history truncated",
            "Execution data incomplete",
            "Allocation profile storage exhausted",
            "Allocation history truncated",
            "Allocation data incomplete",
            "Allocation live-only",
        }:
            values[key] = "true" if key == "Allocation live-only" and live_only else "false"
        elif "lifetime" in key or "retained average age" in key or "retained maximum age" in key:
            values[key] = "1.0"
        elif key == "Allocation interval bytes":
            values[key] = str(DEFAULT_ALLOCATION_INTERVAL_BYTES)
        else:
            values[key] = "1"
    if live_only:
        values.update({key: "1.0" for key in ALLOCATION_LIVE_DIAGNOSTICS if "age" in key})
        values["Allocation analysis"] = '"retained sampled allocations"'
    return values


def profile_fixture(
    mode: str,
    *,
    sampler_mode: int | None = None,
    interval: int | None = None,
    all_threads: bool | None = None,
    ticked: bool | None = None,
    threshold_us: int | None = None,
    included_ticks: int | None = None,
    live_only: bool | None = None,
    diagnostics: dict[str, str] | None = None,
    include_included_ticks: bool = True,
    include_thread: bool = True,
    thread_times: list[float] | None = None,
    node_times: list[float] | None = None,
    include_path: bool = True,
    start_ms: int = 1000,
    end_ms: int = 16000,
    number_of_ticks: int = 300,
) -> bytes:
    spec = next(item for item in PROFILER_MODES if item.name == mode)
    sampler_mode = spec.sampler_mode if sampler_mode is None else sampler_mode
    interval = spec.interval if interval is None else interval
    all_threads = spec.all_threads if all_threads is None else all_threads
    ticked = spec.ticked if ticked is None else ticked
    threshold_us = spec.tick_threshold_us if threshold_us is None else threshold_us
    included_ticks = (number_of_ticks // 2 if spec.ticked else 0) if included_ticks is None else included_ticks
    live_only = spec.live_only if live_only is None else live_only
    if diagnostics is None:
        diagnostics = diagnostic_values(allocation=spec.allocation, live_only=live_only)

    dumper = field_varint(1, 0 if all_threads else 1)
    aggregator = field_varint(1, 1 if ticked else 0) + field_varint(2, 0)
    if ticked:
        aggregator += field_varint(3, threshold_us)
        if include_included_ticks:
            aggregator += field_varint(4, included_ticks)
    metadata = (
        field_varint(2, start_ms)
        + field_varint(3, interval)
        + field_message(4, dumper)
        + field_message(5, aggregator)
        + field_varint(11, end_ms)
        + field_varint(12, number_of_ticks)
        + field_varint(15, sampler_mode)
        + field_varint(16, 1)
        + field_string(17, "endstone-spark test")
    )
    for key, value in diagnostics.items():
        metadata += map_entry(key, value)
    if node_times is None:
        node_times = [1.0]
    if thread_times is None:
        thread_times = [1.0]
    node = field_string(3, "native") + field_string(4, "tick") + packed_doubles(8, node_times)
    thread = field_string(1, "Server thread") + field_message(3, node)
    thread += packed_doubles(4, thread_times)
    if include_path:
        thread += packed_varints(5, [0])
    payload = field_message(1, metadata)
    if include_thread:
        payload += field_message(2, thread)
    return payload


class ProfilerCommandTest(unittest.TestCase):
    def test_exact_command_construction_for_all_six_modes(self) -> None:
        expected = {
            "default": "spark profiler start --timeout 15",
            "1ms": "spark profiler start --timeout 15 --interval 1",
            "all-thread": "spark profiler start --timeout 15 --thread *",
            "only-ticks-over": f"spark profiler start --timeout 15 --only-ticks-over {ONLY_TICKS_OVER_MS}",
            "allocation": "spark profiler start --timeout 15 --alloc",
            "alloc-live-only": "spark profiler start --timeout 15 --alloc-live-only",
        }
        self.assertEqual(set(expected), {spec.name for spec in PROFILER_MODES})
        for mode, command in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual(build_profiler_command(mode), command)
        self.assertNotIn("--interval", build_profiler_command("default"))
        self.assertNotIn("--alloc", build_profiler_command("default"))


class ProfilePayloadValidationTest(unittest.TestCase):
    def test_empty_raw_payload_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProfileValidationError, "invalid Spark profile payload"):
            validate_profile_payload(b"", "default")

    def test_all_six_fixtures_pass(self) -> None:
        for spec in PROFILER_MODES:
            with self.subTest(mode=spec.name):
                result = validate_profile_payload(profile_fixture(spec.name), spec.name, expected_duration_seconds=15)
                self.assertTrue(all(result["assertions"].values()))

    def test_mode_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProfileValidationError, "sampler mode mismatch"):
            validate_profile_payload(profile_fixture("default", sampler_mode=1), "default")

    def test_interval_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProfileValidationError, "interval mismatch"):
            validate_profile_payload(profile_fixture("1ms", interval=4000), "1ms")

    def test_all_thread_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProfileValidationError, "thread dumper mismatch|all_threads mismatch"):
            validate_profile_payload(profile_fixture("all-thread", all_threads=False), "all-thread")

    def test_only_ticks_threshold_and_zero_included_ticks_are_qualified(self) -> None:
        with self.assertRaisesRegex(ProfileValidationError, "tick threshold mismatch"):
            validate_profile_payload(
                profile_fixture("only-ticks-over", threshold_us=ONLY_TICKS_OVER_MS * 1000 + 1000),
                "only-ticks-over",
            )
        omitted_zero = validate_profile_payload(
            profile_fixture(
                "only-ticks-over", included_ticks=0, include_included_ticks=False, include_thread=False
            ),
            "only-ticks-over",
        )
        explicit_empty = validate_profile_payload(
            profile_fixture("only-ticks-over", included_ticks=0, include_thread=False), "only-ticks-over"
        )
        for result in (explicit_empty, omitted_zero):
            self.assertEqual(result["observed"]["number_of_included_ticks"], 0)
            self.assertTrue(result["assertions"]["tick_metadata"])
            self.assertTrue(result["shape"]["zero_included_ticks"])

        positive_samples = (
            ("sample", profile_fixture("only-ticks-over", included_ticks=0)),
            (
                "root",
                profile_fixture("only-ticks-over", included_ticks=0, node_times=[], thread_times=[1.0]),
            ),
            (
                "path",
                profile_fixture("only-ticks-over", included_ticks=0, node_times=[], thread_times=[], include_path=True),
            ),
        )
        for label, payload in positive_samples:
            with (
                self.subTest(positive=label),
                self.assertRaisesRegex(ProfileValidationError, "zero-inclusion profile contains positive"),
            ):
                validate_profile_payload(payload, "only-ticks-over")

    def test_live_only_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(ProfileValidationError, "allocation live-only mismatch"):
            validate_profile_payload(profile_fixture("alloc-live-only", live_only=False), "alloc-live-only")

    def test_missing_diagnostics_and_incomplete_flag_fail_closed(self) -> None:
        missing = diagnostic_values(allocation=False)
        missing.pop("Execution sample queue capacity")
        with self.assertRaisesRegex(ProfileValidationError, "missing required diagnostics"):
            validate_profile_payload(profile_fixture("default", diagnostics=missing), "default")

        incomplete = diagnostic_values(allocation=False)
        incomplete["Execution data incomplete"] = "true"
        with self.assertRaisesRegex(ProfileValidationError, "Execution data is incomplete"):
            validate_profile_payload(profile_fixture("default", diagnostics=incomplete), "default")

    def test_empty_and_tampered_profile_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            empty = root / "empty.sparkprofile"
            empty.write_bytes(b"")
            with self.assertRaisesRegex(ProfileValidationError, "empty"):
                read_verified_profile_payload(empty, root=root)

            payload = profile_fixture("default")
            profile = root / "default.sparkprofile"
            profile.write_bytes(payload)
            with self.assertRaisesRegex(ProfileValidationError, "SHA-256 mismatch"):
                read_verified_profile_payload(
                    profile,
                    expected_sha256=hashlib.sha256(b"tampered").hexdigest(),
                    root=root,
                )


class WorkflowSecurityContractTest(unittest.TestCase):
    workflow_path = Path(__file__).parents[1] / ".github" / "workflows" / "final-profiler-matrix.yml"

    def test_bot_sha_github_env_line_contains_only_machine_assignment(self) -> None:
        workflow = self.workflow_path.read_text(encoding="utf-8")
        self.assertIn('echo "BOT_SHA=$actual_bot_sha" >> "$GITHUB_ENV"', workflow)
        self.assertIn('echo "Bot SHA: $actual_bot_sha"', workflow)
        self.assertNotIn('echo "Bot SHA: $actual_bot_sha" | tee -a "$GITHUB_ENV"', workflow)

    def test_github_api_token_is_step_scoped(self) -> None:
        workflow = self.workflow_path.read_text(encoding="utf-8")
        self.assertNotIn("\nenv:\n  GH_TOKEN: ${{ secrets.REPO_PAT }}", workflow)
        self.assertEqual(workflow.count("GH_TOKEN: ${{ secrets.REPO_PAT }}"), 2)
        self.assertNotIn("\n  REPO_PAT:", workflow)


class ChildEnvironmentSecurityTest(unittest.TestCase):
    def test_child_environment_helper_removes_credentials_case_insensitively(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "token", "REPO_PAT": "pat", "gh_token": "lower"}):
            environment = child_process_env()
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("REPO_PAT", environment)
        self.assertNotIn("gh_token", environment)

    def test_server_and_bot_processes_receive_no_credentials(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.pid = os.getpid()
                self.stdin = io.StringIO()
                self.stdout = io.StringIO()

            def poll(self) -> int:
                return 0

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.dict(os.environ, {"GH_TOKEN": "token", "REPO_PAT": "pat"}):
                server_process = FakeProcess()
                with mock.patch("controller.run_test.subprocess.Popen", return_value=server_process) as server_popen:
                    server = ServerProcess(["bds"], root, root / "server.log")
                    server.start()
                    server.close()

                bot_process = FakeProcess()
                with mock.patch("controller.bot_validation.subprocess.Popen", return_value=bot_process) as bot_popen:
                    bot = BotProcess(Path(sys.executable), root / "bot.log")
                    bot.start()
                    bot.force_close()

            server_environment = server_popen.call_args.kwargs["env"]
            bot_environment = bot_popen.call_args.kwargs["env"]
            for environment in (server_environment, bot_environment):
                self.assertNotIn("GH_TOKEN", environment)
                self.assertNotIn("REPO_PAT", environment)

    def test_fleet_bot_process_receives_scrubbed_environment(self) -> None:
        self._assert_bot_environment(FleetBotProcess, "fleet")

    def test_cross_platform_fleet_bot_process_receives_scrubbed_environment(self) -> None:
        self._assert_bot_environment(CrossPlatformFleetBotProcess, "cross-platform-fleet")

    def test_scenario_file_bot_process_receives_scrubbed_environment(self) -> None:
        self._assert_bot_environment(ScenarioFileBotProcess, "scenario-file")

    def _assert_bot_environment(self, process_type: type[BotProcess], label: str) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.pid = os.getpid()
                self.stdin = io.StringIO()
                self.stdout = io.StringIO()

            def poll(self) -> int:
                return 0

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.dict(
                os.environ,
                {
                    "GH_TOKEN": "token",
                    "gh_token": "lower-token",
                    "REPO_PAT": "pat",
                    "repo_pat": "lower-pat",
                    "BDS_TEST_BOT_SCENARIO_FILE": "",
                    "SPARK_TEST_KEEP": "keep-me",
                },
            ):
                fake_process = FakeProcess()
                with mock.patch(
                    f"controller.{process_type.__module__.split('.', 1)[-1]}.subprocess.Popen",
                    return_value=fake_process,
                ) as popen:
                    if process_type is FleetBotProcess or process_type is CrossPlatformFleetBotProcess:
                        bot = process_type(Path(sys.executable), root / f"{label}.log", 1, "idle")
                    else:
                        bot = process_type(Path(sys.executable), root / f"{label}.log", 1, root / "scenario.json")
                    bot.start()
                    bot.force_close()

            environment = popen.call_args.kwargs["env"]
            self.assertNotIn("GH_TOKEN", environment, label)
            self.assertNotIn("gh_token", environment, label)
            self.assertNotIn("REPO_PAT", environment, label)
            self.assertNotIn("repo_pat", environment, label)
            self.assertEqual(environment["SPARK_TEST_KEEP"], "keep-me", label)


if __name__ == "__main__":
    unittest.main()
