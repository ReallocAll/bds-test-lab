from __future__ import annotations

import json
import pathlib

# Importing the exact runner installs the exact-provenance and Windows lifecycle
# overrides on CombinedPackGameruleFleetValidation.
from controller.combined_pack_gamerule_fleet_exact_runner import (  # noqa: F401
    CombinedPackGameruleFleetValidation,
    _FrameworkShutdownServerProcess,
)


def main() -> int:
    validator = CombinedPackGameruleFleetValidation("windows", pathlib.Path("unused-bot.exe"), 30)
    try:
        validator.install_artifacts()
        validator.start_server()
        server = validator.server
        if not isinstance(server, _FrameworkShutdownServerProcess):
            raise RuntimeError(f"unexpected Windows server process type: {type(server).__name__}")
        ok = server.graceful_stop(20)
        print(
            "WINDOWS_LIFECYCLE_DIAGNOSTIC="
            + json.dumps(server.lifecycle_diagnostic, sort_keys=True, default=str),
            flush=True,
        )
        if not ok:
            server.force_kill_tree()
            return 1
        server.close()
        validator.server = None
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
