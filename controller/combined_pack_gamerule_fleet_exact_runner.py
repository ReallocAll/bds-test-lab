from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any

from controller.combined_pack_gamerule_fleet_validation import (
    CombinedPackGameruleFleetValidation,
    main,
)
from controller.python_evidence_provenance import (
    validate_bds_version,
    validate_component_provenance,
    validate_endstone_runtime_version,
)
from controller.run_test import READY_HINTS, SPARK_LOAD_HINTS, ServerProcess, write_json

_ORIGINAL_INSTALL_ARTIFACTS = CombinedPackGameruleFleetValidation.install_artifacts
_ORIGINAL_START_SERVER = CombinedPackGameruleFleetValidation.start_server
_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", None)


class _FrameworkShutdownServerProcess(ServerProcess):
    """Gracefully interrupt the Windows Endstone root process in headless mode."""

    lifecycle_diagnostic: dict[str, Any]

    def graceful_stop(self, timeout: float = 60.0) -> bool:
        pid = self.process.pid if self.process is not None else None
        diagnostic: dict[str, Any] = {
            "method": "CTRL_BREAK_EVENT",
            "pid": pid,
            "signal_available": _CTRL_BREAK_EVENT is not None,
            "signal_value": int(_CTRL_BREAK_EVENT) if _CTRL_BREAK_EVENT is not None else None,
            "timeout_seconds": timeout,
            "alive_before": self.is_alive(),
        }
        self.lifecycle_diagnostic = diagnostic
        if not diagnostic["alive_before"]:
            diagnostic["outcome"] = "already-exited"
            diagnostic["returncode"] = self.process.returncode if self.process is not None else None
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return True
        if _CTRL_BREAK_EVENT is None or self.process is None:
            diagnostic["outcome"] = "unsupported"
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return False
        try:
            self.process.send_signal(_CTRL_BREAK_EVENT)
            diagnostic["signal_sent"] = True
            self.process.wait(timeout=timeout)
            diagnostic["returncode"] = self.process.returncode
            diagnostic["outcome"] = "exited" if self.process.returncode == 0 else "nonzero-exit"
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return self.process.returncode == 0
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            diagnostic["outcome"] = "exception"
            diagnostic["exception_type"] = type(exc).__name__
            diagnostic["exception"] = str(exc)
            diagnostic["winerror"] = getattr(exc, "winerror", None)
            diagnostic["errno"] = getattr(exc, "errno", None)
            diagnostic["returncode"] = self.process.returncode
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return False


def _install_exact_artifacts(self: CombinedPackGameruleFleetValidation) -> None:
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


def _start_windows_headless_server(self: CombinedPackGameruleFleetValidation) -> None:
    """Run Endstone headlessly with process-group lifecycle control."""

    cmd = [
        sys.executable,
        "-m",
        "endstone",
        "--yes",
        "--no-interactive",
        "--server-folder",
        str(self.server_dir),
    ]
    self.server = _FrameworkShutdownServerProcess(cmd, self.root, self.log_path)
    self.server.start()
    self.server.wait_for(
        lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines),
        240,
        "BDS ready",
    )
    self.check("bds-start", "PASS")
    self.check("ready", "PASS")
    self.server.wait_for(
        lambda lines: any(
            "spark" in line.lower() and any(hint in line.lower() for hint in SPARK_LOAD_HINTS)
            for line in lines
        ),
        30,
        "Spark enable",
    )
    self.check("spark-load-enable", "PASS")
    self.check("windows-headless-lifecycle", "PASS", shutdown_control="CTRL_BREAK_EVENT")
    version_file = self.server_dir / "version.txt"
    if version_file.exists():
        self.result["bds_version"] = version_file.read_text(encoding="utf-8").strip()
        write_json(self.result_path, self.result)


def _start_exact_server(self: CombinedPackGameruleFleetValidation) -> None:
    if self.platform == "windows":
        _start_windows_headless_server(self)
    else:
        _ORIGINAL_START_SERVER(self)
    assert self.server is not None
    observed_protocol = validate_bds_version(self.result, self.server.snapshot())
    self.check(
        "exact-bds-version",
        "PASS",
        observed_protocol=observed_protocol,
        expected_protocol=os.environ.get("EXPECTED_BDS_PROTOCOL_VERSION", "").strip() or None,
        expected_full=os.environ.get("EXPECTED_BDS_VERSION", "").strip() or None,
    )


CombinedPackGameruleFleetValidation.install_artifacts = _install_exact_artifacts
CombinedPackGameruleFleetValidation.start_server = _start_exact_server

if __name__ == "__main__":
    raise SystemExit(main())
