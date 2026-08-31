from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from controller.python_attribution_validation import PythonAttributionValidation, main
from controller.python_evidence_provenance import (
    validate_bds_version,
    validate_component_provenance,
    validate_endstone_runtime_version,
)
from controller.run_test import (
    READY_HINTS,
    SPARK_LOAD_HINTS,
    IntegrationTest,
    ServerProcess,
    write_json,
)

_ORIGINAL_INSTALL_ARTIFACTS = PythonAttributionValidation.install_artifacts
_ORIGINAL_START_SERVER = PythonAttributionValidation.start_server


class _FrameworkShutdownServerProcess(ServerProcess):
    """Gracefully stop the Windows Endstone root through its interactive command map."""

    lifecycle_registered: bool = False

    def graceful_stop(self, timeout: float = 60.0) -> bool:
        if self.was_forced:
            diagnostic = self.lifecycle_diagnostic or self._begin_lifecycle("interactive-cishutdown", "cishutdown", timeout)
            self._finish_lifecycle(diagnostic, outcome="forced", returncode=self.process.returncode if self.process else None)
            diagnostic["cleanup_outcome"] = "forced"
            return False
        diagnostic = self._begin_lifecycle("interactive-cishutdown", "cishutdown", timeout)
        evidence: dict[str, Any] = diagnostic["acknowledgement_evidence"]
        evidence["registration_observed"] = self.lifecycle_registered
        if not diagnostic["alive_before"]:
            if diagnostic["wrapper_identity"].get("status") != "absent":
                self._finish_lifecycle(diagnostic, outcome="verification-failed", returncode=self.process.returncode if self.process else None)
                diagnostic["cleanup_outcome"] = "verification-failed"
                return False
            self._finish_lifecycle(diagnostic, outcome="uncontrolled-exit", returncode=self.process.returncode if self.process else None)
            diagnostic["cleanup_outcome"] = diagnostic["process_tree_verification"]
            return False
        if diagnostic["wrapper_identity"].get("status") != "verified":
            self._finish_lifecycle(diagnostic, outcome="verification-failed", returncode=self.process.returncode if self.process else None)
            diagnostic["cleanup_outcome"] = "verification-failed"
            return False
        if self.process is None:
            self._finish_lifecycle(diagnostic, outcome="missing-process")
            return False
        try:
            command_start = self.command("cishutdown")
            evidence["command_sent"] = True
            self.process.wait(timeout=timeout)
            reader = getattr(self, "_reader", None)
            if reader is not None:
                reader.join(timeout=3)
            returncode = self.process.returncode
            command_output = self.snapshot()[command_start:]
            evidence["shutdown_requested"] = any(
                "ci lifecycle shutdown requested" in line.lower() for line in command_output
            )
            evidence["observed"] = evidence["shutdown_requested"]
            self._finish_lifecycle(
                diagnostic,
                outcome="exited" if returncode == 0 else "nonzero-exit",
                returncode=returncode,
            )
            if returncode == 0 and not evidence["observed"]:
                diagnostic["outcome"] = "acknowledgement-unobserved"
                diagnostic["cleanup_outcome"] = "verification-failed"
                return False
            if returncode == 0 and diagnostic["process_tree_verification"] != "clean":
                diagnostic["outcome"] = diagnostic["process_tree_verification"]
                diagnostic["cleanup_outcome"] = diagnostic["process_tree_verification"]
                return False
            diagnostic["cleanup_outcome"] = "graceful-exit" if returncode == 0 else "nonzero-exit"
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return returncode == 0
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            diagnostic["exception_type"] = type(exc).__name__
            diagnostic["exception"] = str(exc)
            diagnostic["winerror"] = getattr(exc, "winerror", None)
            diagnostic["errno"] = getattr(exc, "errno", None)
            self._finish_lifecycle(
                diagnostic,
                outcome="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "exception",
                returncode=self.process.returncode,
                timeout_reason=(
                    f"process did not exit within {timeout:.1f}s" if isinstance(exc, subprocess.TimeoutExpired) else None
                ),
            )
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return False


def _start_windows_interactive_server(self: PythonAttributionValidation) -> None:
    """Run Endstone with its interactive command map for graceful CI lifecycle control."""

    cmd = [
        sys.executable,
        "-m",
        "endstone",
        "--yes",
        "--server-folder",
        str(self.server_dir),
    ]
    self.server = _FrameworkShutdownServerProcess(cmd, self.root, self.log_path)
    IntegrationTest._prepare_bstats_before_start(self)
    self.server.start()
    self.server.wait_for(
        lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines),
        420,
        "BDS ready",
    )
    self.check("bds-start", "PASS")
    self.check("ready", "PASS")
    self.server.wait_for(
        lambda lines: any(
            "spark" in line.lower() and any(hint in line.lower() for hint in SPARK_LOAD_HINTS) for line in lines
        ),
        30,
        "Spark enable",
    )
    self.check("spark-load-enable", "PASS")
    self.server.wait_for(
        lambda lines: any("ci lifecycle control enabled; cishutdown registered" in line.lower() for line in lines),
        30,
        "CI lifecycle command registration",
    )
    self.server.lifecycle_registered = True
    self.check("windows-interactive-lifecycle", "PASS", shutdown_control="cishutdown")
    version_file = self.server_dir / "version.txt"
    if version_file.exists():
        self.result["bds_version"] = version_file.read_text(encoding="utf-8").strip()
        write_json(self.result_path, self.result)


def _install_exact_artifacts(self: PythonAttributionValidation) -> None:
    self.disable_bstats = True
    _ORIGINAL_INSTALL_ARTIFACTS(self)
    spark = validate_component_provenance(self.metadata, "spark")
    endstone = validate_component_provenance(self.metadata, "endstone")
    runtime_version = validate_endstone_runtime_version()
    self.check(
        "exact-artifact-provenance",
        "PASS",
        spark_sha=spark.get("sha"),
        spark_run_id=spark.get("run_id"),
        endstone_sha=endstone.get("sha"),
        endstone_run_id=endstone.get("run_id"),
        endstone_artifact_id=(endstone.get("artifact") or {}).get("id"),
        endstone_runtime_version=runtime_version,
    )


def _start_exact_server(self: PythonAttributionValidation) -> None:
    if self.platform == "windows":
        _start_windows_interactive_server(self)
    else:
        _ORIGINAL_START_SERVER(self)
    assert self.server is not None
    observed = validate_bds_version(self.result, self.server.snapshot())
    self.check(
        "exact-bds-version",
        "PASS",
        observed_protocol=observed,
        expected_protocol=os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION", "").strip() or None,
        expected_full=os.environ.get("EXPECTED_BDS_VERSION", "").strip() or None,
    )


PythonAttributionValidation.install_artifacts = _install_exact_artifacts
PythonAttributionValidation.start_server = _start_exact_server

if __name__ == "__main__":
    raise SystemExit(main())
