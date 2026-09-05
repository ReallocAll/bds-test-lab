from __future__ import annotations

import time
import uuid
from typing import Any

from controller import combined_pack_gamerule_fleet_exact_runner as exact
from controller import pr49_no_shim_windows_runner as base
from controller.combined_pack_gamerule_fleet_validation import CombinedPackGameruleFleetValidation

HOT_RELOAD_CYCLES = 3
_ORIGINAL_PR49_RUN_PROFILER = CombinedPackGameruleFleetValidation.run_profiler
_ORIGINAL_EXACT_COMMAND = exact._FrameworkShutdownServerProcess.command
_ORIGINAL_EXACT_WAIT_COMMAND_OUTPUT = exact._FrameworkShutdownServerProcess.wait_command_output


def _native_console_command(self: exact._FrameworkShutdownServerProcess, command: str) -> int:
    """Queue commands through Endstone's native console, then an ordered framework ACK."""

    if not self.is_alive() or self.process is None or self.process.stdin is None:
        raise RuntimeError(f"Cannot send command to stopped server: {command}")
    start = len(self.snapshot())
    token = uuid.uuid4().hex
    pending = getattr(self, "_pending_native_console_commands", None)
    if pending is None:
        pending = {}
        self._pending_native_console_commands = pending
    pending[start] = token
    print(f"> {command} [native-console token={token}]", flush=True)
    self.process.stdin.write(command + "\n")
    self.process.stdin.write(f"ciack {token}\n")
    self.process.stdin.flush()
    return start


def _wait_native_console_command_output(
    self: exact._FrameworkShutdownServerProcess,
    start_index: int,
    timeout: float = 8.0,
) -> list[str]:
    pending = getattr(self, "_pending_native_console_commands", {})
    token = pending.get(start_index)
    if token is None:
        return _ORIGINAL_EXACT_WAIT_COMMAND_OUTPUT(self, start_index, timeout)
    acknowledgement = f"ci command transport acknowledged; token={token}".casefold()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = self.snapshot()[start_index:]
        if any(acknowledgement in line.casefold() for line in lines):
            pending.pop(start_index, None)
            return lines
        if not self.is_alive():
            raise RuntimeError("BDS exited before native-console CI command acknowledgement")
        time.sleep(0.05)
    raise TimeoutError(
        f"Timed out after {timeout:.0f}s waiting for native-console CI command acknowledgement: {token}"
    )


# Ordinary commands must enter through BDS's console queue. In particular, Endstone /reload
# must not execute from the lifecycle plugin's own scheduler callback while that plugin is
# being disabled and reloaded. Graceful shutdown remains on the independent file control.
exact._FrameworkShutdownServerProcess.command = _native_console_command
exact._FrameworkShutdownServerProcess.wait_command_output = _wait_native_console_command_output


def _select_live_bds_identity(records: list[dict[str, Any]]) -> tuple[int, float]:
    candidates = [
        record
        for record in records
        if record.get("alive") is True
        and record.get("identity_match") is True
        and "bedrock_server" in str(record.get("name") or "").casefold()
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"expected exactly one verified live bedrock_server process, observed={candidates!r}")
    candidate = candidates[0]
    pid = int(candidate.get("pid") or 0)
    create_time = candidate.get("create_time")
    if pid <= 0 or not isinstance(create_time, (int, float)) or create_time <= 0:
        raise RuntimeError(f"invalid bedrock_server process identity: {candidate!r}")
    return pid, float(create_time)


def _validate_reload_output(lines: list[str]) -> None:
    joined = "\n".join(lines).casefold()
    if "reload complete." not in joined:
        raise RuntimeError("Endstone /reload did not report completion")
    if "failed to load c++ plugin from" in joined and "endstone_spark" in joined:
        raise RuntimeError("Endstone failed to reload the Spark C++ plugin")
    if "error occurred when enabling spark" in joined:
        raise RuntimeError("Spark reported an enable failure after Endstone /reload")


def _validate_post_reload_profiler_info(lines: list[str]) -> None:
    joined = "\n".join(lines).casefold()
    required = ("profiler is already running", "started automatically when spark enabled")
    missing = [marker for marker in required if marker not in joined]
    if missing:
        raise RuntimeError(f"Spark did not restore its background profiler after reload: missing={missing!r}")


