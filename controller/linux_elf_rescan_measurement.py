#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import traceback

from controller.run_test import IntegrationTest, now_iso, write_json

MARKER = "SPARK_ELF_RESCAN_RESULT "


class LinuxElfRescanMeasurement(IntegrationTest):
    disable_bstats = True

    def __init__(self, helper: pathlib.Path) -> None:
        super().__init__("linux")
        self.helper = helper.resolve()
        self.result["test_kind"] = "linux-elf-rescan-measurement"
        write_json(self.result_path, self.result)

    def wait_for_measurement(self) -> dict[str, object]:
        assert self.server is not None
        lines = self.server.wait_for(
            lambda current: any(MARKER in line for line in current),
            timeout=90.0,
            description="ELF rescan measurement marker",
        )
        candidates = [line.split(MARKER, 1)[1].strip() for line in lines if MARKER in line]
        if len(candidates) != 1:
            raise RuntimeError(f"expected exactly one ELF rescan measurement marker, got {len(candidates)}")
        evidence = json.loads(candidates[0])
        if not isinstance(evidence, dict) or evidence.get("status") != "PASS":
            raise RuntimeError(f"ELF rescan measurement did not pass: {evidence!r}")
        required_positive = (
            "iterations",
            "target_count",
            "page_count",
            "wall_mean_ms",
            "thread_cpu_mean_ms",
        )
        missing = [key for key in required_positive if float(evidence.get(key, 0) or 0) <= 0]
        if missing:
            raise RuntimeError(f"ELF rescan evidence missing positive metrics: {missing}")
        if evidence.get("installed_patch_phase_included") is not False:
            raise RuntimeError("probe must truthfully identify scan-only measurement coverage")
        return evidence

    def execute_measurement(self) -> int:
        stage = "initialization"
        previous_helper = os.environ.get("SPARK_ELF_RESCAN_HELPER")
        try:
            if not self.helper.is_file():
                raise FileNotFoundError(self.helper)
            stage = "artifact-discovery"
            self.install_artifacts()
            os.environ["SPARK_ELF_RESCAN_HELPER"] = str(self.helper)
            stage = "bds-start"
            self.start_server()
            stage = "measurement"
            evidence = self.wait_for_measurement()
            self.result["elf_rescan_measurement"] = evidence
            self.check(
                "elf-rescan-real-bds",
                "PASS",
                "production ElfImportHooks scan path measured in the live BDS process",
                iterations=evidence["iterations"],
                target_count=evidence["target_count"],
                wall_mean_ms=evidence["wall_mean_ms"],
                thread_cpu_mean_ms=evidence["thread_cpu_mean_ms"],
                scan_only_amortized_cpu_percent=evidence["scan_only_amortized_cpu_percent"],
            )
            stage = "shutdown"
            self.shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1200]
            diagnostic = traceback.format_exc()
            try:
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:
                diagnostic += "\n\nCleanup failure:\n" + traceback.format_exc()
            self.diagnostics.write_text(diagnostic, encoding="utf-8")
            return 1
        finally:
            if previous_helper is None:
                os.environ.pop("SPARK_ELF_RESCAN_HELPER", None)
            else:
                os.environ["SPARK_ELF_RESCAN_HELPER"] = previous_helper
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    helper = pathlib.Path(os.environ.get("SPARK_ELF_RESCAN_HELPER_BINARY", "fixtures/elf-rescan-probe/libspark_elf_rescan_probe.so"))
    return LinuxElfRescanMeasurement(helper).execute_measurement()


if __name__ == "__main__":
    raise SystemExit(main())
