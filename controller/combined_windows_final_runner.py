from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

# Importing the exact runner installs the exact-artifact, Windows lifecycle,
# behavior-pack state-oracle, and provenance adapters before we replace only
# the Windows bootstrap and hosted command transports below.
from controller import combined_pack_gamerule_fleet_exact_runner as exact
from controller.bot_validation import patch_server_properties
from controller.combined_pack_gamerule_fleet_validation import (
    WORLD_NAME,
    CombinedPackGameruleFleetValidation,
)
from controller.fleet_spark_validation import set_server_property

_ORIGINAL_BOOTSTRAP_SCENARIO_WORLD = CombinedPackGameruleFleetValidation.bootstrap_scenario_world
_ORIGINAL_WAIT_COMMAND_OUTPUT = exact._FrameworkShutdownServerProcess.wait_command_output


def _command_control_path(server: exact._FrameworkShutdownServerProcess) -> Path:
    for line in reversed(server.snapshot()):
        lowered = line.casefold()
        marker = "command-control="
        if "ci lifecycle control enabled;" not in lowered or marker not in lowered:
            continue
        raw = line[lowered.index(marker) + len(marker) :].split(";", 1)[0].strip()
        path = Path(raw)
        if not path.is_absolute():
            path = (server.cwd / path).resolve()
        return path
    raise RuntimeError("CI command file control was not registered by Endstone")


def _file_control_command(self: exact._FrameworkShutdownServerProcess, command: str) -> int:
    if not self.is_alive():
        raise RuntimeError(f"Cannot send command to stopped server: {command}")
    request_path = _command_control_path(self)
    if request_path.exists():
        raise RuntimeError(f"previous CI command request is still pending: {request_path}")
    start = len(self.snapshot())
    token = uuid.uuid4().hex
    pending = getattr(self, "_pending_file_commands", None)
    if pending is None:
        pending = {}
        self._pending_file_commands = pending
    pending[start] = token
    payload = {"token": token, "command": command}
    print(f"> {command} [file-trigger token={token}]", flush=True)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    return start


def _wait_file_control_command_output(
    self: exact._FrameworkShutdownServerProcess,
    start_index: int,
    timeout: float = 8.0,
) -> list[str]:
    pending = getattr(self, "_pending_file_commands", {})
    token = pending.get(start_index)
    if token is None:
        return _ORIGINAL_WAIT_COMMAND_OUTPUT(self, start_index, timeout)

    completion = f"ci command dispatch completed; token={token}; dispatched=".casefold()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = self.snapshot()[start_index:]
        matched = next((line for line in lines if completion in line.casefold()), None)
        if matched is not None:
            pending.pop(start_index, None)
            if "dispatched=true" not in matched.casefold():
                raise RuntimeError(f"Endstone rejected CI command transport request: {matched}")
            return lines
        if not self.is_alive():
            raise RuntimeError("BDS exited before CI command dispatch acknowledgement")
        time.sleep(0.05)
    raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for CI command dispatch acknowledgement: {token}")


exact._FrameworkShutdownServerProcess.command = _file_control_command
exact._FrameworkShutdownServerProcess.wait_command_output = _wait_file_control_command_output


def _world_directories(server_dir: Path) -> dict[str, Path]:
    worlds_root = server_dir / "worlds"
    if not worlds_root.exists():
        return {}
    return {path.name: path for path in worlds_root.iterdir() if path.is_dir()}


def _bootstrap_windows_from_provisioned_world(self: CombinedPackGameruleFleetValidation) -> None:
    """Create one real BDS world, then reuse it for the exact Windows case.

    The former exact Windows path started BDS a second time solely to create a
    differently named fresh world and then immediately asked the interactive
    Endstone wrapper to stop again. Hosted Windows evidence showed that second
    command could remain unacknowledged even though the first shutdown was
    fully graceful. The extra lifecycle is not part of the Spark workload.

    This adapter keeps the strong world oracle: BDS itself must create exactly
    one new world directory during the provisioning boot. Only after a clean
    shutdown do we rename that BDS-created world, apply the same server
    properties, and install the same three behavior packs offline. The final
    measured server still loads the renamed real world and all existing
    behavior-pack, gamerule, 20-player, profile, provenance, bStats, and
    shutdown validators remain unchanged.
    """

    if self.platform != "windows":
        _ORIGINAL_BOOTSTRAP_SCENARIO_WORLD(self)
        return

    before = _world_directories(self.server_dir)
    if WORLD_NAME in before:
        raise RuntimeError(f"target world already exists before bootstrap: {WORLD_NAME}")

    self.start_server()
    self.wait_post_start_initialization()
    self.stop_server_for_phase_change("bootstrap-provisioning")

    after = _world_directories(self.server_dir)
    created_names = sorted(set(after) - set(before))
    if len(created_names) != 1:
        raise RuntimeError(
            "BDS provisioning must create exactly one fresh world directory; "
            f"created={created_names!r}, before={sorted(before)!r}, after={sorted(after)!r}"
        )

    source_world = after[created_names[0]]
    target_world = self.server_dir / "worlds" / WORLD_NAME
    if target_world.exists():
        raise RuntimeError(f"target world unexpectedly exists after bootstrap: {target_world}")
    source_world.rename(target_world)
    if not target_world.is_dir():
        raise RuntimeError(f"failed to preserve BDS-created world at {target_world}")

    properties = self.server_dir / "server.properties"
    patch_server_properties(properties)
    set_server_property(properties, "max-players", "30")
    set_server_property(properties, "level-name", WORLD_NAME)
    set_server_property(properties, "allow-cheats", "true")
    set_server_property(properties, "player-idle-timeout", "0")

    self.install_behavior_packs(target_world)
    self.check(
        "combined-world-bootstrap",
        "PASS",
        "one fresh BDS-provisioned world reused; offline mode, cheats, max-players=30, and three behavior packs configured",
        source_world=created_names[0],
        target_world=WORLD_NAME,
        windows_bootstrap_server_starts=1,
    )


CombinedPackGameruleFleetValidation.bootstrap_scenario_world = _bootstrap_windows_from_provisioned_world


if __name__ == "__main__":
    raise SystemExit(exact.main())
