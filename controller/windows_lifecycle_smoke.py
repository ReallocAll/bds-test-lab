from __future__ import annotations

import json
import pathlib

# Importing the exact runner installs the exact-provenance and Windows lifecycle
# overrides on CombinedPackGameruleFleetValidation.
from controller.combined_pack_gamerule_fleet_exact_runner import CombinedPackGameruleFleetValidation

_EXPECTED_PHASES = ("bootstrap-provisioning", "bootstrap-world")


def main() -> int:
    validator = CombinedPackGameruleFleetValidation("windows", pathlib.Path("unused-bot.exe"), 30)
    try:
        validator.install_artifacts()
        validator.bootstrap_scenario_world()
        events = validator.result.get("shutdown_lifecycle_events") or []
        by_phase = {event.get("phase_name"): event for event in events if isinstance(event, dict)}
        for phase in _EXPECTED_PHASES:
            event = by_phase.get(phase)
            if not isinstance(event, dict):
                raise RuntimeError(f"missing Windows lifecycle phase evidence: {phase}")
            acknowledgement = event.get("acknowledgement_evidence") or {}
            if event.get("wrapper_outcome") != "exited" or event.get("wrapper_return_code") != 0:
                raise RuntimeError(f"non-graceful Windows lifecycle phase {phase}: {event}")
            if acknowledgement.get("transport") != "file-trigger" or acknowledgement.get("observed") is not True:
                raise RuntimeError(f"unverified Windows file-control acknowledgement for {phase}: {event}")
            if event.get("process_tree_verification") != "clean" or event.get("forced") is True:
                raise RuntimeError(f"unclean Windows lifecycle phase {phase}: {event}")
        print(
            "WINDOWS_REPEATED_LIFECYCLE_DIAGNOSTIC="
            + json.dumps([by_phase[phase] for phase in _EXPECTED_PHASES], sort_keys=True, default=str),
            flush=True,
        )
        return 0
    finally:
        if validator.server is not None:
            try:
                if validator.server.is_alive():
                    validator.server.force_kill_tree()
            finally:
                validator.server.close()
                validator.server = None


if __name__ == "__main__":
    raise SystemExit(main())
