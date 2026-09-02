from __future__ import annotations

import ctypes
import json
import os
import time
from pathlib import Path
from typing import Any

from endstone.command import Command, CommandSender
from endstone.plugin import Plugin


class AllocationLivePlugin(Plugin):
    """Bounded native allocations that remain live through Spark export."""

    api_version = "0.11"
    commands = {  # noqa: RUF012 - Endstone discovers command metadata from the plugin class.
        "allocation-live": {
            "description": "Start or inspect the retained allocation workload",
            "usages": ["/allocation-live <start|release|status>"],
            "permissions": ["spark_allocation_live_test.command.allocation-live"],
        }
    }
    permissions = {  # noqa: RUF012 - Endstone discovers permission metadata from the plugin class.
        "spark_allocation_live_test.command.allocation-live": {
            "description": "Allow the CI retained allocation workload command.",
            "default": "op",
        }
    }

    def on_enable(self) -> None:
        if self.get_command("allocation-live") is None:
            raise RuntimeError("Endstone did not register the allocation-live command")
        self.block_bytes = self._positive_env("SPARK_ALLOCATION_LIVE_BLOCK_BYTES", 1 << 20)
        self.block_count = self._positive_env("SPARK_ALLOCATION_LIVE_BLOCKS", 24)
        raw_state_path = os.environ.get("SPARK_ALLOCATION_LIVE_STATE", "").strip()
        self.state_path = Path(raw_state_path) if raw_state_path else None
        raw_helper_path = os.environ.get("SPARK_ALLOCATION_LIVE_HELPER", "").strip()
        if not raw_helper_path:
            raise RuntimeError("SPARK_ALLOCATION_LIVE_HELPER is required")
        self.helper_path = Path(raw_helper_path)
        if self.helper_path.is_symlink() or not self.helper_path.is_file():
            raise RuntimeError(f"retained allocation helper is missing or symlinked: {self.helper_path}")
        self._helper = ctypes.CDLL(str(self.helper_path), mode=getattr(ctypes, "RTLD_LOCAL", 0))
        self._helper.spark_allocation_live_retain.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
        self._helper.spark_allocation_live_retain.restype = ctypes.c_int
        self._helper.spark_allocation_live_release.argtypes = []
        self._helper.spark_allocation_live_release.restype = ctypes.c_int
        self._helper.spark_allocation_live_retained_blocks.argtypes = []
        self._helper.spark_allocation_live_retained_blocks.restype = ctypes.c_size_t
        self._helper.spark_allocation_live_retained_bytes.argtypes = []
        self._helper.spark_allocation_live_retained_bytes.restype = ctypes.c_size_t
        self._started = False
        self._start_ns: int | None = None
        self._failed = False
        self._cleaned_blocks_before: int | None = None
        self._cleaned_bytes_before: int | None = None
        self.server.scheduler.run_task(self, self._allocation_tick, delay=1, period=1)
        self._write_state("enabled")
        self.logger.info(
            f"Spark retained allocation test enabled: blocks={self.block_count} block_bytes={self.block_bytes}"
        )

    @staticmethod
    def _positive_env(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a positive integer") from exc
        if value <= 0:
            raise RuntimeError(f"{name} must be a positive integer")
        return value

    def _allocation_tick(self) -> None:
        if not self._started or self._retained_blocks() >= self.block_count:
            return
        if self._helper.spark_allocation_live_retain(self.block_bytes, self.block_count) != 0:
            self._failed = True
            self._write_state("allocation-failed")
            return
        self._write_state("running")

    def _retained_blocks(self) -> int:
        return int(self._helper.spark_allocation_live_retained_blocks())

    def _retained_bytes(self) -> int:
        return int(self._helper.spark_allocation_live_retained_bytes())

    def _state(self, status: str) -> dict[str, Any]:
        return {
            "status": status,
            "started": self._started,
            "start_monotonic_ns": self._start_ns,
            "target_blocks": self.block_count,
            "block_bytes": self.block_bytes,
            "retained_blocks": self._retained_blocks(),
            "retained_bytes": self._retained_bytes(),
            "allocation_failed": self._failed,
            "updated_monotonic_ns": time.monotonic_ns(),
        }

    def _write_state(
        self,
        status: str,
        retained_before_cleanup: int | None = None,
        bytes_before_cleanup: int | None = None,
    ) -> None:
        if self.state_path is None:
            return
        payload = self._state(status)
        if status == "cleaned":
            payload.update(
                {
                    "retained_blocks_before_cleanup": retained_before_cleanup,
                    "retained_bytes_before_cleanup": bytes_before_cleanup,
                    "cleaned_up": True,
                }
            )
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.state_path)

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        del sender
        if command.name != "allocation-live" or not args:
            return False
        action = args[0].strip().lower()
        if action == "start":
            if self._retained_blocks() != 0:
                self.logger.error("Spark retained allocation workload cannot start with live blocks")
                return False
            self._started = True
            self._start_ns = time.monotonic_ns()
            self._write_state("started")
            self.logger.info("Spark retained allocation workload started")
            return True
        if action == "release":
            retained_before_cleanup = self._retained_blocks()
            bytes_before_cleanup = self._retained_bytes()
            self._started = False
            if self._helper.spark_allocation_live_release() != 0 or self._retained_blocks() != 0:
                self._failed = True
                self._write_state("cleanup-failed")
                return False
            self._cleaned_blocks_before = retained_before_cleanup
            self._cleaned_bytes_before = bytes_before_cleanup
            self._write_state("cleaned", retained_before_cleanup, bytes_before_cleanup)
            self.logger.info(
                f"Spark retained allocation workload released: cleaned_blocks={retained_before_cleanup} "
                f"cleaned_bytes={bytes_before_cleanup}"
            )
            return True
        if action == "status":
            state = self._state("status")
            self.logger.info(
                "Spark retained allocation workload status: "
                f"started={state['started']} retained_blocks={state['retained_blocks']} "
                f"retained_bytes={state['retained_bytes']} failed={state['allocation_failed']}"
            )
            return True
        return False

    def on_disable(self) -> None:
        retained_before_cleanup = self._cleaned_blocks_before
        bytes_before_cleanup = self._cleaned_bytes_before
        if retained_before_cleanup is None or bytes_before_cleanup is None:
            retained_before_cleanup = self._retained_blocks()
            bytes_before_cleanup = self._retained_bytes()
        self._started = False
        if self._helper.spark_allocation_live_release() != 0:
            self._failed = True
        self._write_state("cleaned", retained_before_cleanup, bytes_before_cleanup)
        self.logger.info(
            f"Spark retained allocation test disabled: cleaned_blocks={retained_before_cleanup} "
            f"cleaned_bytes={bytes_before_cleanup}"
        )
