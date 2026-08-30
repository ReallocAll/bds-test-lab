from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from controller.candidate_a_blocked_analyzer import (
    analyze_evidence,
    ci_half_width,
    sequential_ci,
    summarize,
)
from controller.candidate_a_blocked_benchmark import (
    AFFINITY_POLL_INTERVAL_SECONDS,
    BASELINE_SHA,
    BOT_COUNT,
    BOT_PROGRESS_COUNTER_SCOPE,
    BOT_REF,
    BOT_SCENARIO,
    BOT_SCENARIO_SHA256,
    CANDIDATE_SHA,
    CHUNK_RADIUS,
    CPU_METRIC_RESOLUTION_LIMIT_PERCENTAGE_POINTS,
    ENDSTONE_SHA,
    HOTSPOT_ITERATIONS,
    HOTSPOT_ITERATIONS_RATIONALE,
    HOTSPOT_MODE,
    MEASUREMENT_SECONDS,
    PROTOCOL_VERSION,
    SAMPLE_INTERVAL_MS,
    STATIONARY_BOUNDED_AREA_POLICY,
    TREATMENTS,
    WARMUP_SECONDS,
    WORLD_SNAPSHOT_ID,
    AffinityError,
    _scenario_contract,
    batch_schedule,
    block_schedule,
    case_id,
    choose_controlled_cpu,
    pin_and_verify_task_affinity,
    progress_window_deltas,
    validate_affinity_snapshot,
)

SCENARIO_SHA = BOT_SCENARIO_SHA256


