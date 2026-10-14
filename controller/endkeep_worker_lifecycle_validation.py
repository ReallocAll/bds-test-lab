"""EndKeep real-BDS worker ownership regression tests.

The assertion phase never calls worker.shutdown(); cleanup after FAIL is separate.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import psutil

from controller.bstats import write_disabled_bstats_config
from controller.run_test import READY_HINTS, ServerProcess

ROOT = Path.cwd()
WORK = ROOT / "work" / "endkeep-worker-lifecycle"
SERVER_DIR = WORK / "bedrock_server"
LOG = ROOT / "endkeep-lifecycle-bds.log"
RESULT = ROOT / "endkeep-lifecycle-results.json"
SHA = os.environ["ENDKEEP_SHA"]
sys.path.insert(0, str(ROOT / "endkeep" / "src"))
from endstone_endkeep.worker.client import RepositoryWorkerClient, WorkerError  # noqa: E402


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def live(pid: int, creation: float) -> bool:
    try:
        p = psutil.Process(pid)
        return p.create_time() == creation and p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


class Validation:
    def __init__(self) -> None:
        self.server: ServerProcess | None = None
        self.workers: dict[int, float] = {}
        self.checks: list[dict] = []
        self.evidence = {
            "status": "RUNNING", "endkeep_sha": SHA, "python": sys.version,
            "platform": sys.platform, "started_at": utc(), "checks": self.checks,
            "deployment": os.environ.get("ENDKEEP_DEPLOYMENT", "plugins-wheel"),
            "actions_run_url": f"https://github.com/{os.environ.get("GITHUB_REPOSITORY", "ReallocAll/bds-test-lab")}/actions/runs/{os.environ.get("GITHUB_RUN_ID", "local")}",
            "endstone": __import__("importlib.metadata", fromlist=["version"]).version("endstone"),
            "amulet_leveldb": __import__("importlib.metadata", fromlist=["version"]).version("amulet-leveldb"),
        }
        RESULT.write_text(json.dumps(self.evidence, indent=2), encoding="utf-8")

    def record(self, name: str, passed: bool, **details) -> None:
        self.checks.append({"name": name, "status": "PASS" if passed else "FAIL", "at": utc(), **details})
        self.save()
        print(f"[EndKeep lifecycle] {name}: {'PASS' if passed else 'FAIL'} {details}", flush=True)
        if not passed:
            raise AssertionError(f"{name}: {details}")

    def save(self) -> None:
        RESULT.write_text(json.dumps(self.evidence, indent=2, default=str), encoding="utf-8")

    def runtime(self):
        matches = list(SERVER_DIR.rglob("worker-runtime.json"))
        if len(matches) != 1:
            raise RuntimeError(f"Expected one worker runtime file: {matches}")
        r = RepositoryWorkerClient._load_runtime(matches[0])
        if r is None:
            raise RuntimeError("Worker runtime is invalid")
        return matches[0], r

    def worker(self):
        path, runtime = self.runtime()
        proc = psutil.Process(runtime.pid)
        born = proc.create_time()
        if not live(runtime.pid, born):
            raise RuntimeError(f"Worker {runtime.pid} not alive")
        self.workers[runtime.pid] = born
        return path, runtime, born

    def client(self):
        path, r, _ = self.worker()
        return RepositoryWorkerClient(path, r, desired_priority=r.priority)

    def cmd(self, command: str, timeout: float = 15) -> str:
        assert self.server is not None
        start = self.server.command(command)
        out = self.server.wait_command_output(start, timeout)
        body = "\n".join(out)
        if not self.server.is_alive() or any(x in body.lower() for x in ("unknown command", "repository service is unavailable")):
            raise RuntimeError(f"Command {command!r} failed: {body[-1600:]}")
        return body

    def start(self):
        self.server = ServerProcess(
            [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(SERVER_DIR)],
            ROOT, LOG,
        )
        self.server.start()
        self.server.wait_for(
            lambda rows: any(any(h in row.lower() for h in READY_HINTS) for row in rows),
            240, "real BDS ready",
        )
        self.server.wait_for(
            lambda rows: any("endkeep enabled." in row.lower() for row in rows),
            90, "EndKeep enabled",
        )
        self.cmd("backup status")
        _, runtime, born = self.worker()
        self.evidence.setdefault("servers", []).append({
            "wrapper_pid": self.server.pid, "controller_pid": self.client().refresh()["worker"]["controller_pid"],
            "worker_pid": runtime.pid, "worker_creation_time": born, "bds_started_at": utc(),
        })
        self.save()
        return runtime.pid, born

    def wait_worker_exit(self, pid: int, born: float, timeout: float = 90) -> float:
        began = time.monotonic()
        while time.monotonic() - began < timeout:
            if not live(pid, born):
                return time.monotonic() - began
            time.sleep(0.25)
        raise TimeoutError(f"Worker pid={pid} has not exited after {timeout}s")

    def stop(self, *, name: str) -> None:
        assert self.server is not None and self.server.process is not None
        pid, born = self.worker()[1:]
        # Check product behavior before any test infrastructure process cleanup.
        self.server.command("stop")
        self.server.process.wait(timeout=120)
        code = self.server.process.returncode
        elapsed = self.wait_worker_exit(pid.pid, born)
        self.record(name, code == 0 and not live(pid.pid, born), wrapper_pid=self.server.pid,
                    worker_pid=pid.pid, bds_exit_code=code, worker_exit_delay_s=round(elapsed, 3),
                    worker_residual_count=sum(live(p, t) for p, t in self.workers.items()))
        self.server.close()
        self.server = None

    def start_logicalization(self, name: str):
        assert self.server is not None
        cursor = len(self.server.snapshot())
        self.cmd("backup create", timeout=30)
        self.server.wait_for(lambda rows: any(" captured:" in row for row in rows[cursor:]),
                             120, "real RAW capture publication")
        client = self.client()
        deadline = time.monotonic() + 30
        while client.refresh().get("pending_results") and time.monotonic() < deadline:
            time.sleep(0.1)
        raw_ids = [p.name for p in (SERVER_DIR / "backups" / "raw").iterdir()
                   if p.is_dir() and not p.name.startswith(".")]
        accepted = client.start_maintenance("LOGIC_ONLY")
        state = client.status["job"].get("state")
        self.record(name, bool(raw_ids) and accepted and state == "running",
                    raw_snapshots=raw_ids, job_state=state, worker_pid=client.runtime.pid)
        return client

    def main(self) -> None:
        (SERVER_DIR / "plugins").mkdir(parents=True, exist_ok=True)
        wheel = next((ROOT / "endkeep-wheel").glob("endstone_endkeep-*.whl"))
        if os.environ.get("ENDKEEP_DEPLOYMENT") != "installed-wheel":
            shutil.copy2(wheel, SERVER_DIR / "plugins" / wheel.name)
        from hashlib import sha256
        self.evidence["wheel_sha256"] = sha256(wheel.read_bytes()).hexdigest()
        self.evidence["wheel_name"] = wheel.name
        write_disabled_bstats_config(SERVER_DIR)
        (SERVER_DIR / "server.properties").write_text("allow-cheats=true\nallow-list=true\nonline-mode=true\n", encoding="utf-8")
        version_file = SERVER_DIR / "version.txt"
        first_pid, born = self.start()
        self.record("start-worker", True, worker_pid=first_pid, worker_created_at=utc())
        self.stop(name="normal-stop-no-manual-shutdown")
        self.evidence["bds_version"] = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else None
        self.save()

        for index in range(10):
            pid, born = self.start()
            self.record(f"restart-{index + 1}-single-worker", sum(live(p, t) for p, t in self.workers.items()) == 1,
                        pid=pid)
            self.stop(name=f"restart-{index + 1}-clean-stop")

        pid, born = self.start()
        self.cmd("reload", timeout=30)
        assert self.server is not None
        # Actual Endstone reload must execute the plugin disable and enable hooks.
        lines = self.server.wait_for(
            lambda rows: sum("endkeep disabled." in s.lower() for s in rows) >= 1
            and sum("endkeep enabled." in s.lower() for s in rows) >= 2,
            60, "EndKeep plugin hot reload",
        )
        self.record("real-plugin-reload-hooks", True, disable_count=sum("endkeep disabled." in s.lower() for s in lines))
        self.cmd("backup status")
        after, _ = self.worker()[1:]
        self.record("reload-reuses-worker", pid == after.pid, before=pid, after=after.pid)
        self.stop(name="reload-followed-by-clean-stop")

        pid, born = self.start()
        area = self.cmd("tickingarea add circle 0 64 0 2 endkeep_lab true", timeout=30)
        self.record("real-world-chunk-generation", "added" in area.lower(), output=area[-1200:])
        # Let BDS finish generating the small persistent ticking area.
        time.sleep(10)
        client = self.start_logicalization("reload-during-real-logicalization")
        instance = client.runtime.instance_id
        self.cmd("reload", timeout=40)
        self.server.wait_for(lambda rows: sum("endkeep enabled." in row.lower() for row in rows) >= 2,
                             60, "reload during logicalization completed")
        self.record("running-job-reload-handoff", self.worker()[1].instance_id == instance,
                    before_pid=pid, after_pid=self.worker()[1].pid)
        self.cmd("backup verify deep", timeout=30)
        self.server.wait_for(lambda rows: any("repository deep verify pass" in row.lower() for row in rows),
                             180, "reload task repository integrity")
        self.record("reload-task-deep-verify", True)
        self.stop(name="running-reload-clean-stop")

        pid, born = self.start()
        self.cmd("backup create", timeout=30)
        deadline = time.monotonic() + 150
        started = False
        observed_state = None
        last_rejection = ""
        while time.monotonic() < deadline and not started:
            assert self.server is not None
            start_idx = self.server.command("backup maintenance")
            probe_deadline = time.monotonic() + 2
            while time.monotonic() < probe_deadline:
                observed_state = self.client().refresh()["job"].get("state")
                if observed_state in ("running", "cancel_requested"):
                    started = True
                    break
                time.sleep(0.005)
            if not started:
                last_rejection = "\\n".join(self.server.snapshot()[start_idx:])[-1200:]
                time.sleep(1)
        self.record("stop-during-real-job-observed", started, job_state=observed_state,
                    last_output=last_rejection)
        self.stop(name="stop-during-job-no-orphan")
        pid, born = self.start()
        recovery_logs = [x for x in self.server.snapshot() if "startup recovery" in x.lower()]
        self.record("startup-recovery-reported", bool(recovery_logs), logs=recovery_logs[-4:])
        self.cmd("backup verify deep")
        self.server.wait_for(
            lambda rows: any("repository deep verify pass" in x.lower() for x in rows),
            150, "deep verify pass",
        )
        self.record("real-bds-deep-verify", True)
        self.stop(name="recovery-followed-by-clean-stop")
        repo = SERVER_DIR / "backups" / "repo"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "endkeep" / "src")
        restored = WORK / "restored"
        for cmd in (["verify"], ["restore", str(restored)]):
            proc = subprocess.run(
                [sys.executable, "-m", "endstone_endkeep.offline.cli", "--repo", str(repo), *cmd],
                env=env, capture_output=True, text=True, timeout=240,
            )
            self.record(f"offline-{'-'.join(cmd[:1])}", proc.returncode == 0,
                        stdout=proc.stdout[-1000:], stderr=proc.stderr[-1000:])
        self.record("restored-world-db", (restored / "level.dat").exists() and (restored / "db").is_dir())

        pid, born = self.start()
        owner = int(self.client().refresh()["worker"]["controller_pid"])
        actual = psutil.Process(owner)
        command_line = actual.cmdline()
        launched_bds = any(
            "bedrock_server" in arg.lower() or "endstone" in arg.lower()
            for arg in command_line
        )
        self.record("controller-is-real-bds", launched_bds and owner != pid,
                    controller_pid=owner, process_name=actual.name(), command_line=command_line)
        self.start_logicalization("sigkill-during-real-logicalization")
        actual.suspend()
        try:
            deadline = time.monotonic() + 180
            client = self.client()
            while not client.refresh().get("pending_results") and time.monotonic() < deadline:
                time.sleep(0.1)
            self.record("unacked-result-before-bds-kill", bool(client.status.get("pending_results")),
                        worker_pid=pid, pending_results=client.status.get("pending_results"))
            journal = SERVER_DIR / "backups" / "worker-results.json"
            self.record("unacked-result-durable-before-bds-kill", bool(json.loads(journal.read_text())["results"]))
        except BaseException:
            actual.resume()
            raise
        died_at = time.monotonic()
        actual.kill()
        actual.wait(timeout=30)
        delay = self.wait_worker_exit(pid, born)
        self.record("sigkill-bds-worker-self-exits", True, bds_pid=owner, worker_pid=pid,
                    orphan_lifetime_s=round(time.monotonic() - died_at, 3))
        if self.server.process is not None:
            self.server.process.wait(timeout=60)
        self.server.close()
        self.server = None
        pid, born = self.start()
        self.record("restart-after-sigkill", True, worker_pid=pid)
        self.cmd("backup verify deep")
        self.server.wait_for(lambda rows: any("repository deep verify pass" in row.lower() for row in rows),
                             180, "repository integrity after unacknowledged orphan exit")
        self.record("post-sigkill-deep-verify", True)
        self.stop(name="post-sigkill-clean-stop")

        pid, born = self.start()
        path, runtime, _ = self.worker()
        worker = psutil.Process(pid)
        worker.suspend()
        try:
            resumed = threading.Event()
            def resume():
                time.sleep(1.5)
                worker.resume()
                resumed.set()
            thread = threading.Thread(target=resume, daemon=True)
            thread.start()
            self.cmd("reload", timeout=40)
            self.record("transient-worker-unresponsive-recovers", resumed.wait(10) and
                        self.worker()[1].instance_id == runtime.instance_id)
        finally:
            try:
                worker.resume()
            except psutil.Error:
                pass
        self.cmd("backup status")
        self.record("reload-after-temporary-timeout", True)

        # A persistently unresponsive worker must fail closed, not spawn a second writer.
        worker.suspend()
        try:
            assert self.server is not None
            cursor = self.server.command("reload")
            self.server.wait_for(
                lambda rows: any(
                    "failed to enable endkeep" in line.lower() for line in rows[cursor:]
                ),
                30, "unresponsive worker safely rejected",
            )
            self.record("permanent-rpc-unresponsive-no-second-writer",
                        live(pid, born) and self.worker()[1].instance_id == runtime.instance_id,
                        old_worker_pid=pid)
        finally:
            worker.resume()
        self.cmd("reload", timeout=20)
        assert self.server is not None
        self.server.wait_for(
            lambda rows: sum("endkeep enabled." in x.lower() for x in rows) >= 3,
            40, "EndKeep reenabled after suspended worker resumed",
        )
        self.record("unresponsive-worker-recovered-after-resume",
                    self.worker()[1].instance_id == runtime.instance_id, worker_pid=pid)

        before_head = (SERVER_DIR / "backups" / "repo" / "HEAD")
        head = before_head.read_bytes() if before_head.exists() else b""
        other = subprocess.run([
            sys.executable, "-m", "endstone_endkeep.worker", "--storage-root", str(path.parent),
            "--runtime-file", str(path), "--priority", "background",
            "--controller-pid", str(os.getpid()), "--controller-start", "invalid",
            "--launch-id", "competing-worker-real-bds",
        ], env=env, capture_output=True, text=True, timeout=20)
        self.record("two-worker-writer-lock", other.returncode != 0 and "repository is locked" in other.stderr.lower()
                    and live(pid, born) and
                    self.worker()[1].instance_id == runtime.instance_id and
                    (before_head.read_bytes() if before_head.exists() else b"") == head,
                    competing_exit_code=other.returncode, competing_stderr=other.stderr[-1500:])
        self.stop(name="final-clean-stop")

    def run(self) -> int:
        try:
            self.main()
            self.evidence["status"] = "PASS"
            return 0
        except BaseException as e:
            self.evidence["status"] = "FAIL"
            self.evidence["error"] = f"{type(e).__name__}: {e}"
            self.evidence["traceback"] = traceback.format_exc()
            print(self.evidence["traceback"], file=sys.stderr, flush=True)
            return 1
        finally:
            # Explicit post-result cleanup must never change a failed verdict.
            if self.server is not None:
                if self.server.is_alive():
                    self.server.force_kill_tree()
                self.server.close()
            for pid, born in self.workers.items():
                if live(pid, born):
                    try:
                        psutil.Process(pid).resume()
                        psutil.Process(pid).kill()
                    except psutil.Error:
                        pass
            self.evidence["cleanup_after_verdict"] = utc()
            self.evidence["completed_at"] = utc()
            self.save()


if __name__ == "__main__":
    raise SystemExit(Validation().run())
