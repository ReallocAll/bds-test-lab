#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import pathlib
import subprocess
import time
import traceback

from controller.movement_bds_only_validation import MovementBDSOnlyValidation
from controller.run_test import now_iso


class ReferenceMovementValidation(MovementBDSOnlyValidation):
    def __init__(self, reference_binary: pathlib.Path):
        super().__init__(reference_binary, "reference-walk")
        self.reference_binary = reference_binary.resolve()
        self.reference_process: subprocess.Popen[str] | None = None
        self.reference_log_handle = None
        self.reference_log = self.root / "reference-go-test-bds.log"
        self.result.update(
            {
                "test_kind": "bds-only-reference-client-movement",
                "reference_commit": "5fe6b762fe3665caa980a2a25be1ede26e5793fc",
                "reference_log": self.reference_log.name,
                "raw_input_commands_sent": 0,
            }
        )
        self._write_results()

    def start_fleet(self) -> None:
        runtime = self.root / "reference-runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.reference_log_handle = self.reference_log.open("w", encoding="utf-8")
        self.reference_process = subprocess.Popen(
            [
                str(self.reference_binary),
                "--address",
                "127.0.0.1:19132",
                "--name",
                "TestBot",
                "--bots",
                "1",
                "--viewer=false",
                "--log-level=debug",
            ],
            cwd=runtime,
            stdout=self.reference_log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.reference_process.poll() is not None:
                raise RuntimeError(f"reference client exited during login with code {self.reference_process.returncode}")
            try:
                self.wait_player_count(1, 2)
                self.check("reference-client-online", "PASS")
                return
            except RuntimeError:
                if self.server is not None and not self.server.is_alive():
                    raise RuntimeError("BDS exited while reference client was logging in")
            time.sleep(0.5)
        raise RuntimeError("reference client did not appear in BDS player list")

    def validate_authoritative_movement(self) -> None:
        assert self.server is not None
        assert self.reference_process is not None

        # Avoid pathfinding and server-side teleport state entirely.  The point
        # of this control is only to prove that the known headless client can
        # make BDS accept PlayerAuthInput movement.  Repeated moveRawInput
        # instructions feed one forward input into successive client ticks.
        time.sleep(5.0)
        before = self.query_position()
        commands = 120
        for index in range(commands):
            if self.reference_process.poll() is not None:
                raise RuntimeError(f"reference client exited during raw movement with code {self.reference_process.returncode}")
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during reference raw movement")
            action = {
                "action": "moveRawInput",
                "parameters": {"forward": True},
                "id": f"diag-move-{index:03d}",
                "timeoutMs": 2000,
            }
            message = "[RUN_ACTION]" + json.dumps(action, separators=(",", ":"))
            rawtext = json.dumps({"rawtext": [{"text": message}]}, separators=(",", ":"))
            self.server.command(f"tellraw TestBot {rawtext}")
            self.result["raw_input_commands_sent"] = index + 1
            time.sleep(0.06)

        time.sleep(2.0)
        after = self.query_position()
        displacement = math.hypot(after[0] - before[0], after[2] - before[2])
        self.result["server_position_before"] = before
        self.result["server_position_after"] = after
        self.result["server_horizontal_displacement"] = displacement
        self._write_results()
        if displacement <= 8.0:
            raise RuntimeError(
                f"reference client did not move authoritatively after {commands} raw inputs: "
                f"before={before}, after={after}, horizontal={displacement:.3f}"
            )
        self.check(
            "reference-server-authoritative-movement",
            "PASS",
            f"go-test-bds querytarget horizontal displacement={displacement:.3f}",
            before=before,
            after=after,
            raw_input_commands_sent=commands,
        )

    def stop_fleet(self) -> None:
        if self.reference_process is None:
            return
        if self.reference_process.poll() is None:
            self.reference_process.terminate()
            try:
                self.reference_process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.reference_process.kill()
                self.reference_process.wait(timeout=5)
                raise RuntimeError("reference client did not terminate gracefully")
        if self.reference_process.returncode not in (0, -15):
            raise RuntimeError(f"reference client exited with code {self.reference_process.returncode}")
        if self.reference_log_handle is not None:
            self.reference_log_handle.close()
            self.reference_log_handle = None
        self.wait_player_count(0, 30)
        self.check("reference-client-shutdown", "PASS")

    def execute(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_endstone_only()
            stage = "bds-bootstrap"
            self.bootstrap_offline_server()
            stage = "reference-connect"
            self.start_fleet()
            stage = "reference-navigation"
            self.validate_authoritative_movement()
            stage = "reference-disconnect"
            self.stop_fleet()
            stage = "shutdown"
            self.shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self._write_results()
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1200]
            diagnostic = traceback.format_exc()
            try:
                if self.reference_process is not None and self.reference_process.poll() is None:
                    self.reference_process.kill()
                    self.reference_process.wait(timeout=5)
                if self.reference_log_handle is not None:
                    self.reference_log_handle.close()
                    self.reference_log_handle = None
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:
                diagnostic += "\n\nCleanup failure:\n" + traceback.format_exc()
            last_lines = self.server.snapshot()[-300:] if self.server is not None else []
            self.diagnostics.write_text(diagnostic + "\n\nLast BDS log lines:\n" + "\n".join(last_lines), encoding="utf-8")
            self._write_results()
            return 1
        finally:
            if self.reference_process is not None and self.reference_process.poll() is None:
                self.reference_process.kill()
            if self.reference_log_handle is not None:
                self.reference_log_handle.close()
            self.result["completed_at"] = now_iso()
            self._write_results()
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-bot", required=True)
    args = parser.parse_args()
    return ReferenceMovementValidation(pathlib.Path(args.reference_bot)).execute()


if __name__ == "__main__":
    raise SystemExit(main())
