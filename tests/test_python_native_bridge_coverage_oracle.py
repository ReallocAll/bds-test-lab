from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from controller.python_native_bridge_coverage_oracle import (
    CANDIDATE_SHA,
    COUNTER_ALIGNMENT_METHOD,
    COUNTER_CLOCK,
    COUNTER_CLOCK_SOURCE,
    COUNTER_SCOPE,
    EXPECTED_FIXED,
    EXPECTED_MODULE,
    EXPECTED_NESTED,
    EXPECTED_SOURCE,
    PARENT_SHA,
    START_ACK_TOKEN,
    STOP_COMPLETE_ACK_TOKEN,
    STOP_REQUEST_ACK_TOKEN,
    _read_case,
    align_workload_counters,
    analyze_paired_evidence,
    assess_noninferiority,
    parse_case_status,
    validate_profile,
    validate_profile_boundaries,
    validate_workload,
)
from controller.python_profile_payload import Node, ProfilePayload, ThreadTree


def valid_profile(*, include_chain: bool = True, nested_weight: float = 40.0, callback_failures: int = 0) -> ProfilePayload:
    nested = Node(
        class_name=f"[Python] {EXPECTED_MODULE}",
        method_name=EXPECTED_NESTED,
        line_number=12,
        times=[nested_weight],
    )
    fixed = Node(
        class_name=f"[Python] {EXPECTED_MODULE}",
        method_name=EXPECTED_FIXED,
        line_number=8,
        times=[60.0],
        children_refs=[1] if include_chain else [],
    )
    native = Node(method_name="bedrock_server!tick", times=[40.0])
    diagnostics = {
        "Python attribution backend": '"PEP669"',
        "Python function attribution enabled": "true",
        "Python version": '"3.13.5"',
        "Python PY_START events": "12000",
        "Python shadow snapshot attempts": "15000",
        "Python shadow snapshot failures": "0",
        "Python shadow overflows": "0",
        "Python unknown code IDs": "0",
        "Python native boundary misses": "0",
        "Python thread mismatches": "0",
        "Python monitoring callback failures": str(callback_failures),
        "Python attributed samples": "80",
        "Python native-only samples": "20",
    }
    return ProfilePayload(
        start_time_ms=0,
        end_time_ms=60_000,
        sampler_mode=0,
        interval=4000,
        extra_metadata=diagnostics,
        class_sources={f"[Python] {EXPECTED_MODULE}": EXPECTED_SOURCE},
        threads=[
            ThreadTree(name="Server thread", nodes=[fixed, nested, native], times=[100.0], children_refs=[0, 2])
        ],
    )


def valid_workload() -> dict[str, object]:
    first_start = 1_000_000_000
    records = [
        {
            "start_ns": first_start + index * 50_000_000,
            "end_ns": first_start + index * 50_000_000 + 20_000_000,
            "elapsed_ns": 20_000_000,
            "nested_call_count": 1_000,
        }
        for index in range(1_200)
    ]
    profile_start = records[0]["start_ns"]
    profile_end = records[-1]["end_ns"]
    return {
        "module": EXPECTED_MODULE,
        "tick_method": EXPECTED_FIXED,
        "nested_method": EXPECTED_NESTED,
        "window_ns": 20_000_000,
        "invocation_count": len(records),
        "nested_call_count": 1_200_000,
        "elapsed_ns_total": 24_000_000_000,
        "active_seconds": 59.97,
        "invocation_rate_hz": len(records) / 59.97,
        "elapsed_ns": {"count": len(records), "min": 20_000_000, "max": 20_000_000, "mean": 20_000_000, "p50": 20_000_000, "p95": 20_000_000},
        "window_intervals_ns": [50_000_000] * (len(records) - 1),
        "clock": {"name": COUNTER_CLOCK, "source": "time.perf_counter_ns", "unit": "ns"},
        "counter_scope": COUNTER_SCOPE,
        "counter_alignment": {
            "method": COUNTER_ALIGNMENT_METHOD,
            "clock": COUNTER_CLOCK,
            "profile_start_ns": profile_start,
            "profile_end_ns": profile_end,
            "source_window_count": len(records),
            "included_window_count": len(records),
            "excluded_window_count": 0,
            "first_included_start_ns": records[0]["start_ns"],
            "last_included_end_ns": records[-1]["end_ns"],
            "strict": True,
        },
        "window_records": records,
    }


