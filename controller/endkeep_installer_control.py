"""Minimal Python plugin control for the Windows framework installer."""
from __future__ import annotations

import json
import shutil
import sys
import time
import traceback
from pathlib import Path

from controller.bstats import write_disabled_bstats_config
from controller.run_test import ServerProcess


def main() -> int:
    root = Path.cwd()
    target = root / "work" / "endkeep-installer-control" / "bedrock_server"
    (target / "plugins").mkdir(parents=True, exist_ok=True)
    for wheel in (root / "reload-diagnostic-wheel").glob("*.whl"):
        shutil.copy2(wheel, target / "plugins" / wheel.name)
    write_disabled_bstats_config(target)
    evidence = {"status": "RUNNING", "endkeep_installed": False, "worker_created": False, "reloads": []}
    result = root / "endkeep-installer-control.json"
    server = ServerProcess([sys.executable, "-m", "endstone", "--yes", "--server-folder", str(target)],
                           root, root / "endkeep-installer-control.log")
    try:
        server.start()
        server.wait_for(lambda rows: any("Enabling reload_diagnostic" in row for row in rows),
                        180, "minimal Python plugin enabled")
        for _ in range(3):
            began = time.monotonic()
            cursor = server.command("reload")
            server.wait_for(
                lambda rows: any("reload complete" in row.lower() for row in rows[cursor:])
                and any("Enabling reload_diagnostic" in row for row in rows[cursor:]),
                30, "minimal Python plugin reenabled after reload")
            evidence["reloads"].append({"status": "PASS", "seconds": round(time.monotonic() - began, 3)})
        if any("Enabling endkeep" in row for row in server.snapshot()) or list(target.rglob("worker-runtime.json")):
            raise RuntimeError("minimal control unexpectedly loaded EndKeep")
        if not server.graceful_stop(timeout=20):
            raise RuntimeError("minimal plugin server did not stop cleanly")
        evidence["status"] = "PASS"
        return 0
    except Exception as exc:
        evidence["status"] = "FAIL"
        evidence["error"] = repr(exc)
        evidence["traceback"] = traceback.format_exc()
        return 1
    finally:
        result.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence), flush=True)
        if server.is_alive():
            server.force_kill_tree()
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
