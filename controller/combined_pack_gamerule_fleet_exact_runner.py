from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from controller.combined_pack_gamerule_fleet_validation import (
    BEHAVIOR_PACKS,
    CombinedPackGameruleFleetValidation,
    main,
)
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

BEHAVIOR_PACK_STATE_RULE = "spawnradius"
BEHAVIOR_PACK_STATE_BASE = 21

_ORIGINAL_INSTALL_ARTIFACTS = CombinedPackGameruleFleetValidation.install_artifacts
_ORIGINAL_START_SERVER = CombinedPackGameruleFleetValidation.start_server
_ORIGINAL_INSTALL_BEHAVIOR_PACKS = CombinedPackGameruleFleetValidation.install_behavior_packs


class _FrameworkShutdownServerProcess(ServerProcess):
    """Gracefully stop the Windows Endstone root through its interactive command map."""

    lifecycle_diagnostic: dict[str, Any]
    lifecycle_registered: bool = False

    def _record_shutdown_acknowledgement(self, diagnostic: dict[str, Any], command_start: int | None) -> None:
        evidence: dict[str, Any] = diagnostic["acknowledgement_evidence"]
        if command_start is None:
            evidence["shutdown_requested"] = False
            evidence["observed"] = False
            evidence["source"] = "unavailable"
            diagnostic["acknowledgement_observed"] = False
            return
        try:
            command_output = self.snapshot()[command_start:]
        except (AttributeError, TypeError):
            command_output = []
        observed = any("ci lifecycle shutdown requested" in line.lower() for line in command_output)
        evidence["shutdown_requested"] = observed
        evidence["observed"] = observed
        evidence["source"] = "captured-output"
        diagnostic["acknowledgement_observed"] = observed

    def graceful_stop(self, timeout: float = 60.0) -> bool:
        diagnostic = self._begin_lifecycle("interactive-cishutdown", "cishutdown", timeout)
        phase = getattr(self, "lifecycle_phase", None)
        diagnostic["phase_ordinal"] = phase.get("ordinal") if isinstance(phase, dict) else None
        diagnostic["phase_name"] = phase.get("name") if isinstance(phase, dict) else None
        evidence: dict[str, Any] = diagnostic["acknowledgement_evidence"]
        diagnostic["command_sent"] = False
        diagnostic["acknowledgement_observed"] = False
        diagnostic["forced"] = False
        evidence["registration_observed"] = self.lifecycle_registered
        command_start: int | None = None
        if not diagnostic["alive_before"]:
            self._finish_lifecycle(
                diagnostic,
                outcome="already-exited",
                returncode=self.process.returncode if self.process is not None else None,
            )
            diagnostic["wrapper_outcome"] = diagnostic["outcome"]
            diagnostic["wrapper_return_code"] = diagnostic["returncode"]
            self._record_shutdown_acknowledgement(diagnostic, command_start)
            diagnostic["cleanup_outcome"] = "already-exited"
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return True
        if self.process is None:
            self._finish_lifecycle(diagnostic, outcome="missing-process")
            diagnostic["wrapper_outcome"] = diagnostic["outcome"]
            diagnostic["wrapper_return_code"] = diagnostic["returncode"]
            self._record_shutdown_acknowledgement(diagnostic, command_start)
            diagnostic["cleanup_outcome"] = "verification-failed"
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return False
        try:
            command_result = self.command("cishutdown")
            if isinstance(command_result, int):
                command_start = command_result
            evidence["command_sent"] = True
            diagnostic["command_sent"] = True
            self.process.wait(timeout=timeout)
            self._record_shutdown_acknowledgement(diagnostic, command_start)
            self._finish_lifecycle(
                diagnostic,
                outcome="exited" if self.process.returncode == 0 else "nonzero-exit",
                returncode=self.process.returncode,
            )
            diagnostic["wrapper_outcome"] = diagnostic["outcome"]
            diagnostic["wrapper_return_code"] = diagnostic["returncode"]
            diagnostic["cleanup_outcome"] = "graceful-exit" if self.process.returncode == 0 else "nonzero-exit"
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return self.process.returncode == 0
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            self._record_shutdown_acknowledgement(diagnostic, command_start)
            self._finish_lifecycle(
                diagnostic,
                outcome="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "exception",
                returncode=self.process.returncode,
                timeout_reason=(
                    f"process did not exit within {timeout:.1f}s" if isinstance(exc, subprocess.TimeoutExpired) else None
                ),
            )
            diagnostic["wrapper_outcome"] = diagnostic["outcome"]
            diagnostic["wrapper_return_code"] = diagnostic["returncode"]
            diagnostic["cleanup_outcome"] = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "command-failed"
            diagnostic["exception_type"] = type(exc).__name__
            diagnostic["exception"] = str(exc)
            diagnostic["winerror"] = getattr(exc, "winerror", None)
            diagnostic["errno"] = getattr(exc, "errno", None)
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return False


