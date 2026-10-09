"""Real BDS integration for EndKeep PR #19; never touches a user's server."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import traceback

from controller.run_test import READY_HINTS, ServerProcess

ROOT = pathlib.Path.cwd()
RESULT = ROOT / "endkeep-bds-result.json"


def evidence(**data: object) -> None:
    RESULT.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def require_output(server: ServerProcess, command: str, *, accepted: tuple[str, ...] = ()) -> str:
    start = server.command(command)
    output = "\n".join(server.wait_command_output(start, 10))
    lower = output.lower()
    if any(bad in lower for bad in ("unknown command", "command not found", "unrecognized command", "endkeep is busy or disabled", "failed to enable endkeep")):
        raise AssertionError(f"{command} rejected: {output[-2000:]}")
    if accepted and not any(hint in lower for hint in accepted):
        raise AssertionError(f"{command} did not return expected result: {output[-2000:]}")
    return output


def await_manifest(storage: pathlib.Path, size: int, timeout: float = 180) -> list[str]:
    from endstone_endkeep.repository.manifest import ManifestStore

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        manifest = ManifestStore(storage / "repo").load_current()
        if manifest and len(manifest.chain) >= size:
            return [node.snapshot for node in manifest.chain]
        time.sleep(2)
    raise TimeoutError(f"EndKeep did not commit {size} recovery points within {timeout}s")


def run(platform: str) -> int:
    workspace = ROOT / "work" / platform / "bedrock_server"
    storage = (ROOT / "work" / platform / "backups").resolve()
    plugin_dir = workspace / "plugins" / "endkeep"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=True)

    from importlib.resources import files
    from importlib.metadata import version
    from endstone_endkeep.logical.amulet_reader import iter_visible_state

    template = files("endstone_endkeep").joinpath("config.toml").read_text("utf-8")
    template = template.replace('path = "backups"', f'path = "{storage.as_posix()}"')
    template = template.replace("min_free_space_gib = 5", "min_free_space_gib = 1")
    template = template.replace("compression_threads = 4", "compression_threads = 1")
    (plugin_dir / "config.toml").write_text(template, encoding="utf-8")

    # The exact built EndKeep Wheel must be installed into this environment;
    # the Endstone entry point must load it, not a locally imported source.
    meta = {
        "platform": platform,
        "python": sys.version.split()[0],
        "endstone": version("endstone"),
        "endkeep": version("endstone-endkeep"),
        "amulet_leveldb": version("amulet-leveldb"),
        "server_folder": str(workspace),
        "stage": "startup",
    }
    evidence(**meta)
    server = ServerProcess(
        [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(workspace)],
        ROOT,
        ROOT / "endkeep-bds.log",
    )
    started = False
    try:
        server.start()
        started = True
        server.wait_for(lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines), 240, "BDS startup")
        server.wait_for(lambda lines: any("endkeep enabled." in line.lower() for line in lines), 60, "EndKeep plugin enable")
        require_output(server, "backup status", accepted=("generation=", "snapshots="))
        meta["stage"] = "backup1"
        evidence(**meta)
        require_output(server, "backup create", accepted=("capture accepted",))
        time.sleep(12)
        # A maintenance request may be deferred until capture completes; poll status.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            output = require_output(server, "backup status")
            if "busy" not in output.lower() or "idle" in output.lower():
                break
            time.sleep(3)
        require_output(server, "backup maintenance", accepted=("maintenance",))
        ids = await_manifest(storage, 1)
        meta["stage"] = "backup2"
        meta["snapshots"] = ids
        evidence(**meta)
        time.sleep(4)
        require_output(server, "backup create", accepted=("capture accepted",))
        time.sleep(12)
        require_output(server, "backup maintenance", accepted=("maintenance",))
        ids = await_manifest(storage, 2)
        meta["snapshots"] = ids
        meta["stage"] = "verify"
        evidence(**meta)
        # Exercise online verify command and read-only export path.
        require_output(server, "backup verify deep", accepted=("verif",))
        time.sleep(15)
        require_output(server, "backup export " + ids[-1], accepted=("export",))
        exports = storage / "exports" / ids[-1]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not (exports / "db").is_dir():
            time.sleep(2)
        if not (exports / "db").is_dir():
            raise AssertionError("EndKeep online export did not materialize LevelDB")
        with iter_visible_state(exports / "db") as visible:
            online_records = list(visible)
        if not (exports / "level.dat").is_file():
            raise AssertionError("exported level.dat is absent")
        meta["online_records"] = len(online_records)
        meta["stage"] = "shutdown"
        evidence(**meta)
        if not server.graceful_stop(80):
            raise AssertionError("BDS failed graceful shutdown")
        server.close()
        started = False

        # Offline verify and restore, from a persisted real-BDS repository.
        meta["stage"] = "offline-verify-restore"
        evidence(**meta)
        from endstone_endkeep.offline.cli import command_restore, command_verify

        command_verify(storage / "repo")
        restore = ROOT / "work" / platform / "restored-level"
        command_restore(storage / "repo", restore, ids[-1])
        with iter_visible_state(restore / "db") as visible:
            restored_records = list(visible)
        if restored_records != online_records:
            raise AssertionError("offline restore LevelDB differs from online export")
        if (restore / "level.dat").read_bytes() != (exports / "level.dat").read_bytes():
            raise AssertionError("restored sidecar does not match online export")
        meta.update(stage="completed", status="PASS", restored_records=len(restored_records))
        evidence(**meta)
        return 0
    except Exception as exc:
        meta.update(stage=meta["stage"], status="FAIL", error=f"{type(exc).__name__}: {exc}")
        evidence(**meta)
        (ROOT / "endkeep-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return 1
    finally:
        if started:
            if server.is_alive():
                server.force_kill_tree()
            server.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("linux", "windows"), required=True)
    args = parser.parse_args()
    return run(args.platform)


if __name__ == "__main__":
    raise SystemExit(main())
