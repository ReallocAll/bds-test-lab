#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import time
from typing import Any

from controller.bot_validation import list_players, patch_server_properties
from controller.fleet_spark_validation import set_server_property
from controller.run_test import IntegrationTest, write_json

POSITION_RE = re.compile(r'"(x|y|z)"\s*:\s*(-?[0-9]+(?:\.[0-9]+)?)')


def parse_querytarget(output: list[str]) -> list[float] | None:
    values: dict[str, float] = {}
    in_position = False
    for line in output:
        if '"position"' in line:
            in_position = True
            continue
        if in_position and "}" in line:
            break
        if in_position:
            match = POSITION_RE.search(line)
            if match:
                values[match.group(1)] = float(match.group(2))
    if all(axis in values for axis in ("x", "y", "z")):
        return [values["x"], values["y"], values["z"]]
    return None


class ReferenceMovementValidation(IntegrationTest):
    def __init__(self, binary: pathlib.Path, moves: int):
        super().__init__("linux")
        self.binary = binary.resolve()
        self.moves = moves
        self.reference_log = self.root / "reference-client.log"
        self.reference: subprocess.Popen[str] | None = None
        self.result.update({"test_kind": "reference-client-movement", "server_positions": []})
        write_json(self.result_path, self.result)

    def bootstrap(self) -> None:
        self.install_artifacts()
        self.start_server()
        assert self.server is not None
        self.server.wait_for(
            lambda lines: any("[spark] endstone-spark v" in line.lower() and "enabled. run /spark" in line.lower() for line in lines),
            30,
            "Spark post-start enable completion",
        )
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop for reference-client bootstrap")
        self.server.close()
        self.server = None
        props = self.server_dir / "server.properties"
        patch_server_properties(props)
        set_server_property(props, "max-players", "10")
        self.start_server()
        self.server.wait_for(
            lambda lines: any("[spark] endstone-spark v" in line.lower() and "enabled. run /spark" in line.lower() for line in lines),
            30,
            "Spark post-start enable completion",
        )

    def query(self, phase: str) -> list[float]:
        assert self.server is not None
        start = self.server.command("querytarget RefBot")
        output = self.server.wait_command_output(start, 8)
        position = parse_querytarget(output)
        self.result["server_positions"].append({"phase": phase, "position": position, "output": output[-20:]})
        write_json(self.result_path, self.result)
        if position is None:
            raise RuntimeError(f"Could not parse querytarget position: {output[-20:]}")
        return position

    def run_reference(self) -> None:
        assert self.server is not None
        log = self.reference_log.open("w", encoding="utf-8")
        cmd = [str(self.binary), "--address", "127.0.0.1:19132", "--name", "RefBot", "--bots", "1"]
        print("+", " ".join(cmd), flush=True)
        self.reference = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            output = list_players(self.server)
            if any("RefBot" in line for line in output):
                break
            if self.reference.poll() is not None:
                raise RuntimeError(f"reference client exited early: {self.reference.returncode}")
            time.sleep(0.5)
        else:
            raise RuntimeError("reference client did not appear in BDS /list")

        before = self.query("before")
        for index in range(self.moves):
            envelope = {
                "action": "moveRawInput",
                "parameters": {"forward": True},
                "id": f"m{index}",
                "timeoutMs": 2000,
            }
            message = "[RUN_ACTION]" + json.dumps(envelope, separators=(",", ":"))
            rawtext = json.dumps({"rawtext": [{"text": message}]}, separators=(",", ":"))
            self.server.command(f"tellraw RefBot {rawtext}")
            time.sleep(0.1)
        time.sleep(2)
        after = self.query("after")
        horizontal = ((after[0] - before[0]) ** 2 + (after[2] - before[2]) ** 2) ** 0.5
        self.result["horizontal_distance"] = horizontal
        write_json(self.result_path, self.result)
        if horizontal < 0.5:
            raise RuntimeError(f"reference client did not move server-side: before={before} after={after}")
        self.check("reference-server-movement", "PASS", f"BDS accepted reference PlayerAuthInput movement: {horizontal:.3f} blocks")

    def cleanup(self) -> None:
        if self.reference is not None and self.reference.poll() is None:
            self.reference.terminate()
            try:
                self.reference.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.reference.kill()
                self.reference.wait(timeout=5)
        if self.server is not None:
            if not self.server.graceful_stop(60):
                self.server.force_kill_tree()
            self.server.close()
            self.server = None

    def execute_reference(self) -> int:
        try:
            self.bootstrap()
            self.run_reference()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            write_json(self.result_path, self.result)
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"
            write_json(self.result_path, self.result)
            raise
        finally:
            self.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--moves", type=int, default=40)
    args = parser.parse_args()
    return ReferenceMovementValidation(pathlib.Path(args.binary), args.moves).execute_reference()


if __name__ == "__main__":
    raise SystemExit(main())