def _install_exact_artifacts(self: CombinedPackGameruleFleetValidation) -> None:
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
    IntegrationTest._prepare_bstats_before_start(self)
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
    self.server.lifecycle_registered = True
    readiness_start = self.server.command("spark tps")
    readiness_lines = self.server.wait_for(
        lambda lines: any(
            "tps (5s/10s/1m/5m/15m):" in line.lower() for line in lines[readiness_start:]
        ),
        15,
        "interactive command processing readiness",
    )
    self.check(
        "windows-interactive-command-ready",
        "PASS",
        probe="spark tps",
        output=readiness_lines[readiness_start:][-20:],
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


def _state_value(index: int) -> str:
    return str(BEHAVIOR_PACK_STATE_BASE + index)


def _install_behavior_packs_with_state_oracle(
    self: CombinedPackGameruleFleetValidation,
    world_dir: Any,
) -> None:
    """Keep the visible marker and add a deterministic Bedrock state transition."""

    _ORIGINAL_INSTALL_BEHAVIOR_PACKS(self, world_dir)
    for index, pack in enumerate(BEHAVIOR_PACKS):
        function_path = (
            self.server_dir
            / "behavior_packs"
            / pack["directory"]
            / "functions"
            / f"{pack['function']}.mcfunction"
        )
        value = _state_value(index)
        function_path.write_text(
            f"say {pack['marker']}\n"
            f"gamerule {BEHAVIOR_PACK_STATE_RULE} {value}\n",
            encoding="utf-8",
        )
        if isinstance(self.result.get("behavior_packs"), list):
            for evidence in self.result["behavior_packs"]:
                if evidence.get("function") == pack["function"]:
                    evidence["execution_oracle"] = {
                        "kind": "gamerule-state",
                        "rule": BEHAVIOR_PACK_STATE_RULE,
                        "expected": value,
                    }
                    break
    self._write_results()


def _parse_state_value(lines: list[str]) -> str | None:
    rule = BEHAVIOR_PACK_STATE_RULE.casefold()
    for line in reversed(lines):
        lowered = line.strip().casefold()
        if rule not in lowered:
            continue
        for separator in (" is currently set to ", " is set to ", " = "):
            if separator not in lowered:
                continue
            value = lowered.rsplit(separator, 1)[1].strip().split()[0] if lowered.rsplit(separator, 1)[1].strip() else ""
            if value:
                return value.rstrip(".,")
    return None


def _verify_behavior_pack_functions_with_state_oracle(self: CombinedPackGameruleFleetValidation) -> None:
    observations: list[dict[str, str]] = []
    for index, pack in enumerate(BEHAVIOR_PACKS):
        output = self.command_check(
            f"behavior-pack-function-{pack['function']}",
            f"execute run function {pack['function']}",
        )
        joined = "\n".join(output).casefold()
        rejected = ("unknown function", "function not found", "failed to execute", "syntax error")
        if any(marker in joined for marker in rejected):
            raise RuntimeError(
                f"BDS rejected behavior-pack function {pack['function']!r}: " + " | ".join(output[-30:])
            )

        expected = _state_value(index)
        if pack["marker"].casefold() in joined:
            observations.append(
                {
                    "function": pack["function"],
                    "oracle": "console-marker",
                    "expected": pack["marker"],
                    "observed": pack["marker"],
                }
            )
            continue

        state_output = self.command_check(
            f"behavior-pack-state-{pack['function']}",
            f"gamerule {BEHAVIOR_PACK_STATE_RULE}",
        )
        observed = _parse_state_value(state_output)
        if observed != expected:
            raise RuntimeError(
                f"behavior-pack function {pack['function']!r} executed without expected marker "
                f"{pack['marker']!r} and did not produce expected server state "
                f"{BEHAVIOR_PACK_STATE_RULE}={expected!r}; observed={observed!r}: "
                + " | ".join(state_output[-30:])
            )
        observations.append(
            {
                "function": pack["function"],
                "oracle": "gamerule-state",
                "expected": expected,
                "observed": observed,
            }
        )

    if isinstance(self.result, dict):
        self.result["behavior_pack_execution_oracle"] = observations
        self._write_results()
    self.check(
        "behavior-packs-real-load",
        "PASS",
        "all three behavior-pack functions executed inside real BDS",
        count=len(BEHAVIOR_PACKS),
    )


CombinedPackGameruleFleetValidation.install_artifacts = _install_exact_artifacts
CombinedPackGameruleFleetValidation.start_server = _start_exact_server
CombinedPackGameruleFleetValidation.install_behavior_packs = _install_behavior_packs_with_state_oracle
CombinedPackGameruleFleetValidation.verify_behavior_pack_functions = _verify_behavior_pack_functions_with_state_oracle

if __name__ == "__main__":
    raise SystemExit(main())