def valid_boundaries() -> dict[str, object]:
    return {
        "clock": COUNTER_CLOCK,
        "clock_source": COUNTER_CLOCK_SOURCE,
        "profile_start_ns": 120,
        "profile_end_ns": 500,
        "start_command_sent_ns": 100,
        "start_ack_observed_ns": 110,
        "stop_command_sent_ns": 510,
        "stop_request_ack_observed_ns": 520,
        "stop_complete_ack_observed_ns": 530,
        "stop_command_flushed_ns": 540,
        "start_acknowledgement": START_ACK_TOKEN,
        "start_ack_line": "[Spark] Profiler is now running!",
        "stop_request_acknowledgement": STOP_REQUEST_ACK_TOKEN,
        "stop_request_ack_line": "Stopping the profiler and finalizing results, please wait...",
        "stop_complete_acknowledgement": STOP_COMPLETE_ACK_TOKEN,
        "stop_complete_ack_line": "Profiler stopped.",
        "strict": True,
    }


class PythonNativeBridgeCoverageOracleTest(unittest.TestCase):
    def test_profile_passes_with_known_chain_and_diagnostics(self) -> None:
        report = validate_profile(valid_profile())

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["metrics"]["chain_present"])
        self.assertEqual(report["metrics"]["fixed_window_weight"], 60.0)
        self.assertEqual(report["metrics"]["nested_call_weight"], 40.0)
        self.assertAlmostEqual(report["metrics"]["nested_inclusive_over_outer_inclusive"], 40.0 / 60.0)
        self.assertAlmostEqual(report["metrics"]["attributed_fraction"], 0.8)

    def test_missing_chain_is_rejected(self) -> None:
        report = validate_profile(valid_profile(include_chain=False))

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("known fixed_window_tick" in failure for failure in report["failures"]))

    def test_nested_attribution_collapse_is_rejected_when_outer_survives(self) -> None:
        report = validate_profile(valid_profile(nested_weight=0.0))

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("nested_call branch" in failure for failure in report["failures"]))

    def test_zero_required_diagnostic_is_rejected(self) -> None:
        report = validate_profile(valid_profile(callback_failures=1))

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("monitoring callback failures" in failure for failure in report["failures"]))

    def test_observer_callback_names_are_filtered_from_user_branch(self) -> None:
        profile = valid_profile()
        profile.threads[0].nodes.append(Node(method_name="pyStartThunk", times=[1.0]))
        profile.threads[0].children_refs.append(3)

        report = validate_profile(profile)

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("observer/native callback" in failure for failure in report["failures"]))
        self.assertTrue(report["metrics"]["chain_present"])

    def test_workload_timing_and_counters_pass(self) -> None:
        report = validate_workload(valid_workload())

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["metrics"]["elapsed_ns"]["count"], 1_200)
        self.assertEqual(report["metrics"]["invocation_count"], 1_200)

    def test_workload_timing_outside_window_is_rejected(self) -> None:
        workload = valid_workload()
        workload["elapsed_ns"] = {"count": 1_800, "min": 30_000_000, "max": 31_000_000, "mean": 30_000_000, "p50": 30_000_000, "p95": 31_000_000}

        report = validate_workload(workload)

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("approximately 20ms" in failure for failure in report["failures"]))

    def test_workload_alignment_is_required(self) -> None:
        workload = valid_workload()
        workload.pop("counter_alignment")

        report = validate_workload(workload)

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("strict profile-window alignment" in failure for failure in report["failures"]))

    def test_profile_boundaries_require_ordered_observable_acknowledgements(self) -> None:
        boundaries = valid_boundaries()

        self.assertEqual(validate_profile_boundaries(boundaries), [])

        boundaries["stop_command_sent_ns"] = 90
        failures = validate_profile_boundaries(boundaries)

        self.assertTrue(any("boundary ordering" in failure for failure in failures))

    def test_profile_boundaries_require_each_acknowledgement(self) -> None:
        boundaries = valid_boundaries()
        boundaries.pop("stop_complete_acknowledgement")

        failures = validate_profile_boundaries(boundaries)

        self.assertTrue(any("stop completion acknowledgement" in failure for failure in failures))

    def test_timestamp_slice_excludes_windows_outside_profile(self) -> None:
        raw = {
            "module": EXPECTED_MODULE,
            "tick_method": EXPECTED_FIXED,
            "nested_method": EXPECTED_NESTED,
            "window_ns": 20_000_000,
            "clock": {"name": COUNTER_CLOCK, "source": COUNTER_CLOCK_SOURCE, "unit": "ns"},
            "window_records": [
                {"start_ns": 100, "end_ns": 120, "elapsed_ns": 20, "nested_call_count": 10},
                {"start_ns": 200, "end_ns": 220, "elapsed_ns": 20, "nested_call_count": 20},
                {"start_ns": 300, "end_ns": 320, "elapsed_ns": 20, "nested_call_count": 30},
            ],
        }

        aligned = align_workload_counters(raw, 150, 250)

        self.assertEqual(aligned["invocation_count"], 1)
        self.assertEqual(aligned["nested_call_count"], 20)
        self.assertEqual(aligned["counter_alignment"]["excluded_window_count"], 2)
        self.assertEqual(aligned["counter_alignment"]["method"], COUNTER_ALIGNMENT_METHOD)

    def test_timestamp_slice_fails_closed_without_ordered_records(self) -> None:
        raw = {
            "clock": {"name": COUNTER_CLOCK, "source": COUNTER_CLOCK_SOURCE, "unit": "ns"},
            "window_records": [
                {"start_ns": 200, "end_ns": 220, "elapsed_ns": 20, "nested_call_count": 1},
                {"start_ns": 190, "end_ns": 210, "elapsed_ns": 20, "nested_call_count": 1},
            ],
        }

        with self.assertRaises(RuntimeError):
            align_workload_counters(raw, 100, 300)

    def test_stale_aligned_file_differs_from_reconstructed_counters(self) -> None:
        raw = {
            "module": EXPECTED_MODULE,
            "tick_method": EXPECTED_FIXED,
            "nested_method": EXPECTED_NESTED,
            "window_ns": 20_000_000,
            "clock": {"name": COUNTER_CLOCK, "source": COUNTER_CLOCK_SOURCE, "unit": "ns"},
            "window_records": [
                {"start_ns": 100, "end_ns": 120, "elapsed_ns": 20, "nested_call_count": 10},
                {"start_ns": 200, "end_ns": 220, "elapsed_ns": 20, "nested_call_count": 20},
            ],
        }
        derived = align_workload_counters(raw, 100, 220)
        stale = dict(derived)
        stale["nested_call_count"] = derived["nested_call_count"] + 1

        self.assertNotEqual(stale, align_workload_counters(raw, 100, 220))

    def test_analyzer_rejects_stale_aligned_counter_file(self) -> None:
        raw = {
            "module": EXPECTED_MODULE,
            "tick_method": EXPECTED_FIXED,
            "nested_method": EXPECTED_NESTED,
            "window_ns": 20_000_000,
            "clock": {"name": COUNTER_CLOCK, "source": COUNTER_CLOCK_SOURCE, "unit": "ns"},
            "window_records": [
                {"start_ns": 100, "end_ns": 120, "elapsed_ns": 20, "nested_call_count": 10},
                {"start_ns": 200, "end_ns": 220, "elapsed_ns": 20, "nested_call_count": 20},
            ],
        }
        aligned = align_workload_counters(raw, 100, 220)
        stale = dict(aligned)
        stale["nested_call_count"] += 1
        boundaries = valid_boundaries()
        boundaries.update({"profile_start_ns": 100, "profile_end_ns": 220, "start_ack_observed_ns": 100})
        boundaries["command_window_start_ns"] = 100
        boundaries["command_window_end_ns"] = 510

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = root / "rep1-parent"
            case.mkdir()
            (case / "python-native-bridge-coverage-result.json").write_text(
                json.dumps({"status": "PASS", "profile_boundaries": boundaries}), encoding="utf-8"
            )
            (case / "metadata.json").write_text(
                json.dumps(
                    {
                        "components": {
                            "spark": {"sha": PARENT_SHA},
                            "endstone": {"sha": "endstone", "artifact": {"name": "endstone-cp313.whl"}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (case / "python-native-bridge-coverage.sparkprofile").write_bytes(b"profile")
            (case / "coverage-counters.json").write_text(json.dumps(stale), encoding="utf-8")
            (case / "coverage-counters-cumulative.json").write_text(json.dumps(raw), encoding="utf-8")
            (case / "profile-window-boundaries.json").write_text(json.dumps(boundaries), encoding="utf-8")
            row = {
                "rep": 1,
                "target": "parent",
                "sha": PARENT_SHA,
                "label": "rep1-parent",
                "controller_exit_code": 0,
                "status": "PASS",
            }
            with (
                patch("controller.python_native_bridge_coverage_oracle.parse_sampler_data", return_value=valid_profile()),
                patch(
                    "controller.python_native_bridge_coverage_oracle.validate_profile",
                    return_value={"status": "PASS", "failures": [], "metrics": {}},
                ),
                patch(
                    "controller.python_native_bridge_coverage_oracle.validate_workload",
                    return_value={"status": "PASS", "failures": [], "metrics": {}},
                ),
            ):
                report, problems, _endstone_sha = _read_case(
                    root, row, {"parent": PARENT_SHA, "candidate": CANDIDATE_SHA}, 60
                )

        self.assertIsNone(report)
        self.assertTrue(any("do not exactly match cumulative records" in problem for problem in problems))

    def test_noninferiority_passes_with_lower_bound_above_margin(self) -> None:
        report = assess_noninferiority([0.01, 0.02, 0.005, 0.015, 0.012], -0.01)

        self.assertEqual(report["status"], "PASS")
        self.assertGreater(report["lower_95_bound"], -0.01)
        self.assertEqual(report["interval"]["bound_used"], "two_sided_95_lower")

    def test_noninferiority_clear_margin_failure(self) -> None:
        report = assess_noninferiority([-0.10, -0.11, -0.09, -0.105, -0.115], -0.01)

        self.assertEqual(report["status"], "FAIL")
        self.assertLessEqual(report["upper_95_bound"], -0.01)

    def test_high_variance_is_inconclusive(self) -> None:
        report = assess_noninferiority([-0.12, 0.10, -0.08, 0.11, -0.05], -0.01)

        self.assertEqual(report["status"], "INCONCLUSIVE")

    def test_zero_variance_has_finite_degenerate_interval_and_can_pass(self) -> None:
        report = assess_noninferiority([0.01] * 5, -0.01)

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["interval"]["degenerate"])
        self.assertEqual(report["interval"]["two_sided_95"]["lower"], 0.01)
        self.assertEqual(report["interval"]["two_sided_95"]["upper"], 0.01)

    def test_zero_variance_can_fail_at_noninferiority_margin(self) -> None:
        report = assess_noninferiority([-0.03] * 5, -0.02)

        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["interval"]["two_sided_95"]["lower"], -0.03)

    def test_analyzer_reports_nested_inclusive_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lines = ["rep\ttarget\tsha\tlabel\tcontroller_exit_code\tstatus\tnote"]
            for rep in range(1, 6):
                for target, sha in (("parent", PARENT_SHA), ("candidate", CANDIDATE_SHA)):
                    lines.append(f"{rep}\t{target}\t{sha}\trep{rep}-{target}\t0\tPASS\t")
            (root / "case-status.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")

            def fake_read_case(_root, row, _expected_sha, _expected_seconds):
                ratio = 0.67 if row["target"] == "parent" else 0.66
                return (
                    {
                        "rep": row["rep"],
                        "target": row["target"],
                        "spark_sha": row["sha"],
                        "endstone_sha": "endstone",
                        "profile": {
                            "metrics": {
                                "attributed_fraction": 0.8,
                                "fixed_window_fraction": 0.8,
                                "nested_inclusive_over_outer_inclusive": ratio,
                            }
                        },
                        "workload": {},
                    },
                    [],
                    "endstone",
                )

            with patch("controller.python_native_bridge_coverage_oracle._read_case", side_effect=fake_read_case):
                report = analyze_paired_evidence(root, PARENT_SHA, CANDIDATE_SHA)

        endpoint = report["estimands"]["nested_inclusive_over_outer_inclusive"]
        self.assertEqual(endpoint["status"], "PASS")
        for delta in endpoint["deltas"]:
            self.assertAlmostEqual(delta, -0.01)
        for delta in endpoint["deltas_percentage_points"]:
            self.assertAlmostEqual(delta, -1.0)
        self.assertEqual(report["preregistered_estimands"]["nested_inclusive_over_outer_inclusive"]["margin_percentage_points"], -2.0)

    def test_analyzer_fails_on_near_total_nested_attribution_collapse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lines = ["rep\ttarget\tsha\tlabel\tcontroller_exit_code\tstatus\tnote"]
            for rep in range(1, 6):
                for target, sha in (("parent", PARENT_SHA), ("candidate", CANDIDATE_SHA)):
                    lines.append(f"{rep}\t{target}\t{sha}\trep{rep}-{target}\t0\tPASS\t")
            (root / "case-status.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")

            def fake_read_case(_root, row, _expected_sha, _expected_seconds):
                ratio = 0.67 if row["target"] == "parent" else 0.01
                return (
                    {
                        "rep": row["rep"],
                        "target": row["target"],
                        "spark_sha": row["sha"],
                        "endstone_sha": "endstone",
                        "profile": {
                            "metrics": {
                                "attributed_fraction": 0.8,
                                "fixed_window_fraction": 0.8,
                                "nested_inclusive_over_outer_inclusive": ratio,
                            }
                        },
                        "workload": {},
                    },
                    [],
                    "endstone",
                )

            with patch("controller.python_native_bridge_coverage_oracle._read_case", side_effect=fake_read_case):
                report = analyze_paired_evidence(root, PARENT_SHA, CANDIDATE_SHA)

        endpoint = report["estimands"]["nested_inclusive_over_outer_inclusive"]
        self.assertEqual(endpoint["status"], "FAIL")
        self.assertEqual(report["status"], "FAIL")

    def test_case_status_requires_exact_sha_and_case_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case-status.tsv"
            lines = ["rep\ttarget\tsha\tlabel\tcontroller_exit_code\tstatus\tnote"]
            for rep in range(1, 6):
                for target, sha in (("parent", PARENT_SHA), ("candidate", CANDIDATE_SHA)):
                    lines.append(f"{rep}\t{target}\t{sha}\trep{rep}-{target}\t0\tPASS\t")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            rows, problems = parse_case_status(path, 5)

        self.assertEqual(len(rows), 10)
        self.assertEqual(problems, [])

    def test_case_status_rejects_missing_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case-status.tsv"
            path.write_text(
                "rep\ttarget\tsha\tlabel\tcontroller_exit_code\tstatus\tnote\n"
                f"1\tparent\t{PARENT_SHA}\trep1-parent\t0\tPASS\t\n",
                encoding="utf-8",
            )

            _rows, problems = parse_case_status(path, 5)

        self.assertTrue(any("case set mismatch" in problem for problem in problems))

    def test_case_evidence_rejects_wrong_spark_sha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            row = {
                "rep": 1,
                "target": "parent",
                "sha": "0" * 40,
                "label": "rep1-parent",
                "controller_exit_code": 0,
                "status": "PASS",
            }

            _report, problems, _endstone_sha = _read_case(
                Path(directory), row, {"parent": PARENT_SHA, "candidate": CANDIDATE_SHA}, 60
            )

        self.assertTrue(any("status SHA" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
