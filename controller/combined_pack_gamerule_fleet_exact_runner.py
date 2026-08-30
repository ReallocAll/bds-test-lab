from __future__ import annotations

import os
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


class _FrameworkShutdownServerProcess(ServerProcess):
    """Gracefully stop the Windows Endstone root through its interactive command map."""

    lifecycle_diagnostic: dict[str, Any]

    def graceful_stop(self, timeout: float = 60.0) -> bool:
        pid = self.process.pid if self.process is not None else None
        diagnostic: dict[str, Any] = {
            "method": "interactive-cishutdown",
            "command": "cishutdown",
            "pid": pid,
            "timeout_seconds": timeout,
            "alive_before": self.is_alive(),
        }
        self.lifecycle_diagnostic = diagnostic
        if not diagnostic["alive_before"]:
            diagnostic["outcome"] = "already-exited"
            diagnostic["returncode"] = self.process.returncode if self.process is not None else None
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return True
        if self.process is None:
            diagnostic["outcome"] = "missing-process"
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return False
        try:
            self.command("cishutdown")
            diagnostic["command_sent"] = True
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


def _start_windows_interactive_server(self: CombinedPackGameruleFleetValidation) -> None:
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
    self.server.wait_for(
        lambda lines: any("ci lifecycle control enabled; cishutdown registered" in line.lower() for line in lines),
        30,
        "CI lifecycle command registration",
    )
    self.check("windows-interactive-lifecycle", "PASS", shutdown_control="cishutdown")
    version_file = self.server_dir / "version.txt"
    if version_file.exists():
        self.result["bds_version"] = version_file.read_text(encoding="utf-8").strip()
        write_json(self.result_path, self.result)


def _start_exact_server(self: CombinedPackGameruleFleetValidation) -> None:
    if self.platform == "windows":
        _start_windows_interactive_server(self)
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
