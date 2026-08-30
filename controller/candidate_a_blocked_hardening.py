#!/usr/bin/env python3
"""Hardening layer for the pre-registered Candidate A controlled benchmark.

This module preserves the benchmark/statistical semantics in
``candidate_a_blocked_benchmark`` while fixing runner-state correctness:

* the measurement process is the Endstone ``python -m endstone`` root process;
* controller process/thread affinity is restored after every case, including
  partial failures;
* the runner CPU topology is fixed across all cases in a block;
* evidence upload is enabled only after every case prepared the exact Spark and
  Endstone artifacts successfully; and
* the final block manifest is rewritten after those checks are known.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any

import psutil

from controller import candidate_a_blocked_benchmark as base

ACTUAL_SCENARIO_SHA256 = "169360cb46acc6dc29ed5b38e082543b12434860bb65119c519a095de2a04799"
UPLOAD_GATE_NAME = ".candidate-a-upload-ok"

_BASE_CASE = base.CandidateABlockedCase
_BASE_RUN_CASE = base.run_case
_BASE_RUN_BLOCK = base.run_block
_EXPECTED_TOPOLOGY: dict[str, Any] | None = None


class RunnerStateError(base.AffinityError):
    """Raised when persistent runner state violates the controlled protocol."""


def runner_topology() -> dict[str, Any]:
    """Return the topology properties that must remain fixed across cases."""

    return {
        "allowed_cpus": base._sched_affinity(os.getpid()),
        "cpu_count": os.cpu_count(),
    }


def require_topology(expected: dict[str, Any], *, phase: str) -> dict[str, Any]:
    observed = runner_topology()
    if observed != expected:
        raise RunnerStateError(
            f"runner CPU topology drift during {phase}: expected={expected} observed={observed}"
        )
    return observed


def capture_controller_affinity_state() -> dict[str, Any]:
    """Capture the controller process and every currently-live thread mask."""

    pid = os.getpid()
    tids = base._linux_task_ids(pid)
    return {
        "pid": pid,
        "process_affinity": base._sched_affinity(pid),
        "tid_affinities": {str(tid): base._sched_affinity(tid) for tid in tids},
    }


def restore_controller_affinity_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Restore the controller and all live TIDs to their pre-case masks.

    Threads created during the case have no historical mask; they are restored
    to the original process mask. Threads that exited are recorded but are not
    treated as a restoration failure.
    """

    pid = int(snapshot.get("pid", 0))
    if pid != os.getpid():
        raise RunnerStateError(f"controller PID changed across case: {pid} != {os.getpid()}")
    process_affinity = [int(cpu) for cpu in snapshot.get("process_affinity", [])]
    if not process_affinity:
        raise RunnerStateError("controller pre-case process affinity is missing")
    raw_tid_affinities = snapshot.get("tid_affinities")
    if not isinstance(raw_tid_affinities, dict) or not raw_tid_affinities:
        raise RunnerStateError("controller pre-case per-TID affinity snapshot is missing")
    original = {
        int(raw_tid): [int(cpu) for cpu in cpus]
        for raw_tid, cpus in raw_tid_affinities.items()
    }

    # Repeat to catch threads created while restoration is in progress.
    seen_current: set[int] = set()
    for _attempt in range(8):
        current = base._linux_task_ids(pid)
        seen_current.update(current)
        for tid in current:
            target = original.get(tid, process_affinity)
            if base._sched_affinity(tid) != target:
                base._set_sched_affinity(tid, target)
            observed = base._sched_affinity(tid)
            if observed != target:
                raise RunnerStateError(
                    f"controller TID {tid} affinity restoration failed: {observed} != {target}"
                )
        if set(base._linux_task_ids(pid)) == set(current):
            break
    else:
        raise RunnerStateError("controller task set did not stabilize during affinity restoration")

    final_tids = base._linux_task_ids(pid)
    final_affinities: dict[str, list[int]] = {}
    for tid in final_tids:
        target = original.get(tid, process_affinity)
        observed = base._sched_affinity(tid)
        if observed != target:
            raise RunnerStateError(
                f"controller TID {tid} remains on {observed}; expected restored mask {target}"
            )
        final_affinities[str(tid)] = observed

    return {
        "restored": True,
        "pid": pid,
        "process_affinity_before": process_affinity,
        "original_tid_affinities": {str(tid): cpus for tid, cpus in sorted(original.items())},
        "final_tid_affinities": final_affinities,
        "new_tids_restored_to_process_mask": sorted(set(final_tids) - set(original)),
        "exited_tids": sorted(set(original) - set(final_tids)),
        "seen_current_tids": sorted(seen_current),
    }


