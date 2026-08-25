#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import signal
import subprocess
import sys
import threading
import time
import traceback
from typing import Any

from controller.run_test import READY_HINTS, ServerProcess, locate_one, now_iso, run_checked
from providers.artifact_provider import _download_artifact, discover

RECOMMENDED_PROPERTIES = {
    "online-mode": "false",
    "allow-list": "false",
    "player-idle-timeout": "0",
    "gamemode": "creative",
    "force-gamemode": "true",
    "difficulty": "peaceful",
    "allow-cheats": "true",
    "default-player-permission-level": "operator",
    "view-distance": "8",
    "tick-distance": "4",
    "client-side-chunk-generation-enabled": "false",
}


class BotProcess:
    def __init__(self, binary: pathlib.Path, log_path: pathlib.Path):
        self.binary = binary.resolve()
        self.log_path = log_path
        self.process: subprocess.Popen[str] | None = None
        self.events: list[dict[str, Any]] = []
        self.lines: list[str] = []
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._log = None

    def start(self) -> None:
        cmd = [
            str(self.binary),
            "--host",
            "127.0.0.1",
            "--port",
            "19132",
            "--name",
            "TestBot",
            "--chunk-radius",
            "8",
            "--connect-timeout",
            "15s",
            "--spawn-timeout",
            "30s",
            "--json",
        ]
        print("+", " ".join(cmd), flush=True)
        self._log = self.log_path.open("w", encoding="utf-8")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._read_loop, name="bot-log-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for raw in self.process.stdout:
            line = raw.rstrip("\r\n")
            print(f"[bot] {line}", flush=True)
            if self._log is not None:
                self._log.write(raw)
                self._log.flush()
            event: dict[str, Any] | None = None
            try:
                decoded = json.loads(line)
                if isinstance(decoded, dict):
                    event = decoded
            except json.JSONDecodeError:
                pass
            with self._lock:
                self.lines.append(line)
                if event is not None:
                    self.events.append(event)

    def event_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.events)

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def wait_event(self, event_name: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.event_snapshot():
                if event.get("event") == event_name:
                    return event
            if not self.is_alive():
                assert self.process is not None
                raise RuntimeError(f"Bot exited with code {self.process.returncode} before event={event_name}")
            time.sleep(0.25)
        raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for bot event={event_name}")

    def terminate(self, timeout: float = 15.0) -> int:
        if self.process is None:
            return 0
        if self.process.poll() is None:
            termination_signal = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM
            self.process.send_signal(termination_signal)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                raise RuntimeError("Bot did not exit after graceful termination signal")
        code = int(self.process.returncode or 0)
        if self._reader is not None:
            self._reader.join(timeout=3)
        if self._log is not None:
            self._log.close()
            self._log = None
        return code

    def force_close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self._reader is not None:
            self._reader.join(timeout=2)
        if self._log is not None:
            self._log.close()
            self._log = None


def install_endstone(root: pathlib.Path, result: dict[str, Any]) -> None:
    run, artifact = discover("endstone", "linux")
    payload = _download_artifact("EndstoneMC/endstone", artifact, root / "downloads" / "endstone")
    wheel = locate_one(payload, ["endstone-*-cp313-cp313-*.whl", "endstone-*.whl"])
    result["endstone"] = {
        "sha": run.get("head_sha"),
        "run_id": run.get("id"),
        "run_url": run.get("html_url"),
        "artifact_id": artifact.get("id"),
        "artifact_name": artifact.get("name"),
        "wheel": str(wheel.relative_to(root)),
    }
    run_checked(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--force-reinstall",
            str(wheel),
        ],
        timeout=300,
    )


def start_server(root: pathlib.Path, server_dir: pathlib.Path, log_path: pathlib.Path) -> ServerProcess:
    server_dir.mkdir(parents=True, exist_ok=True)
    server = ServerProcess(
        [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(server_dir)],
        root,
        log_path,
    )
    server.start()
    server.wait_for(
        lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines),
        240,
        "BDS ready",
    )
    return server


def stop_server(server: ServerProcess | None) -> None:
    if server is None:
        return
    if not server.graceful_stop(60):
        server.force_kill_tree()
        raise RuntimeError("BDS did not stop gracefully")
    server.close()


