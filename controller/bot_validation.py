#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import threading
import time
import traceback
from typing import Any

from controller.run_test import IntegrationTest, now_iso, write_json
from providers.bot_provider import resolve_bot

BOT_NAME = "TestBot"
BOT_PORT = 19132

RECOMMENDED_PROPERTIES = {
    "server-port": str(BOT_PORT),
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


class BotClientProcess:
    def __init__(self, binary: pathlib.Path, log_path: pathlib.Path):
        self.binary = binary
        self.log_path = log_path
        self.process: subprocess.Popen[str] | None = None
        self.events: list[dict[str, Any]] = []
        self.lines: list[str] = []
        self._condition = threading.Condition()
        self._reader: threading.Thread | None = None
        self._log = None

    def start(self) -> None:
        cmd = [
            str(self.binary),
            "--host",
            "127.0.0.1",
            "--port",
            str(BOT_PORT),
            "--name",
            BOT_NAME,
            "--chunk-radius",
            "8",
            "--connect-timeout",
            "15s",
            "--spawn-timeout",
            "45s",
            "--json",
        ]
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self._log = self.log_path.open("w", encoding="utf-8", errors="replace")
        print("+", " ".join(cmd), flush=True)
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
        self._reader = threading.Thread(target=self._read_loop, name="bot-log-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for raw in self.process.stdout:
            line = raw.rstrip("\r\n")
            if self._log is not None:
                self._log.write(raw)
                self._log.flush()
            print(f"[bot] {line}", flush=True)
            event: dict[str, Any] | None = None
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and isinstance(parsed.get("event"), str):
                    event = parsed
            except json.JSONDecodeError:
                pass
            with self._condition:
                self.lines.append(line)
                if event is not None:
                    self.events.append(event)
                self._condition.notify_all()

    def snapshot_events(self) -> list[dict[str, Any]]:
        with self._condition:
            return list(self.events)

    def wait_event(self, name: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self.events:
                    if event.get("event") == "error":
                        raise RuntimeError(
                            f"Bot reported {event.get('stage', 'unknown')} error: {event.get('message', '')}"
                        )
                    if event.get("event") == name:
                        return event
                if self.process is not None and self.process.poll() is not None:
                    raise RuntimeError(
                        f"Bot exited with code {self.process.returncode} before event {name}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for bot event {name}")
                self._condition.wait(timeout=min(0.5, remaining))

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self, timeout: float = 15.0) -> int:
        if self.process is None:
            return 0
        if self.process.poll() is None:
            if os.name == "nt":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                self.force_kill()
                raise RuntimeError("Bot did not stop after termination signal") from exc
        if self._reader is not None:
            self._reader.join(timeout=3)
        if self._log is not None:
            self._log.close()
            self._log = None
        return int(self.process.returncode or 0)

    def force_kill(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
            )
        else:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def apply_server_properties(path: pathlib.Path) -> None:
    existing = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    remaining = dict(RECOMMENDED_PROPERTIES)
    output: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _ = line.split("=", 1)
        key = key.strip()
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1] != "":
            output.append("")
        output.append("# bds-test-bot integration profile")
        for key, value in remaining.items():
            output.append(f"{key}={value}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def command_output(test: IntegrationTest, command: str, timeout: float = 8.0) -> list[str]:
    assert test.server is not None
    start = test.server.command(command)
    return test.server.wait_command_output(start, timeout)


def require_bot_in_list(test: IntegrationTest, present: bool) -> None:
    # Run twice so a just-emitted disconnect line cannot be mistaken for list output.
    last_output: list[str] = []
    for attempt in range(2):
        last_output = command_output(test, "list")
        joined = "\n".join(last_output).lower()
        seen = BOT_NAME.lower() in joined
        if seen == present:
            return
        if attempt == 0:
            time.sleep(1.5)
    state = "present" if present else "absent"
    raise RuntimeError(
        f"Expected {BOT_NAME} to be {state} in BDS list output; got: "
        + " | ".join(last_output[-20:])
    )


def validate_event_contract(events: list[dict[str, Any]]) -> None:
    names = [str(event.get("event")) for event in events]
    required = [
        "connecting",
        "connected",
        "start_game",
        "spawned",
        "chunk_radius_requested",
        "chunk_radius",
        "chunk_received",
        "online",
    ]
    missing = [name for name in required if name not in names]
    if missing:
        raise RuntimeError("Bot event contract missing: " + ", ".join(missing))
    ordered = ["connecting", "connected", "start_game", "spawned", "chunk_radius_requested"]
    indices = [names.index(name) for name in ordered]
    if indices != sorted(indices):
        raise RuntimeError(f"Bot startup events out of order: {names}")
    online_index = names.index("online")
    if names.index("chunk_radius") > online_index or names.index("chunk_received") > online_index:
        raise RuntimeError("online was emitted before chunk readiness")


class BotIntegrationTest(IntegrationTest):
    def __init__(self, platform_name: str, bot_ref: str, bot_sha: str | None, soak_seconds: int):
        super().__init__(platform_name)
        self.bot_ref = bot_ref
        self.bot_sha = bot_sha
        self.soak_seconds = soak_seconds
        self.bot_log = self.root / "bot.log"
        self.bot_metadata_path = self.root / "bot-metadata.json"
        self.bot: BotClientProcess | None = None

    def bootstrap_and_configure_server(self) -> None:
        # First start lets Endstone download/extract the current BDS package. We then
        # stop it and edit the actual generated server.properties before the test run.
        self.start_server()
        self.shutdown()
        self.server = None
        properties = self.server_dir / "server.properties"
        apply_server_properties(properties)
        self.check("bot-server-properties", "PASS", str(properties.relative_to(self.root)))
        self.start_server()

    def run_bot(self) -> None:
        bot_info = resolve_bot(
            self.platform,
            self.bot_ref,
            self.downloads / "bot",
            self.bot_metadata_path,
            expected_sha=self.bot_sha,
        )
        self.result["bot"] = bot_info
        write_json(self.result_path, self.result)
        self.check(
            "bot-artifact",
            "PASS",
            f"{bot_info['sha']} run={bot_info['run_id']} artifact={bot_info['artifact']['name']}",
        )

        binary = pathlib.Path(bot_info["binary"])
        if self.platform == "linux":
            binary.chmod(binary.stat().st_mode | 0o111)
        self.bot = BotClientProcess(binary, self.bot_log)
        self.bot.start()
        online = self.bot.wait_event("online", 90)
        events = self.bot.snapshot_events()
        validate_event_contract(events)
        self.check(
            "bot-online-event",
            "PASS",
            f"chunks={online.get('chunks_received')} packets={online.get('packets_received')}",
        )

        require_bot_in_list(self, True)
        self.check("bds-sees-bot", "PASS", BOT_NAME)

        deadline = time.monotonic() + self.soak_seconds
        next_probe = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not self.bot.is_alive():
                code = self.bot.process.poll() if self.bot.process else None
                raise RuntimeError(f"Bot exited during soak with code {code}")
            if self.server is None or not self.server.is_alive():
                raise RuntimeError("BDS exited during bot soak")
            now = time.monotonic()
            if now >= next_probe:
                require_bot_in_list(self, True)
                next_probe = now + 60
            time.sleep(min(1.0, max(0.0, deadline - now)))
        self.check("bot-soak", "PASS", f"{self.soak_seconds}s online")

        code = self.bot.stop()
        if code != 0:
            raise RuntimeError(f"Bot returned non-zero after termination signal: {code}")
        self.check("bot-signal-exit", "PASS", "SIGTERM/CTRL_BREAK -> exit 0")
        time.sleep(2)
        require_bot_in_list(self, False)
        self.check("bds-sees-bot-quit", "PASS", BOT_NAME)

    def execute(self) -> int:
        stage = "initialization"
        try:
            stage = "base-artifacts"
            self.install_artifacts()
            stage = "bds-bootstrap"
            self.bootstrap_and_configure_server()
            stage = "bot-e2e"
            self.run_bot()
            stage = "shutdown"
            self.shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1200]
            diagnostic = traceback.format_exc()
            try:
                if self.bot is not None and self.bot.is_alive():
                    self.bot.force_kill()
            except Exception:
                diagnostic += "\n\nBot cleanup failure:\n" + traceback.format_exc()
            try:
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
                    self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:
                diagnostic += "\n\nServer cleanup failure:\n" + traceback.format_exc()
            bds_lines = self.server.snapshot()[-200:] if self.server is not None else []
            bot_events = self.bot.snapshot_events() if self.bot is not None else []
            self.diagnostics.write_text(
                diagnostic
                + "\n\nBot events:\n"
                + json.dumps(bot_events, indent=2)
                + "\n\nLast BDS log lines:\n"
                + "\n".join(bds_lines),
                encoding="utf-8",
            )
            return 1
        finally:
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)
            print(json.dumps(self.result, indent=2), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot-ref", default="feat/minimal-client")
    parser.add_argument("--bot-sha", default="")
    parser.add_argument("--soak-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.soak_seconds < 1:
        parser.error("--soak-seconds must be positive")
    return BotIntegrationTest(
        args.platform,
        args.bot_ref,
        args.bot_sha or None,
        args.soak_seconds,
    ).execute()


if __name__ == "__main__":
    raise SystemExit(main())