def _case_result(block: int, position: int, treatment: str, cpu: float) -> dict[str, object]:
    revision = "B" if treatment.endswith("-B") else "C"
    expected_sha = BASELINE_SHA if revision == "B" else CANDIDATE_SHA
    progress_keys = ("chunks_received", "auth_inputs_sent", "movement_inputs_sent", "action_packets_sent")
    progress_boundaries = {
        name: {
            "monotonic_ns": timestamp,
            "event_count": 1,
            "fleet_progress_events": 1,
            "progress_counters": {
                "counters": {key: 0 for key in progress_keys},
                "source_event": "fleet_progress",
                "source_index": 0,
            },
        }
        for name, timestamp in zip(
            ("warmup_start", "warmup_end", "measurement_start", "measurement_end"),
            (2, 3, 4, 5),
            strict=True,
        )
    }
    protocol: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "case_id": case_id(block, position, treatment),
        "block_index": block,
        "position": position,
        "treatment": treatment,
        "mode": treatment.split("-", 1)[0],
        "revision": revision,
        "baseline_sha": BASELINE_SHA,
        "candidate_sha": CANDIDATE_SHA,
        "endstone_sha": ENDSTONE_SHA,
        "endstone_artifact": {
            "repository": "EndstoneMC/endstone",
            "sha": ENDSTONE_SHA,
            "run_id": 404,
            "artifact": {
                "id": 505,
                "name": "endstone-linux-cp313.zip",
                "size_in_bytes": 123,
                "expires_at": "2099-01-01T00:00:00Z",
            },
        },
        "bot_ref": BOT_REF,
        "expected_spark_sha": expected_sha,
        "platform": "linux",
        "bot_count": BOT_COUNT,
        "bot_scenario": BOT_SCENARIO,
        "hotspot_mode": HOTSPOT_MODE,
        "hotspot_iterations": HOTSPOT_ITERATIONS,
        "hotspot_iterations_rationale": HOTSPOT_ITERATIONS_RATIONALE,
        "warmup_seconds": WARMUP_SECONDS,
        "measurement_seconds": MEASUREMENT_SECONDS,
        "sample_interval_ms": SAMPLE_INTERVAL_MS,
        "cpu_metric_resolution_limit_percentage_points": CPU_METRIC_RESOLUTION_LIMIT_PERCENTAGE_POINTS,
        "affinity_poll_interval_seconds": AFFINITY_POLL_INTERVAL_SECONDS,
        "chunk_radius": CHUNK_RADIUS,
        "world_snapshot_id": WORLD_SNAPSHOT_ID,
        "scenario": {
            "name": BOT_SCENARIO,
            "sha256": SCENARIO_SHA,
            "steps": 1,
            "actions": ["idle"],
            "indefinite_idle": True,
            "bounded_area_policy": STATIONARY_BOUNDED_AREA_POLICY,
        },
        "bot_progress_counter_keys": [
            "chunks_received",
            "auth_inputs_sent",
            "movement_inputs_sent",
            "action_packets_sent",
        ],
        "bot_progress_counter_scope": BOT_PROGRESS_COUNTER_SCOPE,
        "pep_event_scope": "full-profile-cumulative; not window-aligned",
        "affinity_model": "controlled-process CPU isolation; host and kernel work are not excluded",
        "world": {
            "snapshot_id": WORLD_SNAPSHOT_ID,
            "level_type": "FLAT",
            "level_seed": "8675309",
            "view_distance": CHUNK_RADIUS,
            "tick_distance": 4,
            "world_recreated": True,
        },
    }
    performance: dict[str, object] = {
        "process_cpu_percent_of_one_core": cpu,
        "cpu_ms_per_tick": cpu / 2,
        "ticks": 12000,
        "wall_seconds": 600.0,
        "cpu_seconds": cpu * 6.0,
        "cpu_snapshots": {
            "start": {"monotonic_ns": 60_000_000_002, "cpu_seconds": 0.0},
            "end": {"monotonic_ns": 660_000_000_002, "cpu_seconds": cpu * 6.0},
            "interval_ns": 600_000_000_000,
            "interval_seconds": 600.0,
            "denominator": "end CPU snapshot monotonic_ns - start CPU snapshot monotonic_ns",
            "counter_resolution_seconds": 0.01,
            "metric_resolution_percentage_points": 0.01 / 600.0 * 100.0,
        },
        "tick_statistics": {
            "samples": 12000,
            "mspt_mean": 25.0,
            "window": {
                "start_monotonic_ns": 60_000_000_002,
                "end_monotonic_ns": 660_000_000_002,
                "inclusive": True,
            },
        },
        "counter_windows": {
            "warmup": {
                "configured_seconds": 60,
                "observed_seconds": 60.0,
                "start_monotonic_ns": 1,
                "end_monotonic_ns": 60_000_000_001,
            },
            "measurement": {
                "configured_seconds": 600,
                "observed_seconds": 600.0,
                "start_monotonic_ns": 60_000_000_002,
                "end_monotonic_ns": 660_000_000_002,
                "tick_start": 100,
                "tick_end": 12100,
                "ticks": 12000,
                "tick_start_monotonic_ns": 60_000_000_002,
                "tick_end_monotonic_ns": 660_000_000_002,
            },
        },
    }
    if treatment.startswith("full"):
        performance.update(
            {
                "viewer_url": "https://spark.lucko.me/test-profile",
                "profile_file": "python-attribution-performance.sparkprofile",
                "profile_file_bytes": len(b"profile"),
                "profile_file_sha256": hashlib.sha256(b"profile").hexdigest(),
                "pep_event_window": {
                    "scope": "full-profile-cumulative; not window-aligned",
                },
                "pep_events": {
                    "py_start": 10_000,
                    "py_resume": 10_000,
                    "py_throw": 0,
                    "py_return": 10_000,
                    "py_yield": 10_000,
                    "py_unwind": 0,
                    "registered_threads": 3,
                    "overflows": 0,
                    "snapshot_attempts": 100,
                    "snapshot_failures": 1,
                    "attributed_samples": 10,
                    "native_only_samples": 2,
                    "native_boundary_misses": 0,
                    "thread_mismatches": 1,
                    "unknown_code_ids": 0,
                    "callback_failures": 0,
                },
            }
        )
    return {
        "status": "PASS",
        "protocol": protocol,
        "artifact_metadata": {
            "components": {
                "spark": {"sha": expected_sha},
                "endstone": {
                    "repository": "EndstoneMC/endstone",
                    "sha": ENDSTONE_SHA,
                    "run_id": 404,
                    "artifact": {
                        "id": 505,
                        "name": "endstone-linux-cp313.zip",
                        "size_in_bytes": 123,
                        "expires_at": "2099-01-01T00:00:00Z",
                    },
                },
            }
        },
        "world": {
            "snapshot_id": WORLD_SNAPSHOT_ID,
            "level_type": "FLAT",
            "level_seed": "8675309",
            "view_distance": CHUNK_RADIUS,
            "tick_distance": 4,
            "world_recreated": True,
        },
        "affinity": {
            "bds_pid": 101,
            "initial_bds_pid": 100,
            "load_generator_pid": 202,
            "controller_pid": 303,
                "controlled_cpu": 7,
                "bds_affinity_after": [7],
                "load_generator_affinity": [0, 1, 2, 3, 4, 5, 6],
                "controller_affinity": [0, 1, 2, 3, 4, 5, 6],
                "available_cpus": [0, 1, 2, 3, 4, 5, 6, 7],
                "bds_tid_affinities": {"101": [7], "102": [7]},
                "load_generator_tid_affinities": {"202": [0, 1, 2, 3, 4, 5, 6]},
                "controller_tid_affinities": {"303": [0, 1, 2, 3, 4, 5, 6]},
                "bds_tids": [101, 102],
                "load_generator_tids": [202],
                "controller_tids": [303],
                "runner_cpu_topology": {
                    "allowed_cpus": [0, 1, 2, 3, 4, 5, 6, 7],
                    "cpu_count": 8,
                    "controlled_process_isolation": True,
                    "host_work_excluded": False,
                    "kernel_and_unrelated_host_work_excluded": False,
                },
                "verification_count": 2,
                "verification_samples": [
                    {
                        "monotonic_ns": 1,
                        "phase": "warmup",
                        "bds_tids": {"101": [7], "102": [7]},
                        "load_generator_tids": {"202": [0, 1, 2, 3, 4, 5, 6]},
                        "controller_tids": {"303": [0, 1, 2, 3, 4, 5, 6]},
                    },
                    {
                        "monotonic_ns": 2,
                        "phase": "measurement",
                        "bds_tids": {"101": [7], "102": [7]},
                        "load_generator_tids": {"202": [0, 1, 2, 3, 4, 5, 6]},
                        "controller_tids": {"303": [0, 1, 2, 3, 4, 5, 6]},
                    },
                ],
                "verified": True,
        },
        "workload": {
            "bot_count": BOT_COUNT,
            "scenario": BOT_SCENARIO,
            "chunk_radius": CHUNK_RADIUS,
            "progress_counter_scope": BOT_PROGRESS_COUNTER_SCOPE,
            "boundaries": {
                "online": {"monotonic_ns": 1},
                **progress_boundaries,
                "before_disconnect": {"monotonic_ns": 6},
            },
            "fleet_online": {"online": BOT_COUNT, "count": BOT_COUNT},
            "fleet_shutdown": {
                "graceful_shutdown": True,
                "launched": BOT_COUNT,
                "online": BOT_COUNT,
                "packets_received": 0,
                "chunks_received": 0,
                "auth_inputs_sent": 0,
                "movement_inputs_sent": 0,
                "action_packets_sent": 0,
            },
            "progress_window_deltas": {
                "counter_keys": [
                    "chunks_received",
                    "auth_inputs_sent",
                    "movement_inputs_sent",
                    "action_packets_sent",
                ],
                "warmup": {key: 0 for key in ("chunks_received", "auth_inputs_sent", "movement_inputs_sent", "action_packets_sent")},
                "measurement": {key: 0 for key in ("chunks_received", "auth_inputs_sent", "movement_inputs_sent", "action_packets_sent")},
                "scope": BOT_PROGRESS_COUNTER_SCOPE,
                "monotonic": True,
            },
            "input_counters": {
                "packets_received": 0,
                "chunks_received": 0,
                "auth_inputs_sent": 0,
                "movement_inputs_sent": 0,
                "action_packets_sent": 0,
            },
        },
        "performance": performance,
    }