def patch_server_properties(path: pathlib.Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"BDS did not create {path}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _ = line.split("=", 1)
        key = key.strip()
        if key in RECOMMENDED_PROPERTIES:
            output.append(f"{key}={RECOMMENDED_PROPERTIES[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in RECOMMENDED_PROPERTIES.items():
        if key not in seen:
            output.append(f"{key}={value}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def list_players(server: ServerProcess) -> list[str]:
    start = server.command("list")
    return server.wait_command_output(start, 8)


def player_present(output: list[str]) -> bool:
    return any("testbot" in line.lower() for line in output)


def require_player(server: ServerProcess, present: bool) -> list[str]:
    output = list_players(server)
    found = player_present(output)
    if found != present:
        state = "present" if present else "absent"
        raise RuntimeError(f"Expected TestBot to be {state} in BDS list output: {' | '.join(output[-20:])}")
    return output


def wait_player_state(server: ServerProcess, present: bool, timeout: float = 20.0) -> tuple[list[str], float, int]:
    """Wait for BDS to converge on the requested player-list state.

    A client process can close its RakNet socket and exit before the BDS game
    thread has consumed the disconnect and removed the player from `list`.
    Treat that bounded server-side propagation delay as asynchronous state, not
    as a failed client shutdown.
    """
    started = time.monotonic()
    deadline = started + timeout
    probes = 0
    last_output: list[str] = []
    while True:
        if not server.is_alive():
            raise RuntimeError("BDS exited while waiting for player-list state")
        probes += 1
        last_output = list_players(server)
        if player_present(last_output) == present:
            return last_output, time.monotonic() - started, probes
        if time.monotonic() >= deadline:
            state = "present" if present else "absent"
            raise RuntimeError(
                f"Timed out after {timeout:.0f}s waiting for TestBot to become {state} in BDS list output: "
                f"{' | '.join(last_output[-20:])}"
            )
        time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", required=True)
    parser.add_argument("--soak-seconds", type=int, default=300)
    args = parser.parse_args()

    root = pathlib.Path.cwd()
    server_dir = root / "work" / "bot" / "bedrock_server"
    bot_log = root / "bot.log"
    bds_log = root / "bot-bds.log"
    result_path = root / "bot-test-result.json"
    diagnostics = root / "bot-failure-diagnostics.txt"
    result: dict[str, Any] = {
        "status": "running",
        "started_at": now_iso(),
        "completed_at": None,
        "failed_stage": None,
        "error": None,
        "bds_version": None,
        "checks": [],
        "bot_online_event": None,
        "bot_events": [],
        "soak_seconds": max(300, args.soak_seconds),
    }

    server: ServerProcess | None = None
    bot: BotProcess | None = None
    stage = "endstone-artifact"
    exit_code = 1
    try:
        install_endstone(root, result)
        result["checks"].append({"name": "endstone-artifact", "status": "PASS"})

        stage = "bds-bootstrap"
        server = start_server(root, server_dir, bds_log)
        stop_server(server)
        server = None
        patch_server_properties(server_dir / "server.properties")
        result["checks"].append({"name": "server-properties", "status": "PASS", "values": RECOMMENDED_PROPERTIES})

        stage = "bds-start"
        server = start_server(root, server_dir, bds_log)
        version_file = server_dir / "version.txt"
        if version_file.exists():
            result["bds_version"] = version_file.read_text(encoding="utf-8", errors="replace").strip()
        result["checks"].append({"name": "bds-ready", "status": "PASS"})

        stage = "bot-start"
        bot = BotProcess(pathlib.Path(args.bot), bot_log)
        bot.start()
        online = bot.wait_event("online", 60)
        events = bot.event_snapshot()
        names = {str(event.get("event")) for event in events}
        required = {"connecting", "connected", "start_game", "spawned", "chunk_radius_requested", "chunk_radius", "chunk_received", "online"}
        missing = sorted(required - names)
        if missing:
            raise RuntimeError(f"Bot reached online but required events are missing: {missing}")
        chunks = int(online.get("chunks_received", 0))
        if chunks < 1:
            raise RuntimeError(f"online event reported chunks_received={chunks}")
        result["bot_online_event"] = online
        result["checks"].append({"name": "bot-online", "status": "PASS", "event": online})

        stage = "bds-player-visible"
        output = require_player(server, True)
        result["checks"].append({"name": "bds-player-visible", "status": "PASS", "output": output[-20:]})

        stage = "soak"
        soak_seconds = max(300, args.soak_seconds)
        deadline = time.monotonic() + soak_seconds
        next_probe = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not server.is_alive():
                raise RuntimeError("BDS exited during bot soak")
            if not bot.is_alive():
                assert bot.process is not None
                raise RuntimeError(f"Bot exited with code {bot.process.returncode} during soak")
            if time.monotonic() >= next_probe:
                require_player(server, True)
                next_probe += 60
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
        result["checks"].append({"name": "five-minute-soak", "status": "PASS", "seconds": soak_seconds})

        stage = "bot-sigterm"
        code = bot.terminate(15)
        if code != 0:
            raise RuntimeError(f"Bot exited with code {code} after SIGTERM")
        if not any(event.get("event") == "disconnected" for event in bot.event_snapshot()):
            raise RuntimeError("Bot exited cleanly after SIGTERM but emitted no disconnected event")
        result["checks"].append({"name": "sigterm-clean-exit", "status": "PASS", "exit_code": code})

        stage = "bds-player-left"
        output, propagation_seconds, probes = wait_player_state(server, False, timeout=20)
        result["checks"].append(
            {
                "name": "bds-player-left",
                "status": "PASS",
                "output": output[-20:],
                "propagation_seconds": round(propagation_seconds, 3),
                "probes": probes,
            }
        )

        stage = "bds-stop"
        stop_server(server)
        server = None
        result["checks"].append({"name": "bds-stop", "status": "PASS"})

        result["status"] = "PASS"
        exit_code = 0
    except Exception as exc:
        result["status"] = "FAIL"
        result["failed_stage"] = stage
        result["error"] = f"{type(exc).__name__}: {exc}"
        diagnostics.write_text(traceback.format_exc(), encoding="utf-8")
        print(traceback.format_exc(), file=sys.stderr, flush=True)
    finally:
        if bot is not None:
            result["bot_events"] = bot.event_snapshot()
            if bot.is_alive():
                bot.force_close()
        if server is not None:
            try:
                stop_server(server)
            except Exception as exc:
                print(f"cleanup: {exc}", file=sys.stderr, flush=True)
        result["completed_at"] = now_iso()
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(result, indent=2, sort_keys=True), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