def validate_endstone_root_process(process: psutil.Process) -> dict[str, Any]:
    """Require the managed measurement root to be this Python running Endstone."""

    try:
        pid = int(process.pid)
        executable = pathlib.Path(process.exe()).resolve()
        cmdline = [str(value) for value in process.cmdline()]
        create_time = float(process.create_time())
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError) as exc:
        raise RunnerStateError(f"unable to inspect Endstone root process: {exc}") from exc

    expected_executable = pathlib.Path(sys.executable).resolve()
    if executable != expected_executable:
        raise RunnerStateError(
            f"Endstone root executable mismatch: {executable} != {expected_executable}"
        )
    try:
        module_index = cmdline.index("-m")
    except ValueError as exc:
        raise RunnerStateError(f"Endstone root command is not a Python module invocation: {cmdline}") from exc
    if module_index + 1 >= len(cmdline) or cmdline[module_index + 1] != "endstone":
        raise RunnerStateError(f"Endstone root command is not 'python -m endstone': {cmdline}")

    return {
        "pid": pid,
        "executable": str(executable),
        "expected_executable": str(expected_executable),
        "cmdline": cmdline,
        "create_time": create_time,
        "validated_python_module": "endstone",
    }


class HardenedCandidateABlockedCase(_BASE_CASE):
    """Candidate A case with authoritative Endstone-root process identity."""

    def _measurement_process(self) -> psutil.Process:
        if self.server is None or self.server.process is None:
            raise RunnerStateError("Endstone server root process is unavailable")
        process = psutil.Process(self.server.process.pid)
        identity = validate_endstone_root_process(process)
        self.result["measurement_process_identity"] = identity
        self.protocol["measurement_process_identity"] = identity
        return process


def _write_case_result(case_dir: pathlib.Path, result: dict[str, Any]) -> None:
    base.write_json(case_dir / "candidate-a-blocked-result.json", result)


def hardened_run_case(**kwargs: Any) -> tuple[int, dict[str, Any]]:
    """Run one case while making controller affinity a transactional resource."""

    global _EXPECTED_TOPOLOGY
    if _EXPECTED_TOPOLOGY is None:
        raise RunnerStateError("block topology contract was not initialized")
    expected = dict(_EXPECTED_TOPOLOGY)
    topology_before = require_topology(expected, phase="before-case")
    snapshot = capture_controller_affinity_state()

    primary_error: BaseException | None = None
    code: int | None = None
    result: dict[str, Any] | None = None
    try:
        code, result = _BASE_RUN_CASE(**kwargs)
    except BaseException as exc:  # preserve restoration for constructor/launch failures too
        primary_error = exc

    restore_error: BaseException | None = None
    restoration: dict[str, Any] | None = None
    try:
        restoration = restore_controller_affinity_state(snapshot)
        topology_after = require_topology(expected, phase="after-case-restoration")
    except BaseException as exc:
        restore_error = exc
        topology_after = runner_topology()

    if primary_error is not None:
        if restore_error is not None:
            raise RunnerStateError(
                f"case failed with {type(primary_error).__name__}: {primary_error}; "
                f"affinity restoration also failed with {type(restore_error).__name__}: {restore_error}"
            ) from primary_error
        raise primary_error

    assert code is not None and result is not None
    result["runner_topology"] = {
        "expected": expected,
        "before_case": topology_before,
        "after_case": topology_after,
        "stable": restore_error is None,
    }
    if restoration is not None:
        result["affinity_restoration"] = restoration
    if restore_error is not None:
        code = 1
        result["status"] = "FAIL"
        result["state"] = "failed"
        result["failed_stage"] = "affinity-restore"
        result["affinity_restore_error"] = f"{type(restore_error).__name__}: {restore_error}"
    protocol = result.get("protocol")
    if isinstance(protocol, dict):
        protocol["runner_topology"] = result["runner_topology"]
        protocol["affinity_restoration"] = result.get("affinity_restoration")
    _write_case_result(pathlib.Path(kwargs["case_dir"]), result)
    return code, result


