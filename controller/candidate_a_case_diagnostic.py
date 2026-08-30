"""Run one non-admissible Candidate A case and print its hidden failure state.

This helper is diagnostic-only. It deliberately does not create the formal
``.candidate-a-upload-ok`` marker and its output must never be consumed by the
cumulative stopping-rule analyzer.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
from typing import Any

from controller import candidate_a_blocked_benchmark as base
from controller import candidate_a_blocked_hardening as hardening

DIAGNOSTIC_FILE = "candidate-a-invalid-diagnostic.json"


def _summary(result: dict[str, Any], *, code: int, topology: dict[str, Any]) -> dict[str, Any]:
    performance = result.get("performance")
    return {
        "admissible": False,
        "formal_upload_marker_created": False,
        "exit_code": code,
        "status": result.get("status"),
        "state": result.get("state"),
        "failed_stage": result.get("failed_stage"),
        "error_summary": result.get("error_summary"),
        "affinity_restore_error": result.get("affinity_restore_error"),
        "runner_topology": result.get("runner_topology"),
        "affinity_restoration": result.get("affinity_restoration"),
        "counter_windows": performance.get("counter_windows") if isinstance(performance, dict) else None,
        "artifact_metadata": result.get("artifact_metadata"),
        "measurement_process_identity": result.get("measurement_process_identity"),
        "diagnostic_runner_topology": topology,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--evidence-root", default="evidence-diagnostic")
    parser.add_argument("--treatment", default="off-B", choices=("off-B", "off-C", "full-B", "full-C"))
    args = parser.parse_args()

    scenario_path = pathlib.Path(os.environ.get(base.SCENARIO_FILE_ENV, "").strip()).resolve()
    scenario_contract = base._scenario_contract(scenario_path)
    if scenario_contract.get("sha256") != hardening.ACTUAL_SCENARIO_SHA256:
        raise base.BenchmarkConfigurationError(
            f"diagnostic scenario SHA mismatch: {scenario_contract.get('sha256')} != "
            f"{hardening.ACTUAL_SCENARIO_SHA256}"
        )
    scenario_contract["path"] = str(scenario_path)
    # run_case changes cwd to the case directory. Match the formal run_block
    # contract by freezing the scenario environment variable to its absolute
    # repository path before entering that working directory.
    os.environ[base.SCENARIO_FILE_ENV] = str(scenario_path)

    evidence_root = pathlib.Path(args.evidence_root).resolve()
    case_dir = evidence_root / args.treatment
    evidence_root.mkdir(parents=True, exist_ok=True)

    hardening.install_hardening()
    topology = hardening.runner_topology()
    hardening._EXPECTED_TOPOLOGY = topology
    try:
        code, result = hardening.hardened_run_case(
            case_dir=case_dir,
            platform_name="linux",
            bot_binary=pathlib.Path(args.bot).resolve(),
            block_index=1,
            position=0,
            treatment=args.treatment,
            baseline_sha=base.BASELINE_SHA,
            candidate_sha=base.CANDIDATE_SHA,
            bot_ref=base.BOT_REF,
            scenario_contract=scenario_contract,
        )
    finally:
        hardening._EXPECTED_TOPOLOGY = None

    diagnostic = _summary(result, code=code, topology=topology)
    diagnostic_path = evidence_root / DIAGNOSTIC_FILE
    base.write_json(diagnostic_path, diagnostic)
    print("Candidate A diagnostic result (NON-ADMISSIBLE):", flush=True)
    print(json.dumps(diagnostic, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
