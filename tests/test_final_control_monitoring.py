from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tomllib

from controller.final_control_monitoring import (
    DEFAULT_MEASUREMENT_SECONDS,
    WORKLOAD_ITERATIONS,
    FinalControlMonitoringCase,
    _numeric_deltas,
    _required_bds_version,
    _validate_exact_bds_evidence,
    parse_profiler_inactivity,
    read_activity_snapshot,
    validate_profiler_window,
)


class FinalControlMonitoringContractTest(unittest.TestCase):
    @staticmethod
    def _case(root: Path, name: str, enabled: bool) -> FinalControlMonitoringCase:
        root.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(Path, "cwd", return_value=root):
            return FinalControlMonitoringCase(name, enabled, "a" * 40, bds_version="1.26.44.3")

    def test_control_and_monitoring_have_identical_workload_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = self._case(root / "control", "control", False)
            monitoring = self._case(root / "monitoring", "monitoring", True)

        self.assertEqual(control.result["measurement_contract"], monitoring.result["measurement_contract"])
        self.assertFalse(control.result["spark_enabled"])
        self.assertTrue(monitoring.result["spark_enabled"])
        self.assertIsNone(control.result["deployment"]["spark_plugin"])
        self.assertEqual(control.result["deployment"]["spark_absence_proof"]["spark_binary_present"], False)
        self.assertIsNone(monitoring.result["deployment"]["spark_absence_proof"])
        self.assertEqual(control.result["measurement_contract"]["workload_iterations"], WORKLOAD_ITERATIONS)
        self.assertIn("rss_p95_bytes", control.result["measurement_contract"]["measurement_metric_fields"])
        self.assertIn("mspt_p95", control.result["measurement_contract"]["workload_metric_fields"])

    def test_spark_deployment_must_match_case_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(Path, "cwd", return_value=Path(temp)):
            with self.assertRaisesRegex(ValueError, "does not match"):
                FinalControlMonitoringCase("control", True, "a" * 40, bds_version="1.26.44.3")
            with self.assertRaisesRegex(ValueError, "does not match"):
                FinalControlMonitoringCase("monitoring", False, "a" * 40, bds_version="1.26.44.3")

    def test_profiler_inactivity_requires_explicit_inactive_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            case = self._case(Path(temp), "monitoring", True)
            case.command_check = mock.Mock(return_value=["There isn't an active profiler running."])
            case.profiler_inactive("before")
            self.assertIn("before", case.result["profiler_state"])

            case.command_check.return_value = ["Profiler is currently running"]
            with self.assertRaisesRegex(RuntimeError, "active or ambiguous|not proven"):
                case.profiler_inactive("after")

    def test_profiler_inactivity_rejects_generic_and_ambiguous_status_text(self) -> None:
        for lines in (["profiler is not running"], ["The profiler has stopped; results are still being finalized."]):
            with self.subTest(lines=lines), self.assertRaisesRegex(RuntimeError, "not proven|active or ambiguous"):
                parse_profiler_inactivity(lines)

    def test_exact_bds_version_requires_full_input_and_derives_protocol(self) -> None:
        self.assertEqual(_required_bds_version("1.26.44.3"), ("1.26.44.3", "26.44"))
        for value in ("", "26.44", "1.26.44", "1.26.44.x"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "EXPECTED_BDS_VERSION"):
                _required_bds_version(value)

    def test_exact_bds_evidence_rejects_missing_or_mismatched_runtime_values(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "protocol version mismatch"):
            _validate_exact_bds_evidence(
                {"bds_version": "25.0"},
                ["Version: 1.26.44.3"],
                "1.26.44.3",
                "26.44",
            )
        with self.assertRaisesRegex(RuntimeError, "full version mismatch"):
            _validate_exact_bds_evidence(
                {"bds_version": "26.44"},
                [],
                "1.26.44.3",
                "26.44",
            )

    def test_profiler_window_rejects_activity_changes_and_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "activity.json"
            path.write_text("[]\n", encoding="utf-8")
            before = read_activity_snapshot(path)
            path.write_text(
                json.dumps(
                    [
                        {
                            "time": 150,
                            "type": "profiler",
                            "data": {"type": "url", "value": "https://spark.lucko.me/test"},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            after = read_activity_snapshot(path)
            with self.assertRaisesRegex(RuntimeError, "activity log changed"):
                validate_profiler_window(before, after, start_ns=100, end_ns=200)

            path.write_text("[]\n", encoding="utf-8")
            after = read_activity_snapshot(path)
            with self.assertRaisesRegex(RuntimeError, "transition evidence"):
                validate_profiler_window(
                    before,
                    after,
                    start_ns=100,
                    end_ns=200,
                    log_lines=["Stopping the profiler and finalizing results, please wait..."],
                )

    def test_comparable_metrics_require_in_window_samples_and_fixed_iterations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case = self._case(root, "control", False)
            case.measure_start_ns = 100
            case.measure_end_ns = 400
            path = root / "comparable-workload-metrics.json"
            path.write_text(
                json.dumps(
                    {
                        "metric": "endstone_server_current_mspt_tps",
                        "iterations": WORKLOAD_ITERATIONS,
                        "samples": [{"monotonic_ns": 50, "mspt": 1, "tps": 20}]
                        + [
                            {"monotonic_ns": 100 + index * 50, "mspt": 4 + index, "tps": 20}
                            for index in range(7)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            case.workload_metrics_path = path
            case.validate_workload_metrics()
            observed = case.result["measurement"]["workload_metrics"]
            self.assertEqual(observed["samples"], 7)
            self.assertEqual(observed["window_start_ns"], 100)
            self.assertEqual(observed["window_end_ns"], 400)

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["iterations"] = WORKLOAD_ITERATIONS + 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "iterations drifted"):
                case.validate_workload_metrics()

    def test_measurement_duration_defaults_are_stable_and_bounded(self) -> None:
        self.assertEqual(DEFAULT_MEASUREMENT_SECONDS, 15)
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(Path, "cwd", return_value=Path(temp)),
            self.assertRaisesRegex(ValueError, "between 5 and 300"),
        ):
            FinalControlMonitoringCase(
                "control", False, "a" * 40, bds_version="1.26.44.3", measurement_seconds=4
            )

    def test_comparison_controller_requires_canonical_disabled_bstats(self) -> None:
        self.assertTrue(FinalControlMonitoringCase.disable_bstats)

    def test_comparable_workload_declares_a_valid_endstone_entry_point(self) -> None:
        fixture = Path(__file__).parents[1] / "fixtures" / "endstone-final-comparable-workload" / "pyproject.toml"
        metadata = tomllib.loads(fixture.read_text(encoding="utf-8"))
        entry_points = metadata["project"]["entry-points"]["endstone"]
        self.assertEqual(
            entry_points,
            {"final-comparable-workload": "endstone_final_comparable_workload:ComparableWorkloadPlugin"},
        )

    def test_paired_comparison_records_monitoring_minus_control_deltas(self) -> None:
        results = {
            "control": {"measurement": {"metrics": {"cpu_ms_per_tick": 2.0, "ticks": 10}}},
            "monitoring": {"measurement": {"metrics": {"cpu_ms_per_tick": 3.5, "ticks": 10}}},
        }

        self.assertEqual(_numeric_deltas(results, ("measurement", "metrics")), {"cpu_ms_per_tick": 1.5, "ticks": 0.0})


class FinalControlMonitoringWorkflowTest(unittest.TestCase):
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "final-control-monitoring.yml"

    def test_workflow_is_linux_paired_exact_sha_and_fail_closed(self) -> None:
        text = self.workflow.read_text(encoding="utf-8")
        self.assertIn("spark_sha:", text)
        self.assertIn("bds_version:", text)
        self.assertIn("endstone_sha:", text)
        self.assertIn("--spark-sha \"$SPARK_SHA\"", text)
        self.assertIn("--bds-version \"$EXPECTED_BDS_VERSION\"", text)
        self.assertIn("EXPECTED_BDS_VERSION", text)
        self.assertIn("EXPECTED_BDS_PROTOCOL_VERSION", text)
        self.assertIn("--result evidence/control/test-results.json", text)
        self.assertIn("--result evidence/monitoring/test-results.json", text)
        self.assertIn("if: always()", text)
        self.assertIn("runs-on: ubuntu-24.04", text)
        controller = (self.workflow.parents[2] / "controller" / "final_control_monitoring.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("endstone-final-comparable-workload", controller)


if __name__ == "__main__":
    unittest.main()
