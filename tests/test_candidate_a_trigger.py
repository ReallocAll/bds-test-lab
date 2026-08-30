from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from controller.candidate_a_trigger import (
    TriggerConfigurationError,
    normalize_trigger,
    parse_trigger_file,
    resolve_trigger,
    write_github_output,
)


class CandidateATriggerTest(unittest.TestCase):
    def test_push_trigger_resolves_first_look(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trigger = Path(temp) / "trigger.txt"
            trigger.write_text(
                "Candidate A controlled benchmark batch 1\n"
                "start_block=1\n"
                "batch_size=4\n"
                "baseline_sha=ignored-by-trigger-resolver\n",
                encoding="utf-8",
            )
            values = resolve_trigger(
                event_name="push",
                trigger_file=trigger,
                input_start_block="",
                input_batch_size="",
                input_prior_run_ids="",
            )
        self.assertEqual(values, {"start_block": "1", "batch_size": "4", "prior_run_ids": ""})

    def test_push_trigger_resolves_cumulative_second_look(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trigger = Path(temp) / "trigger.txt"
            trigger.write_text(
                "start_block=5\n"
                "batch_size=4\n"
                "prior_run_ids=33300978767\n",
                encoding="utf-8",
            )
            values = resolve_trigger(
                event_name="push",
                trigger_file=trigger,
                input_start_block="",
                input_batch_size="",
                input_prior_run_ids="",
            )
        self.assertEqual(values["start_block"], "5")
        self.assertEqual(values["prior_run_ids"], "33300978767")

    def test_dispatch_uses_inputs(self) -> None:
        values = resolve_trigger(
            event_name="workflow_dispatch",
            trigger_file=Path("unused"),
            input_start_block="9",
            input_batch_size="4",
            input_prior_run_ids="100,200",
        )
        self.assertEqual(values["start_block"], "9")
        self.assertEqual(values["prior_run_ids"], "100,200")

    def test_each_look_requires_exact_prior_batch_count(self) -> None:
        for start, prior in ((1, ""), (5, "1"), (9, "1,2"), (13, "1,2,3"), (17, "1,2,3,4")):
            with self.subTest(start=start):
                values = normalize_trigger(start_block=str(start), batch_size="4", prior_run_ids=prior)
                self.assertEqual(values["start_block"], str(start))
        with self.assertRaisesRegex(TriggerConfigurationError, "requires exactly 1 prior run IDs"):
            normalize_trigger(start_block="5", batch_size="4", prior_run_ids="")
        with self.assertRaisesRegex(TriggerConfigurationError, "requires exactly 2 prior run IDs"):
            normalize_trigger(start_block="9", batch_size="4", prior_run_ids="1")

    def test_rejects_duplicate_or_invalid_prior_run_ids(self) -> None:
        with self.assertRaisesRegex(TriggerConfigurationError, "unique"):
            normalize_trigger(start_block="9", batch_size="4", prior_run_ids="1,1")
        with self.assertRaisesRegex(TriggerConfigurationError, "invalid prior run ID"):
            normalize_trigger(start_block="5", batch_size="4", prior_run_ids="abc")

    def test_rejects_illegal_start_and_batch(self) -> None:
        with self.assertRaisesRegex(TriggerConfigurationError, "start_block"):
            normalize_trigger(start_block="2", batch_size="4", prior_run_ids="")
        with self.assertRaisesRegex(TriggerConfigurationError, "batch_size"):
            normalize_trigger(start_block="1", batch_size="8", prior_run_ids="")

    def test_duplicate_trigger_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trigger = Path(temp) / "trigger.txt"
            trigger.write_text("start_block=1\nstart_block=5\n", encoding="utf-8")
            with self.assertRaisesRegex(TriggerConfigurationError, "duplicate trigger key"):
                parse_trigger_file(trigger)

    def test_github_output_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output.txt"
            write_github_output(
                output,
                {"start_block": "5", "batch_size": "4", "prior_run_ids": "123"},
            )
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "start_block=5\nbatch_size=4\nprior_run_ids=123\n",
            )


if __name__ == "__main__":
    unittest.main()