def _write_batch(
    root: Path,
    *,
    start_block: int = 1,
    did_values: list[float] | None = None,
) -> None:
    did_values = did_values or [1.0, 1.0, 1.0, 1.0]
    for offset in range(4):
        block = start_block + offset
        for position, treatment in enumerate(block_schedule(block)):
            # Pick treatment baselines so that the requested DID is exact.
            off_b = 90.0 + block
            off_c = off_b + 1.0
            full_b = 92.0 + block
            full_c = full_b + 1.0 + did_values[offset]
            cpu = {
                "off-B": off_b,
                "off-C": off_c,
                "full-B": full_b,
                "full-C": full_c,
            }[treatment]
            directory = root / f"block-{block:02d}" / treatment
            directory.mkdir(parents=True, exist_ok=True)
            result = _case_result(block, position, treatment, cpu)
            if treatment.startswith("full"):
                (directory / "python-attribution-performance.sparkprofile").write_bytes(b"profile")
            (directory / "candidate-a-blocked-result.json").write_text(
                json.dumps(result), encoding="utf-8"
            )


class CandidateABlockedBenchmarkTest(unittest.TestCase):
    def test_four_block_schedule_is_position_balanced(self) -> None:
        schedule = batch_schedule(1)
        expected = {
            1: ("off-B", "off-C", "full-B", "full-C"),
            2: ("off-C", "off-B", "full-C", "full-B"),
            3: ("full-B", "full-C", "off-B", "off-C"),
            4: ("full-C", "full-B", "off-C", "off-B"),
        }
        self.assertEqual(schedule, expected)
        self.assertEqual(set(schedule), {1, 2, 3, 4})
        for row in schedule.values():
            self.assertEqual(abs(row.index("off-B") - row.index("off-C")), 1)
            self.assertEqual(abs(row.index("full-B") - row.index("full-C")), 1)
        for treatment in TREATMENTS:
            positions = [row.index(treatment) for row in schedule.values()]
            self.assertEqual(sorted(positions), [0, 1, 2, 3])
        self.assertEqual(block_schedule(5), expected[1])

    def test_ci_uses_two_sided_paired_t_interval(self) -> None:
        self.assertAlmostEqual(ci_half_width([1.0, 1.0, 1.0]), 0.0)
        self.assertGreater(ci_half_width([0.0, 1.0]) or 0.0, 6.0)
        self.assertEqual(summarize([1.0, 2.0])["min"], 1.0)

    def test_confirmatory_interval_uses_five_look_bonferroni_values(self) -> None:
        critical_values = {
            4: 5.840909,
            8: 3.499483,
            12: 3.105807,
            16: 2.946714,
            20: 2.860935,
        }
        for sample_count, expected_critical in critical_values.items():
            interval = sequential_ci([1.0] * sample_count)
            self.assertAlmostEqual(interval["critical_value"], expected_critical)
        interval = sequential_ci([1.0, 1.0, 1.0, 1.0])
        self.assertEqual(interval["n"], 4)
        self.assertEqual(interval["degrees_of_freedom"], 3)
        self.assertEqual(interval["confidence"], 0.99)
        self.assertEqual(interval["familywise_confidence"], 0.95)
        self.assertAlmostEqual(interval["critical_value"], 5.840909)
        self.assertEqual(interval["lower"], 1.0)
        self.assertEqual(interval["upper"], 1.0)
        self.assertEqual(interval["half_width"], 0.0)
        self.assertTrue(interval["precision_target_met"])

    def test_sequential_decision_branches_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cases = (
                ([-1.0, -1.0, -1.0, -1.0], "KEEP"),
                ([1.0, 1.0, 1.0, 1.0], "REVERT"),
                ([0.0, 0.0, 0.0, 0.0], "INCONCLUSIVE"),
                ([-4.0, 2.0, -3.0, 5.0], "CONTINUE"),
            )
            for index, (did_values, expected_outcome) in enumerate(cases):
                root = Path(temp) / f"case-{index}"
                _write_batch(root, did_values=did_values)
                summary = analyze_evidence([root], start_block=1)
                self.assertTrue(summary["valid"])
                self.assertEqual(summary["outcome"], expected_outcome)
                self.assertEqual(summary["sequential_inference"]["decision"], expected_outcome)
        self.assertFalse(summary["significance_test_used"])
        self.assertEqual(summary["ci_confidence"], 0.95)
        self.assertEqual(summary["ci_role"], "descriptive_only")

    def test_continue_when_precision_target_is_not_reached(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_batch(root, did_values=[-4.0, 2.0, -3.0, 5.0])
            summary = analyze_evidence([root], start_block=1)
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["outcome"], "CONTINUE")
        self.assertGreater(summary["did"]["cpu_percent_of_one_core"]["ci95_half_width"], 0.5)
        self.assertGreater(summary["did"]["cpu_percent_of_one_core"]["confirmatory_ci"]["half_width"], 0.5)

    def test_combines_prior_batches_in_deterministic_block_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "run-1"
            second = root / "run-2"
            _write_batch(first)
            _write_batch(second, start_block=5)
            summary = analyze_evidence([second, first], start_block=5)
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["blocks_evaluated"], list(range(1, 9)))
        self.assertEqual(summary["case_count"], 32)

    def test_missing_case_is_invalid_and_preserves_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_batch(root)
            missing = root / "block-04" / block_schedule(4)[0] / "candidate-a-blocked-result.json"
            missing.unlink()
            failure = missing.parent / "failure-diagnostics.txt"
            failure.write_text("BDS exited", encoding="utf-8")
            failure_preserved = failure.is_file()
            summary = analyze_evidence([root], start_block=1)
        self.assertFalse(summary["valid"])
        self.assertEqual(summary["outcome"], "CONTINUE")
        self.assertTrue(failure_preserved)
        self.assertTrue(any("missing cases" in error for error in summary["errors"]))

    def test_sha_mismatch_and_counter_window_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_batch(root)
            result_path = root / "block-01" / block_schedule(1)[0] / "candidate-a-blocked-result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["artifact_metadata"]["components"]["spark"]["sha"] = "0" * 40
            result["artifact_metadata"]["components"]["endstone"]["sha"] = "0" * 40
            result["performance"]["counter_windows"]["measurement"]["observed_seconds"] = 590.0
            result["protocol"]["scenario"]["sha256"] = "drifted"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            summary = analyze_evidence([root], start_block=1)
        self.assertFalse(summary["valid"])
        self.assertTrue(any("SHA mismatch" in error for error in summary["errors"]))
        self.assertTrue(any("Endstone SHA mismatch" in error for error in summary["errors"]))
        self.assertTrue(any("exact 600 seconds" in error for error in summary["errors"]))
        self.assertTrue(any("scenario contract mismatch" in error for error in summary["errors"]))

    def test_endstone_artifact_drift_invalidates_cumulative_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_batch(root)
            result_path = root / "block-02" / block_schedule(2)[0] / "candidate-a-blocked-result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["artifact_metadata"]["components"]["endstone"]["run_id"] = 405
            result_path.write_text(json.dumps(result), encoding="utf-8")
            summary = analyze_evidence([root], start_block=1)
        self.assertFalse(summary["valid"])
        self.assertTrue(any("drifts across the cumulative experiment" in error for error in summary["errors"]))

    def test_cpu_snapshot_timing_and_resolution_are_gated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "timing"
            _write_batch(root)
            result_path = root / "block-01" / block_schedule(1)[0] / "candidate-a-blocked-result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["performance"]["cpu_snapshots"]["interval_seconds"] = 599.0
            result_path.write_text(json.dumps(result), encoding="utf-8")
            timing_summary = analyze_evidence([root], start_block=1)

            result["performance"]["cpu_snapshots"]["interval_seconds"] = 600.0
            result["performance"]["cpu_snapshots"]["metric_resolution_percentage_points"] = 0.5
            result_path.write_text(json.dumps(result), encoding="utf-8")
            resolution_summary = analyze_evidence([root], start_block=1)
        self.assertFalse(timing_summary["valid"])
        self.assertTrue(any("CPU snapshot interval seconds" in error for error in timing_summary["errors"]))
        self.assertFalse(resolution_summary["valid"])
        self.assertTrue(any("metric resolution" in error for error in resolution_summary["errors"]))

    def test_full_profile_required_diagnostics_must_be_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_batch(root)
            result_path = root / "block-01" / block_schedule(1)[2] / "candidate-a-blocked-result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["performance"]["pep_events"]["callback_failures"] = 1
            result_path.write_text(json.dumps(result), encoding="utf-8")
            summary = analyze_evidence([root], start_block=1)
        self.assertFalse(summary["valid"])
        self.assertTrue(any("callback_failures" in error for error in summary["errors"]))

    def test_maximum_blocks_are_inconclusive_without_direction_when_precision_is_not_reached(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            roots = []
            for start_block in (1, 5, 9, 13, 17):
                batch_root = root / f"run-{start_block}"
                _write_batch(batch_root, start_block=start_block, did_values=[10.0, 10.0, 10.0, 20.0])
                roots.append(batch_root)
            summary = analyze_evidence(roots, start_block=17)
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["end_block"], 20)
        self.assertEqual(summary["outcome"], "MAX_INCONCLUSIVE")
        interval = summary["did"]["cpu_percent_of_one_core"]["confirmatory_ci"]
        self.assertGreater(interval["lower"], 0.0)
        self.assertGreater(interval["half_width"], 0.5)

    def test_affinity_validation_requires_separate_cpus(self) -> None:
        controlled, load = choose_controlled_cpu([0, 1, 2, 3])
        self.assertEqual(controlled, 3)
        self.assertEqual(load, [0, 1, 2])
        report = validate_affinity_snapshot(
            controlled_cpu=3,
            bds_affinity=[3],
            load_generator_affinity=[0, 1, 2],
            available_cpus=[0, 1, 2, 3],
            bds_tid_affinities={"10": [3]},
            load_generator_tid_affinities={"20": [0, 1, 2]},
            controller_affinity=[0, 1, 2],
            controller_tid_affinities={"30": [0, 1, 2]},
        )
        self.assertTrue(report["verified"])
        with self.assertRaises(AffinityError):
            validate_affinity_snapshot(
                controlled_cpu=3,
                bds_affinity=[3],
                load_generator_affinity=[0, 3],
                available_cpus=[0, 1, 2, 3],
                bds_tid_affinities={"10": [3]},
                load_generator_tid_affinities={"20": [0, 3]},
                controller_affinity=[0, 1, 2],
                controller_tid_affinities={"30": [0, 1, 2]},
            )

    def test_stationary_scenario_contract_rejects_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "candidate-a-stationary.json"
            path.write_text(
                '{"name":"candidate-a-stationary","steps":[{"action":"move","ticks":1}]}'
                , encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                _scenario_contract(path)

    def test_affinity_validation_requires_exact_controller_and_tid_sets(self) -> None:
        with self.assertRaises(AffinityError):
            validate_affinity_snapshot(
                controlled_cpu=3,
                bds_affinity=[3],
                load_generator_affinity=[0, 1, 2],
                available_cpus=[0, 1, 2, 3],
                bds_tid_affinities={"10": [3]},
                load_generator_tid_affinities={"20": [0, 1]},
                controller_affinity=[0, 1, 2],
                controller_tid_affinities={"30": [0, 1, 2]},
            )

    def test_stationary_scenario_hash_and_non_saturating_iterations(self) -> None:
        scenario_path = Path(__file__).parents[1] / "scenarios" / "candidate-a-stationary.json"
        contract = _scenario_contract(scenario_path)
        self.assertEqual(contract["sha256"], BOT_SCENARIO_SHA256)
        self.assertEqual(contract["actions"], ["idle"])
        self.assertTrue(contract["indefinite_idle"])
        self.assertEqual(contract["bounded_area_policy"], STATIONARY_BOUNDED_AREA_POLICY)
        self.assertEqual(HOTSPOT_ITERATIONS, 1000)
        self.assertIn("1800-iteration baseline", HOTSPOT_ITERATIONS_RATIONALE)

    def test_progress_window_deltas_allow_zero_stationary_counters_and_reject_backwards(self) -> None:
        keys = ("chunks_received", "auth_inputs_sent", "movement_inputs_sent", "action_packets_sent")
        boundaries = {
            name: {
                "progress_counters": {
                    "counters": {key: 0 for key in keys},
                }
            }
            for name in ("warmup_start", "warmup_end", "measurement_start", "measurement_end")
        }
        deltas = progress_window_deltas(boundaries)
        self.assertEqual(deltas["measurement"], {key: 0 for key in keys})
        boundaries["measurement_end"]["progress_counters"]["counters"]["chunks_received"] = -1
        with self.assertRaises(RuntimeError):
            progress_window_deltas(boundaries)

    def test_progress_boundaries_require_fixed_bot_progress_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_batch(root)
            result_path = root / "block-01" / block_schedule(1)[0] / "candidate-a-blocked-result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["workload"]["boundaries"]["measurement_start"]["progress_counters"]["source_event"] = "bot_stats"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            summary = analyze_evidence([root], start_block=1)
        self.assertFalse(summary["valid"])
        self.assertTrue(any("not from fleet_progress" in error for error in summary["errors"]))

    def test_all_bds_threads_are_repaired_and_fail_when_unverifiable(self) -> None:
        affinity = {101: [0], 102: [0]}
        task_calls = 0

        def fake_task_ids(_pid: int) -> list[int]:
            nonlocal task_calls
            task_calls += 1
            return [101] if task_calls == 1 else [101, 102]

        def fake_get(tid: int) -> list[int]:
            return affinity[tid]

        def fake_set(tid: int, cpus: list[int]) -> None:
            affinity[tid] = list(cpus)

        with (
            mock.patch("controller.candidate_a_blocked_benchmark._linux_task_ids", side_effect=fake_task_ids),
            mock.patch("controller.candidate_a_blocked_benchmark._sched_affinity", side_effect=fake_get),
            mock.patch("controller.candidate_a_blocked_benchmark._set_sched_affinity", side_effect=fake_set),
        ):
            observed = pin_and_verify_task_affinity(999, [3], label="BDS")
        self.assertEqual(observed, {"101": [3], "102": [3]})
        self.assertEqual(affinity, {101: [3], 102: [3]})

        with (
            mock.patch("controller.candidate_a_blocked_benchmark._linux_task_ids", return_value=[101]),
            mock.patch("controller.candidate_a_blocked_benchmark._sched_affinity", return_value=[0]),
            mock.patch("controller.candidate_a_blocked_benchmark._set_sched_affinity"),
            self.assertRaises(AffinityError),
        ):
            pin_and_verify_task_affinity(999, [3], label="BDS")


if __name__ == "__main__":
    unittest.main()
