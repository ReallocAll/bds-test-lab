from __future__ import annotations

import json
from pathlib import Path

from endstone.command import Command, CommandSender
from endstone.plugin import Plugin
from endstone.scheduler import Task


class CiLifecycleControl(Plugin):
    """CI-only lifecycle and command control using low-frequency file transports."""

    api_version = "0.11"
    commands = {  # noqa: RUF012 - Endstone discovers command metadata from the plugin class.
        "cishutdown": {
            "description": "Gracefully shut down Endstone for CI lifecycle validation",
            "usages": ["/cishutdown"],
            "permissions": ["endstone_ci_lifecycle_control.command.cishutdown"],
        }
    }
    permissions = {  # noqa: RUF012 - Endstone discovers permission metadata from the plugin class.
        "endstone_ci_lifecycle_control.command.cishutdown": {
            "description": "Allow the CI lifecycle harness to shut down Endstone gracefully.",
            "default": "op",
        }
    }

    def __init__(self) -> None:
        super().__init__()
        self._request_path: Path | None = None
        self._command_path: Path | None = None
        self._file_control_task: Task | None = None

    def on_enable(self) -> None:
        if self.get_command("cishutdown") is None:
            raise RuntimeError("Endstone did not register the cishutdown command")
        self.data_folder.mkdir(parents=True, exist_ok=True)
        self._request_path = self.data_folder / "shutdown.request"
        self._command_path = self.data_folder / "command.request"
        self._request_path.unlink(missing_ok=True)
        self._command_path.unlink(missing_ok=True)
        self._file_control_task = self.server.scheduler.run_task(
            self,
            self._poll_file_control,
            delay=1,
            period=1,
        )
        if self._file_control_task is None:
            raise RuntimeError("Endstone did not schedule the CI file control")
        self.logger.info(
            "CI lifecycle control enabled; cishutdown registered; "
            f"command-control={self._command_path.resolve()}; file-control={self._request_path.resolve()}"
        )

    def on_disable(self) -> None:
        if self._file_control_task is not None and not self._file_control_task.is_cancelled:
            self._file_control_task.cancel()
        self._file_control_task = None
        if self._request_path is not None:
            self._request_path.unlink(missing_ok=True)
        if self._command_path is not None:
            self._command_path.unlink(missing_ok=True)

    def _poll_file_control(self) -> None:
        command_path = self._command_path
        if command_path is not None and command_path.is_file():
            try:
                payload = json.loads(command_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                command_path.unlink(missing_ok=True)
                self.logger.error(f"CI command file rejected: {type(exc).__name__}: {exc}")
            else:
                command_path.unlink(missing_ok=True)
                token = str(payload.get("token", "")).strip()
                command_line = str(payload.get("command", "")).strip()
                if not token or not command_line:
                    self.logger.error("CI command file rejected: token and command are required")
                else:
                    self.logger.info(f"CI command dispatch requested; token={token}; command={command_line}")
                    dispatched = self.server.dispatch_command(self.server.command_sender, command_line)
                    self.logger.info(
                        f"CI command dispatch completed; token={token}; dispatched={str(bool(dispatched)).lower()}"
                    )

        request_path = self._request_path
        if request_path is None or not request_path.is_file():
            return
        try:
            token = request_path.read_text(encoding="utf-8").strip()
        finally:
            request_path.unlink(missing_ok=True)
        if not token:
            self.logger.warning("Ignoring empty CI lifecycle file request")
            return
        if self._file_control_task is not None and not self._file_control_task.is_cancelled:
            self._file_control_task.cancel()
        self.logger.info(f"CI lifecycle shutdown requested via file; token={token}")
        self.server.shutdown()

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        del sender, args
        if command.name != "cishutdown":
            return False
        self.logger.info("CI lifecycle shutdown requested")
        self.server.shutdown()
        return True