def _case_artifacts_are_exact(result: dict[str, Any], treatment: str) -> tuple[bool, str]:
    expected_spark = base.BASELINE_SHA if treatment.endswith("-B") else base.CANDIDATE_SHA
    metadata = result.get("artifact_metadata")
    components = metadata.get("components") if isinstance(metadata, dict) else None
    spark = components.get("spark") if isinstance(components, dict) else None
    endstone = components.get("endstone") if isinstance(components, dict) else None
    if not isinstance(spark, dict) or str(spark.get("sha", "")).lower() != expected_spark:
        return False, f"{treatment}: exact Spark artifact was not prepared"
    if not isinstance(endstone, dict) or str(endstone.get("sha", "")).lower() != base.ENDSTONE_SHA:
        return False, f"{treatment}: exact Endstone artifact was not prepared"
    protocol = result.get("protocol")
    endstone_artifact = protocol.get("endstone_artifact") if isinstance(protocol, dict) else None
    if not isinstance(endstone_artifact, dict):
        return False, f"{treatment}: Endstone artifact identity is missing"
    if str(endstone_artifact.get("sha", "")).lower() != base.ENDSTONE_SHA:
        return False, f"{treatment}: Endstone artifact identity drifted"
    return True, "exact artifacts prepared"


def evaluate_upload_gate(block_dir: pathlib.Path, schedule: tuple[str, ...]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    safe = True
    for treatment in schedule:
        result_path = block_dir / treatment / "candidate-a-blocked-result.json"
        if not result_path.is_file():
            safe = False
            checks.append({"treatment": treatment, "safe": False, "reason": "case result is missing"})
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            safe = False
            checks.append(
                {"treatment": treatment, "safe": False, "reason": f"case result unreadable: {exc}"}
            )
            continue
        case_safe, reason = _case_artifacts_are_exact(result, treatment)
        safe = safe and case_safe
        checks.append({"treatment": treatment, "safe": case_safe, "reason": reason})
    return {"safe": safe, "checks": checks}


def hardened_run_block(**kwargs: Any) -> int:
    """Run a block with fixed topology and emit an explicit artifact-upload gate."""

    global _EXPECTED_TOPOLOGY
    if base.BOT_SCENARIO_SHA256 != ACTUAL_SCENARIO_SHA256:
        raise base.BenchmarkConfigurationError(
            f"registered stationary scenario SHA is stale: {base.BOT_SCENARIO_SHA256} != {ACTUAL_SCENARIO_SHA256}"
        )

    evidence_root = pathlib.Path(kwargs["evidence_root"])
    block_index = int(kwargs["block_index"])
    block_dir = evidence_root / f"block-{block_index:02d}"
    gate_path = evidence_root / UPLOAD_GATE_NAME
    gate_path.unlink(missing_ok=True)

    expected = runner_topology()
    if len(expected["allowed_cpus"]) < 2:
        raise RunnerStateError(f"runner exposes fewer than two CPUs: {expected}")
    _EXPECTED_TOPOLOGY = expected
    try:
        code = _BASE_RUN_BLOCK(**kwargs)
    finally:
        _EXPECTED_TOPOLOGY = None

    topology_after_block = runner_topology()
    topology_stable = topology_after_block == expected
    if not topology_stable:
        code = 1

    schedule = base.block_schedule(block_index)
    gate = evaluate_upload_gate(block_dir, schedule)
    gate["runner_topology_expected"] = expected
    gate["runner_topology_after_block"] = topology_after_block
    gate["runner_topology_stable"] = topology_stable
    gate["scenario_sha256"] = ACTUAL_SCENARIO_SHA256
    gate["eligible"] = bool(gate["safe"] and topology_stable)

    manifest_path = block_dir / "candidate-a-blocked-block.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except (OSError, json.JSONDecodeError):
            manifest = {}
    manifest["runner_topology_contract"] = {
        "expected": expected,
        "after_block": topology_after_block,
        "stable": topology_stable,
    }
    manifest["artifact_upload_gate"] = gate
    manifest["status"] = "PASS" if code == 0 else "FAIL"
    base.write_json(manifest_path, manifest)

    if gate["eligible"]:
        gate_path.write_text(
            json.dumps(
                {
                    "block_index": block_index,
                    "eligible": True,
                    "scenario_sha256": ACTUAL_SCENARIO_SHA256,
                    "runner_topology": expected,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return code


def install_hardening() -> None:
    """Patch only the runner hooks; benchmark and analyzer semantics stay fixed."""

    base.CandidateABlockedCase = HardenedCandidateABlockedCase
    base.run_case = hardened_run_case
    base.run_block = hardened_run_block


def main() -> int:
    install_hardening()
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
