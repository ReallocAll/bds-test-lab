from __future__ import annotations

import json
import pathlib
import subprocess
import sys

# Importing the exact runner installs the exact-provenance and Windows lifecycle
# overrides on CombinedPackGameruleFleetValidation.
from controller.combined_pack_gamerule_fleet_exact_runner import (
    CombinedPackGameruleFleetValidation,
    _FrameworkShutdownServerProcess,
)
from controller.run_test import READY_HINTS, SPARK_LOAD_HINTS, ServerProcess


def _interactive_shutdown(validator: CombinedPackGameruleFleetValidation) -> dict[str, object]:
    cmd = [
        sys.executable,
        "-m",
        "endstone",
        "--yes",
        "--server-folder",
        str(validator.server_dir),
    ]
    server = ServerProcess(cmd, validator.root, validator.log_path)
    diagnostic: dict[str, object] = {
        "method": "interactive-cishutdown",
        "cmd": cmd,
    }
    try:
        server.start()
        server.wait_for(
            lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines),
            240,
            "interactive BDS ready",
        )
        server.wait_for(
            lambda lines: any(
                "spark" in line.lower() and any(hint in line.lower() for hint in SPARK_LOAD_HINTS)
                for line in lines
            ),
            30,
            "interactive Spark enable",
        )
        server.wait_for(
            lambda lines: any("ci lifecycle control enabled; cishutdown registered" in line.lower() for line in lines),
            30,
            "interactive lifecycle plugin enable",
        )
        diagnostic["pid"] = server.process.pid if server.process is not None else None
        server.command("cishutdown")
        assert server.process is not None
        try:
            server.process.wait(timeout=20)
        except subprocess.TimeoutExpired as exc:
            diagnostic["outcome"] = "timeout"
            diagnostic["exception"] = str(exc)
            return diagnostic
        diagnostic["returncode"] = server.process.returncode
        diagnostic["outcome"] = "exited" if server.process.returncode == 0 else "nonzero-exit"
        return diagnostic
    except Exception as exc:  # noqa: BLE001 - preserve the exact runner-side failure for CI evidence
        diagnostic["outcome"] = "exception"
        diagnostic["exception_type"] = type(exc).__name__
        diagnostic["exception"] = str(exc)
        diagnostic["winerror"] = getattr(exc, "winerror", None)
        diagnostic["errno"] = getattr(exc, "errno", None)
        return diagnostic
    finally:
        if server.is_alive():
            server.force_kill_tree()
        server.close()


def main() -> int:
    validator = CombinedPackGameruleFleetValidation("windows", pathlib.Path("unused-bot.exe"), 30)
    try:
        validator.install_artifacts()
        validator.start_server()
        server = validator.server
        if not isinstance(server, _FrameworkShutdownServerProcess):
            raise RuntimeError(f"unexpected Windows server process type: {type(server).__name__}")
        ctrl_break_ok = server.graceful_stop(20)
        ctrl_break = dict(server.lifecycle_diagnostic)
        print(
            "WINDOWS_CTRL_BREAK_DIAGNOSTIC=" + json.dumps(ctrl_break, sort_keys=True, default=str),
            flush=True,
        )
        if server.is_alive():
            server.force_kill_tree()
        server.close()
        validator.server = None

        interactive = _interactive_shutdown(validator)
        print(
            "WINDOWS_INTERACTIVE_DIAGNOSTIC=" + json.dumps(interactive, sort_keys=True, default=str),
            flush=True,
        )
        interactive_ok = interactive.get("outcome") == "exited" and interactive.get("returncode") == 0
        print(
            "WINDOWS_LIFECYCLE_COMPARISON="
            + json.dumps(
                {
                    "ctrl_break_ok": ctrl_break_ok,
                    "interactive_cishutdown_ok": interactive_ok,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0 if ctrl_break_ok or interactive_ok else 1
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
