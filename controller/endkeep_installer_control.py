"""Minimal Python plugin control for the Windows framework installer."""
from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
from pathlib import Path

import psutil

from controller.bstats import write_disabled_bstats_config
from controller.run_test import ServerProcess


def main() -> int:
    root = Path.cwd()
    target = root / "work" / "endkeep-installer-control" / "bedrock_server"
    (target / "plugins").mkdir(parents=True, exist_ok=True)
    for wheel in (root / "reload-diagnostic-wheel").glob("*.whl"):
        shutil.copy2(wheel, target / "plugins" / wheel.name)
    write_disabled_bstats_config(target)
    evidence = {"status": "RUNNING", "endkeep_installed": False, "worker_created": False}
    result = root / "endkeep-installer-control.json"
    os.environ["PIP_LOG"] = str(root / "endkeep-installer-pip.log")
    os.environ["PIP_VERBOSE"] = "3"
    server = ServerProcess([sys.executable, "-m", "endstone", "--yes", "--server-folder", str(target)],
                           root, root / "endkeep-installer-control.log")
    try:
        server.start()
        server.wait_for(lambda rows: any("Enabling reload_diagnostic" in row for row in rows),
                        180, "minimal Python plugin enabled")
        cursor = server.command("reload")
        server.wait_for(lambda rows: any("reload complete" in row.lower() for row in rows[cursor:]),
                        30, "minimal Python plugin framework reload")
        evidence["status"] = "PASS"
        return 0
    except Exception as exc:
        evidence["status"] = "FAIL"
        evidence["error"] = repr(exc)
        evidence["traceback"] = traceback.format_exc()
        evidence["children"] = []
        if server.pid:
            for process in psutil.Process(server.pid).children(recursive=True):
                try:
                    evidence["children"].append({"pid": process.pid, "command": process.cmdline(),
                                                 "status": process.status()})
                except psutil.Error:
                    pass
        return 1
    finally:
        result.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence), flush=True)
        if server.is_alive():
            if evidence["status"] == "FAIL" or not server.graceful_stop(timeout=20):
                server.force_kill_tree()
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