def _wait_reload_complete(
    self: CombinedPackGameruleFleetValidation, start_index: int, cycle: int
) -> list[str]:
    assert self.server is not None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        lines = self.server.snapshot()[start_index:]
        if any("reload complete." in line.casefold() for line in lines):
            remaining = max(0.1, deadline - time.monotonic())
            lines = self.server.wait_command_output(start_index, remaining)
            _validate_reload_output(lines)
            return lines
        if not self.server.is_alive():
            raise RuntimeError(f"BDS exited during Endstone /reload cycle {cycle}")
        time.sleep(0.25)
    raise RuntimeError(f"Endstone /reload cycle {cycle} did not complete within 60s")


def _run_reload_allocation_probe(
    self: CombinedPackGameruleFleetValidation, cycle: int
) -> str:
    assert self.server is not None
    start = self.server.command("spark profiler start --timeout 5 --alloc")
    deadline = time.monotonic() + 45
    url: str | None = None
    while time.monotonic() < deadline:
        lines = self.server.snapshot()
        recent = "\n".join(lines[start:]).casefold()
        if "allocation profiler status: failed" in recent or "incomplete profile data was discarded" in recent:
            raise RuntimeError(f"allocation profiler failed after hot reload cycle {cycle}")
        url = self._viewer_url(lines, start)
        if url:
            break
        if not self.server.is_alive():
            raise RuntimeError(f"BDS exited during post-reload allocation probe cycle {cycle}")
        time.sleep(0.5)
    if url is None:
        stop_at = self.server.command("spark profiler stop")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            lines = self.server.snapshot()
            url = self._viewer_url(lines, min(start, stop_at))
            if url:
                break
            if not self.server.is_alive():
                raise RuntimeError(f"BDS exited while finalizing post-reload allocation probe cycle {cycle}")
            time.sleep(0.5)
    if url is None:
        raise RuntimeError(f"post-reload allocation probe cycle {cycle} produced no spark viewer URL")
    return url


def _run_true_hot_reload_cycles(self: CombinedPackGameruleFleetValidation) -> None:
    assert self.server is not None
    baseline_identity = _select_live_bds_identity(self.server.process_tree_snapshot())
    evidence: list[dict[str, Any]] = []
    self.result["hot_reload_cycles"] = evidence
    self._write_results()

    for cycle in range(1, HOT_RELOAD_CYCLES + 1):
        self.assert_20_players(f"before-hot-reload-{cycle}")
        start = self.server.command("reload")
        reload_lines = _wait_reload_complete(self, start, cycle)

        observed_identity = _select_live_bds_identity(self.server.process_tree_snapshot())
        if observed_identity != baseline_identity:
            raise RuntimeError(
                f"bedrock_server process identity changed across hot reload cycle {cycle}: "
                f"baseline={baseline_identity!r} observed={observed_identity!r}"
            )

        plugin_dir = self.server_dir / "plugins"
        if any(path.name.casefold().startswith("spark_allocation_shim.") for path in plugin_dir.rglob("*")):
            raise RuntimeError(f"spark_allocation_shim appeared after hot reload cycle {cycle}")

        info_at = self.server.command("spark profiler info")
        info = self.server.wait_command_output(info_at, 12)
        _validate_post_reload_profiler_info(info)
        viewer_url = _run_reload_allocation_probe(self, cycle)
        self.assert_20_players(f"after-hot-reload-{cycle}")

        evidence.append(
            {
                "cycle": cycle,
                "bds_pid": observed_identity[0],
                "bds_create_time": observed_identity[1],
                "same_bds_identity": True,
                "allocation_profile_viewer_url": viewer_url,
                "reload_output_tail": reload_lines[-20:],
            }
        )
        self._write_results()

    self.check(
        "pr49-true-endstone-hot-reload",
        "PASS",
        "same bedrock_server process survived three Endstone /reload cycles; Spark re-enabled and produced a fresh allocation profile after every true C++ plugin unload/reload",
        cycles=HOT_RELOAD_CYCLES,
        bds_pid=baseline_identity[0],
        bds_create_time=baseline_identity[1],
        allocation_profile_viewer_urls=[row["allocation_profile_viewer_url"] for row in evidence],
    )


def _run_final_pr49_profiler(self: CombinedPackGameruleFleetValidation, allocation: bool) -> str | None:
    url = _ORIGINAL_PR49_RUN_PROFILER(self, allocation)
    if allocation:
        _run_true_hot_reload_cycles(self)
        self.assert_20_players("after-true-hot-reload-cycles")
    return url


CombinedPackGameruleFleetValidation.run_profiler = _run_final_pr49_profiler


if __name__ == "__main__":
    raise SystemExit(base.main())
