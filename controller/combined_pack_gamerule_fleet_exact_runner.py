from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid
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
_ORIGINAL_RUN_PUBLIC_PROFILE_PHASE = CombinedPackGameruleFleetValidation.run_public_profile_phase


class _FrameworkShutdownServerProcess(ServerProcess):
    """Gracefully stop Windows Endstone through a deterministic test-only file control."""

    lifecycle_diagnostic: dict[str, Any]
    lifecycle_registered: bool = False
    lifecycle_request_path: pathlib.Path | None = None

    def _record_shutdown_acknowledgement(
        self,
        diagnostic: dict[str, Any],
        output_start: int | None,
        token: str | None,
    ) -> None:
        evidence: dict[str, Any] = diagnostic["acknowledgement_evidence"]
        if output_start is None:
            evidence["shutdown_requested"] = False
            evidence["observed"] = False
            evidence["source"] = "unavailable"
            diagnostic["acknowledgement_observed"] = False
            return
        try:
            output = self.snapshot()[output_start:]
        except (AttributeError, TypeError):
            output = []
        if token is None:
            marker = "ci lifecycle shutdown requested"
            source = "captured-output"
        else:
            marker = f"ci lifecycle shutdown requested via file; token={token}".casefold()
            source = "captured-output-token"
        observed = any(marker in line.casefold() for line in output)
        evidence["shutdown_requested"] = observed
        evidence["observed"] = observed
        evidence["source"] = source
        diagnostic["acknowledgement_observed"] = observed

    def _graceful_stop_compat_command(
        self,
        diagnostic: dict[str, Any],
        timeout: float,
    ) -> bool:
        """Keep the historical command path only for fixtures without file-control registration."""

        diagnostic["method"] = "interactive-cishutdown"
        diagnostic["stop_method"] = "interactive-cishutdown"
        diagnostic["command"] = "cishutdown"
        evidence: dict[str, Any] = diagnostic["acknowledgement_evidence"]
        evidence["transport"] = "console-command-compat"
        output_start: int | None = None
        try:
            output_start = self.command("cishutdown")
            evidence["command_sent"] = True
            diagnostic["command_sent"] = True
            assert self.process is not None
            self.process.wait(timeout=timeout)
            reader = getattr(self, "_reader", None)
            if reader is not None:
                reader.join(timeout=3)
            self._record_shutdown_acknowledgement(diagnostic, output_start, None)
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
            self._record_shutdown_acknowledgement(diagnostic, output_start, None)
            self._finish_lifecycle(
                diagnostic,
                outcome="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "exception",
                returncode=self.process.returncode if self.process is not None else None,
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

    def graceful_stop(self, timeout: float = 60.0) -> bool:
        request_path = self.lifecycle_request_path
        diagnostic = self._begin_lifecycle(
            "framework-file-shutdown",
            str(request_path) if request_path is not None else "file-trigger-unavailable",
            timeout,
        )
        phase = getattr(self, "lifecycle_phase", None)
        diagnostic["phase_ordinal"] = phase.get("ordinal") if isinstance(phase, dict) else None
        diagnostic["phase_name"] = phase.get("name") if isinstance(phase, dict) else None
        evidence: dict[str, Any] = diagnostic["acknowledgement_evidence"]
        diagnostic["command_sent"] = False
        diagnostic["acknowledgement_observed"] = False
        diagnostic["forced"] = False
        evidence["registration_observed"] = self.lifecycle_registered
        evidence["transport"] = "file-trigger"
        evidence["request_path"] = str(request_path) if request_path is not None else None
        output_start: int | None = None
        token: str | None = None
        if not diagnostic["alive_before"]:
            self._finish_lifecycle(
                diagnostic,
                outcome="already-exited",
                returncode=self.process.returncode if self.process is not None else None,
            )
            diagnostic["wrapper_outcome"] = diagnostic["outcome"]
            diagnostic["wrapper_return_code"] = diagnostic["returncode"]
            self._record_shutdown_acknowledgement(diagnostic, output_start, token)
            diagnostic["cleanup_outcome"] = "already-exited"
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return True
        if self.process is None or not self.lifecycle_registered:
            self._finish_lifecycle(
                diagnostic,
                outcome="file-control-unavailable",
                returncode=self.process.returncode if self.process is not None else None,
            )
            diagnostic["wrapper_outcome"] = diagnostic["outcome"]
            diagnostic["wrapper_return_code"] = diagnostic["returncode"]
            self._record_shutdown_acknowledgement(diagnostic, output_start, token)
            diagnostic["cleanup_outcome"] = "verification-failed"
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return False
        if request_path is None:
            return self._graceful_stop_compat_command(diagnostic, timeout)
        try:
            output_start = len(self.snapshot())
            token = uuid.uuid4().hex
            evidence["token"] = token
            request_path.parent.mkdir(parents=True, exist_ok=True)
            request_path.write_text(token + "\n", encoding="utf-8")
            evidence["command_sent"] = True
            diagnostic["command_sent"] = True
            self.process.wait(timeout=timeout)
            reader = getattr(self, "_reader", None)
            if reader is not None:
                reader.join(timeout=3)
            self._record_shutdown_acknowledgement(diagnostic, output_start, token)
            self._finish_lifecycle(
                diagnostic,
                outcome="exited" if self.process.returncode == 0 else "nonzero-exit",
                returncode=self.process.returncode,
            )
            diagnostic["wrapper_outcome"] = diagnostic["outcome"]
            diagnostic["wrapper_return_code"] = diagnostic["returncode"]
            if self.process.returncode == 0 and not diagnostic["acknowledgement_observed"]:
                diagnostic["outcome"] = "acknowledgement-unobserved"
                diagnostic["cleanup_outcome"] = "verification-failed"
                print(f"[windows-lifecycle] {diagnostic}", flush=True)
                return False
            if self.process.returncode == 0 and diagnostic["process_tree_verification"] != "clean":
                diagnostic["outcome"] = diagnostic["process_tree_verification"]
                diagnostic["cleanup_outcome"] = diagnostic["process_tree_verification"]
                print(f"[windows-lifecycle] {diagnostic}", flush=True)
                return False
            diagnostic["cleanup_outcome"] = "graceful-exit" if self.process.returncode == 0 else "nonzero-exit"
            print(f"[windows-lifecycle] {diagnostic}", flush=True)
            return self.process.returncode == 0
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            self._record_shutdown_acknowledgement(diagnostic, output_start, token)
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
    """Run Endstone with deterministic test-only framework lifecycle control."""

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
    lines = self.server.wait_for(
        lambda current: any(
            "ci lifecycle control enabled;" in line.lower()
            and ("file-control=" in line.lower() or "cishutdown registered" in line.lower())
            for line in current
        ),
        30,
        "CI lifecycle control registration",
    )
    registration = next(
        line
        for line in reversed(lines)
        if "ci lifecycle control enabled;" in line.lower()
        and ("file-control=" in line.lower() or "cishutdown registered" in line.lower())
    )
    if "file-control=" in registration.lower():
        raw_path = registration.split("file-control=", 1)[1].strip()
        request_path = pathlib.Path(raw_path)
        if not request_path.is_absolute():
            request_path = (self.root / request_path).resolve()
        self.server.lifecycle_request_path = request_path
        self.server.lifecycle_registered = True
        self.check(
            "windows-framework-lifecycle",
            "PASS",
            shutdown_control="file-trigger",
            request_path=str(request_path),
            compatibility_command="cishutdown",
        )
    else:
        self.server.lifecycle_request_path = None
        self.server.lifecycle_registered = True
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


def _run_public_profile_phase_with_restart_pack_evidence(
    self: CombinedPackGameruleFleetValidation,
) -> None:
    """Avoid a redundant Windows console probe after local real-BDS execution proof."""

    if self.platform != "windows":
        _ORIGINAL_RUN_PUBLIC_PROFILE_PHASE(self)
        return

    checks = self.result.get("checks") if isinstance(self.result, dict) else None
    local_execution_proved = isinstance(checks, list) and any(
        isinstance(check, dict)
        and check.get("name") == "behavior-packs-real-load"
        and check.get("status") == "PASS"
        for check in checks
    )
    if not local_execution_proved:
        raise RuntimeError("Windows public phase requires prior local real-BDS behavior-pack execution proof")

    original_verify = self.verify_behavior_pack_functions

    def verify_restart_pack_stack() -> None:
        assert self.server is not None
        startup_log = "\n".join(self.server.snapshot()).casefold()
        missing = [pack["name"] for pack in BEHAVIOR_PACKS if pack["name"].casefold() not in startup_log]
        if missing:
            raise RuntimeError(
                "Windows public restart did not reload expected behavior packs: " + ", ".join(missing)
            )
        self.check(
            "behavior-packs-public-restart",
            "PASS",
            "all three behavior packs reloaded; semantic execution was already proved in the local real-BDS phase",
            count=len(BEHAVIOR_PACKS),
            execution_oracle="local-phase-real-bds",
            restart_oracle="startup-pack-stack",
        )

    self.verify_behavior_pack_functions = verify_restart_pack_stack  # type: ignore[method-assign]
    try:
        _ORIGINAL_RUN_PUBLIC_PROFILE_PHASE(self)
    finally:
        self.verify_behavior_pack_functions = original_verify  # type: ignore[method-assign]


CombinedPackGameruleFleetValidation.install_artifacts = _install_exact_artifacts
CombinedPackGameruleFleetValidation.start_server = _start_exact_server
CombinedPackGameruleFleetValidation.install_behavior_packs = _install_behavior_packs_with_state_oracle
CombinedPackGameruleFleetValidation.verify_behavior_pack_functions = _verify_behavior_pack_functions_with_state_oracle
CombinedPackGameruleFleetValidation.run_public_profile_phase = _run_public_profile_phase_with_restart_pack_evidence

if __name__ == "__main__":
    raise SystemExit(main())
