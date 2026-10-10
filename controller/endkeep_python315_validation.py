"""Real BDS smoke test for EndKeep on CPython 3.15.

Artifacts and the Python interpreter are provisioned by the dedicated workflow.
The runner never transfers GitHub credentials to BDS or its worker process.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import traceback
from pathlib import Path

from controller.bstats import write_disabled_bstats_config
from controller.run_test import READY_HINTS, ServerProcess


def main() -> int:
    root = Path.cwd()
    work = root / "work" / "endkeep-python315"
    server_dir = work / "bedrock_server"
    plugin_dir = server_dir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    wheel = next((root / "endkeep-wheel").glob("endstone_endkeep-*.whl"))
    shutil.copy2(wheel, plugin_dir / wheel.name)
    write_disabled_bstats_config(server_dir)

    evidence: dict[str, object] = {
        "python": sys.version,
        "endkeep_wheel": wheel.name,
        "status": "running",
        "checks": [],
        "server": str(server_dir.relative_to(root)),
    }
    log = root / "endkeep-bds.log"
    server = ServerProcess(
        [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(server_dir)],
        root,
        log,
    )

    def checkpoint(name: str, detail: str = "") -> None:
        evidence["checks"].append({"name": name, "detail": detail})
        (root / "endkeep-results.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(f"[EndKeep CP315] {name}: {detail}", flush=True)

    def command(cmd: str, *, timeout: float = 20) -> str:
        start = server.command(cmd)
        output = server.wait_command_output(start, timeout)
        body = "\n".join(output)
        if not server.is_alive():
            raise RuntimeError(f"BDS exited during {cmd}: {body[-2500:]}")
        if any(msg in body.lower() for msg in ("unknown command", "command not found", "repository service is unavailable")):
            raise RuntimeError(f"BDS rejected {cmd}: {body[-2500:]}")
        return body

    try:
        server.start()
        lines = server.wait_for(
            lambda rows: any(any(hint in line.lower() for hint in READY_HINTS) for line in rows),
            240,
            "BDS ready",
        )
        checkpoint("bds-ready", next((line for line in lines if "server started" in line.lower()), ""))

        server.wait_for(
            lambda rows: any("endkeep" in line.lower() and ("enabl" in line.lower() or "load" in line.lower()) for line in rows),
            90,
            "EndKeep enabled",
        )
        checkpoint("endkeep-enabled")

        status = command("backup status")
        if not any(token in status.lower() for token in ("endkeep", "snapshots=", "worker:")):
            raise AssertionError(f"EndKeep status response not found: {status[-2000:]}")
        checkpoint("status", status[-900:])

        capture = command("backup create", timeout=30)
        checkpoint("capture-requested", capture[-900:])

        repository_head: Path | None = None
        for attempt in range(25):
            if not server.is_alive():
                raise RuntimeError("BDS stopped during capture and maintenance")
            if attempt > 0:
                time.sleep(4)
            result = command("backup maintenance", timeout=8)
            if "maintenance started" not in result.lower():
                continue
            checkpoint("maintenance-started", result[-900:])
            break
        else:
            raise RuntimeError("EndKeep did not accept maintenance after a capture")

        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            matches = list(work.rglob("repo/HEAD"))
            if matches:
                repository_head = matches[0]
                if repository_head.read_text(encoding="utf-8").strip():
                    break
            if not server.is_alive():
                raise RuntimeError("BDS exited during EndKeep logicalization")
            time.sleep(3)
        if repository_head is None or not repository_head.exists():
            raise RuntimeError("EndKeep repository HEAD was not committed")
        checkpoint("repository-committed", str(repository_head.relative_to(root)))

        # Wait until the worker is idle before requesting repository verification.
        for attempt in range(35):
            verify = command("backup verify deep", timeout=8)
            if "verification started" in verify.lower():
                checkpoint("deep-verify-started", verify[-500:])
                break
            time.sleep(3)
        else:
            raise RuntimeError("Deep verification request was never accepted")

        server.wait_for(
            lambda rows: any("repository deep verify pass" in line.lower() for line in rows),
            120,
            "EndKeep deep repository verification PASS",
        )
        checkpoint("deep-verify-pass")
        stopped = server.graceful_stop(timeout=70)
        evidence["shutdown_lifecycle"] = server.lifecycle_diagnostic
        evidence["shutdown_exit_code"] = server.process.returncode if server.process else None
        checkpoint("shutdown-diagnostics", json.dumps(server.lifecycle_diagnostic, default=str)[-4000:])
        if not stopped:
            diagnostic = server.lifecycle_diagnostic
            # EndKeep workers intentionally survive plugin reloads and can outlive BDS.
            # Stop the identified worker explicitly, then still require *all* tracked
            # descendants to exit. Never declare success while BDS remains alive.
            if (
                server.process is None
                or server.process.returncode != 0
                or diagnostic.get("bds_child_liveness_after")
                or diagnostic.get("process_tree_verification") != "residual-processes"
            ):
                raise RuntimeError("BDS did not stop successfully: " + json.dumps(diagnostic, default=str)[-3500:])

            sys.path.insert(0, str(root / "endkeep" / "src"))
            from endstone_endkeep.worker.client import RepositoryWorkerClient

            runtime_file = server_dir / "backups" / "worker-runtime.json"
            runtime = RepositoryWorkerClient._load_runtime(runtime_file)
            if runtime is None:
                raise RuntimeError("EndKeep worker runtime missing during shutdown cleanup")
            worker = RepositoryWorkerClient(runtime_file, runtime, desired_priority="background")
            worker.shutdown()
            checkpoint("endkeep-worker-shutdown", f"pid={runtime.pid}")

            deadline = time.monotonic() + 20
            remaining: list[str] = []
            while time.monotonic() < deadline:
                remaining = server.managed_residual_processes()
                if not remaining:
                    break
                time.sleep(0.25)
            if remaining:
                raise RuntimeError("Unclean Windows process tree after worker shutdown: " + ", ".join(remaining))
            checkpoint("all-child-processes-exited")
        checkpoint("bds-graceful-stop")
        server.close()
        evidence["status"] = "PASS"
        return 0
    except BaseException as exc:
        evidence["status"] = "FAIL"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["traceback"] = traceback.format_exc()
        print(evidence["traceback"], file=sys.stderr, flush=True)
        return 1
    finally:
        if server.is_alive():
            server.force_kill_tree()
        server.close()
        (root / "endkeep-results.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
