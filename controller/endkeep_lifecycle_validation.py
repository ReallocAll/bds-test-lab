"""Exact-Wheel real BDS lifecycle acceptance; finally cleanup never changes verdicts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path

import psutil
from controller.bstats import write_disabled_bstats_config
from controller.run_test import READY_HINTS, ServerProcess, child_process_env


def utc() -> str:
    return datetime.now(UTC).isoformat()


def wait_for(predicate, timeout: float, detail: str, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timeout: {detail}")


def main() -> int:
    root = Path.cwd()
    wheel = next((root / "endkeep-wheel").glob("endstone_endkeep-*.whl"))
    # Inspector imports the packaged Wheel, never the candidate checkout.
    sys.path.insert(0, str(wheel.resolve()))
    from endstone_endkeep.offline.cli import command_restore
    from endstone_endkeep.repository.lock import RepositoryLock, RepositoryLockError
    from endstone_endkeep.repository.manifest import ManifestStore
    from endstone_endkeep.repository.objects import ObjectStore
    from endstone_endkeep.repository.verify import RepositoryVerifier
    from endstone_endkeep.worker.client import RepositoryWorkerClient, WorkerError
    from endstone_endkeep.worker.process import process_alive

    server_dir = root / "work" / "endkeep-lifecycle" / "bedrock_server"
    plugins = server_dir / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    shutil.copy2(wheel, plugins / wheel.name)
    write_disabled_bstats_config(server_dir)
    config_dir = plugins / "endkeep"
    config_dir.mkdir(exist_ok=True)
    from zipfile import ZipFile

    import tomlkit

    with ZipFile(wheel) as archive:
        config = tomlkit.parse(archive.read("endstone_endkeep/config.toml").decode())
    config["storage"]["min_free_space_gib"] = 1
    later = datetime.now(UTC) + timedelta(hours=6)
    config["capture"]["times"] = [later.strftime("%H:%M")]
    config["maintenance"]["times"] = [(later + timedelta(minutes=1)).strftime("%H:%M")]
    (config_dir / "config.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    storage = server_dir / "backups"
    runtime_path = storage / "worker-runtime.json"
    evidence = {
        "status": "running",
        "endkeep_sha": os.environ["ENDKEEP_SHA"],
        "run_url": os.environ["RUN_URL"],
        "python": sys.version,
        "endstone": version("endstone"),
        "amulet_leveldb": version("amulet-leveldb"),
        "wheel": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "scenarios": [],
        "sessions": [],
        "cleanup": [],
    }
    server = None
    known_workers = {}
    suspended = None

    def save():
        (root / "endkeep-lifecycle-results.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8"
        )

    def workers():
        rows = []
        for proc in psutil.process_iter(
            ["pid", "cmdline", "create_time", "ppid", "exe"]
        ):
            args = proc.info["cmdline"] or []
            if "endstone_endkeep.worker" not in args or "--storage-root" not in args:
                continue
            if (
                Path(args[args.index("--storage-root") + 1]).resolve()
                == storage.resolve()
            ):
                rows.append(
                    {
                        "pid": proc.pid,
                        "created_at": proc.info["create_time"],
                        "parent_pid": proc.info["ppid"],
                        "executable": proc.info["exe"],
                    }
                )
        return rows

    def worker_processes():
        rows = workers()
        runtime = RepositoryWorkerClient._load_runtime(runtime_path)
        ancestors = set()
        if runtime is not None:
            try:
                ancestors = {p.pid for p in psutil.Process(runtime.pid).parents()}
            except psutil.NoSuchProcess:
                pass
        for row in rows:
            row["role"] = "worker"
            # Windows venv python.exe redirects to the base interpreter and waits for it.
            if os.name == "nt" and row["pid"] in ancestors:
                assert (
                    Path(row["executable"]).resolve() == Path(sys.executable).resolve()
                ), row
                row["role"] = "venv-launcher"
        return rows

    def writer_count():
        return sum(row["role"] == "worker" for row in worker_processes())

    def inspect():
        runtime = RepositoryWorkerClient._load_runtime(runtime_path)
        if runtime is None:
            return None
        return RepositoryWorkerClient(
            runtime_path, runtime, desired_priority="background"
        )

    def ping():
        client = inspect()
        if client is None:
            return None
        try:
            return client._rpc({"command": "ping"})["status"]
        except WorkerError:
            return None

    def start(*, expected_failure=False):
        nonlocal server
        index = len(evidence["sessions"])
        server = ServerProcess(
            [
                sys.executable,
                "-m",
                "endstone",
                "--yes",
                "--server-folder",
                str(server_dir),
            ],
            root,
            root / f"endkeep-lifecycle-{index:02}.log",
        )
        server.start()
        server.wait_for(
            lambda rows: any(
                any(h in line.lower() for h in READY_HINTS) for line in rows
            ),
            240,
            "BDS ready",
        )
        if expected_failure:
            server.wait_for(
                lambda rows: any("Failed to enable EndKeep:" in line for line in rows),
                30,
                "safe refusal",
            )
            evidence["sessions"].append(
                {
                    "wrapper_pid": server.pid,
                    "started_at": utc(),
                    "expected_refusal": True,
                }
            )
            return
        server.wait_for(
            lambda rows: any("EndKeep enabled." in line for line in rows),
            30,
            "EndKeep enabled",
        )
        status = wait_for(ping, 10, "Worker RPC ready")
        client = inspect()
        assert client is not None
        worker = client.runtime
        owner_pid = status["worker"]["controller"]["pid"]
        proc = psutil.Process(owner_pid)
        assert "bedrock_server" in Path(proc.exe()).name.lower(), (
            owner_pid,
            proc.name(),
            proc.cmdline(),
        )
        session = {
            "wrapper_pid": server.pid,
            "bds_pid": owner_pid,
            "worker_pid": worker.pid,
            "worker_identity": worker.process_identity,
            "worker_created_at": psutil.Process(worker.pid).create_time(),
            "started_at": utc(),
            "rpc": "PASS",
            "worker_count": writer_count(),
            "worker_processes": worker_processes(),
            "bds_executable": proc.exe(),
        }
        known_workers[worker.pid] = worker.process_identity
        assert session["worker_count"] == 1, workers()
        assert not any("alive but not responding" in line for line in server.snapshot())
        versions = [
            line
            for line in server.snapshot()
            if re.search(r"Version:|BDS.*version", line, re.IGNORECASE)
        ]
        evidence["bds_version_lines"] = versions
        evidence["sessions"].append(session)
        save()

    def stop(*, crash=False, timeout=120):
        assert server is not None
        client = inspect()
        assert client is not None
        worker = client.runtime
        status = client._rpc({"command": "ping"})["status"]
        owner = psutil.Process(status["worker"]["controller"]["pid"])
        began = time.monotonic()
        if crash:
            owner.kill()  # Only BDS: never kill a process group or the Worker.
            wait_for(
                lambda: (
                    not process_alive(
                        owner.pid, status["worker"]["controller"]["identity"]
                    )
                ),
                15,
                "BDS death",
            )
            death_observed = time.monotonic()
        else:
            server.command("stop")
        if server.process is not None:
            server.process.wait(timeout=70)
            if not crash:
                assert server.process.returncode == 0, server.snapshot()[-30:]
        if not crash:
            death_observed = time.monotonic()
        wait_for(
            lambda: not process_alive(worker.pid, worker.process_identity),
            timeout,
            "Worker self-exit",
        )
        assert not runtime_path.exists(), "runtime file survived Worker exit"
        assert workers() == [], workers()
        row = evidence["sessions"][-1]
        row.update(
            {
                "stop": "BDS-only forced exit" if crash else "normal",
                "worker_exited_at": utc(),
                "worker_exit_observed": True,
                "residual_workers": 0,
                "shutdown_seconds": time.monotonic() - began,
                "bds_death_to_worker_exit_seconds": time.monotonic() - death_observed,
                "wrapper_returncode": server.process.returncode,
            }
        )
        with RepositoryLock(storage / "worker-control"):
            row["worker_lock_released"] = True
        with RepositoryLock(storage / "repo"):
            row["repository_lock_released"] = True
        server.close()
        save()

    def command(cmd, timeout=15):
        offset = server.command(cmd)
        output = "\n".join(server.wait_command_output(offset, timeout))
        assert server.is_alive(), output
        assert "Unknown command" not in output, output
        return output

    def idle():
        status = ping()
        return status and not status["job_occupied"] and not status["pending_results"]

    def capture():
        wait_for(idle, 30, "idle before RAW capture")
        offset = server.command("backup create")
        server.wait_for(
            lambda rows: any("captured: files=" in line for line in rows[offset:]),
            60,
            "real RAW captured",
        )
        raw = sorted((storage / "raw").iterdir())
        assert raw, "no RAW capture"
        return raw

    def busy_maintenance():
        wait_for(idle, 30, "idle before logicalization")
        server.command("backup maintenance")

        def occupied():
            state = ping()
            return (
                state
                if state
                and state["job_occupied"]
                and state["job"]["kind"] == "maintenance"
                and state["job"]["state"] == "running"
                and state["job"]["detail"] is not None
                else None
            )

        return wait_for(occupied, 5, "actual logicalization running", interval=0.01)

    def verify_restore(name):
        manifests = ManifestStore(storage / "repo")
        objects = ObjectStore(
            storage / "repo", compression_level=6, compression_threads=1
        )
        with RepositoryLock(storage / "repo"):
            report = RepositoryVerifier(manifests, objects).verify(deep=True)
            manifest = manifests.load_current()
            assert manifest is not None and manifest.chain
        destination = root / "work" / f"restore-{name}"
        command_restore(
            storage / "repo",
            destination,
            manifest.chain[-1].snapshot,
            show_progress=False,
        )
        assert (destination / "db").is_dir() and (destination / "level.dat").is_file()
        return {
            "deep_verify": str(report),
            "restored_snapshot": manifest.chain[-1].snapshot,
            "restore": "PASS",
        }

    def scenario(name, operation):
        row = {"name": name, "started_at": utc(), "status": "running"}
        evidence["scenarios"].append(row)
        save()
        try:
            row["detail"] = operation()
            row["status"] = "PASS"
        except BaseException as exc:
            row["status"] = "FAIL"
            row["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            row["finished_at"] = utc()
            save()
            print(json.dumps(row), flush=True)

    try:

        def restarts():
            for _ in range(10):
                start()
                stop()
            return {"normal_stop_iterations": 10, "residual_workers": 0}

        scenario("normal-start-stop-and-ten-restarts", restarts)

        def reload():
            start()
            command("tickingarea add circle 0 64 0 4 endkeep-test")
            command("fill 0 60 0 127 60 127 stone")
            capture()
            before = inspect().runtime
            active = busy_maintenance()
            offset = server.command("reload")
            server.wait_for(
                lambda rows: any("Reload complete." in line for line in rows[offset:]),
                60,
                "framework reload",
            )
            server.wait_for(
                lambda rows: any("EndKeep enabled." in line for line in rows[offset:]),
                30,
                "plugin re-enabled",
            )
            after = inspect().runtime
            assert before.instance_id == after.instance_id and before.pid == after.pid
            assert writer_count() == 1
            wait_for(idle, 120, "reload task result delivered")
            stop()
            return {
                "running_job": active["job"],
                "same_worker_pid": before.pid,
                **verify_restore("reload"),
            }

        scenario("actual-framework-reload-during-task", reload)

        def stop_busy(crash):
            start()
            capture()
            active = busy_maintenance()
            stop(crash=crash)
            saved = json.loads((storage / "worker-results.json").read_text())
            assert saved["results"], "completion result not retained for restart"
            start()
            assert any("Startup recovery:" in line for line in server.snapshot())
            wait_for(idle, 120, "persisted completion ACK after restart")
            stop()
            return {
                "running_job": active["job"],
                "persisted_results": len(saved["results"]),
                **verify_restore("crash" if crash else "stop"),
            }

        scenario("normal-stop-during-real-logicalization", lambda: stop_busy(False))
        scenario(
            "BDS-only-force-termination-during-logicalization", lambda: stop_busy(True)
        )

        def timeouts():
            nonlocal suspended
            start()
            before = inspect().runtime
            suspended = psutil.Process(before.pid)
            suspended.suspend()
            timer = threading.Timer(0.9, suspended.resume)
            timer.start()
            try:
                output = command("backup reload", 15)
                assert "EndKeep configuration reloaded." in output, output
            finally:
                timer.join()
                suspended.resume()
                suspended = None
            assert inspect().runtime.instance_id == before.instance_id
            # Freeze the old Worker across a real BDS restart. Safety requires refusal.
            suspended = psutil.Process(before.pid)
            suspended.suspend()
            owner_pid = evidence["sessions"][-1]["bds_pid"]
            psutil.Process(owner_pid).kill()
            server.process.wait(timeout=30)
            server.close()
            start(expected_failure=True)
            assert (
                writer_count() == 1
                and inspect().runtime.instance_id == before.instance_id
            )
            server.command("stop")
            server.process.wait(timeout=70)
            server.close()
            suspended.resume()
            suspended = None
            wait_for(
                lambda: not process_alive(before.pid, before.process_identity),
                120,
                "old Worker drain after resume",
            )
            assert workers() == []
            start()
            stop()
            return {
                "transient_timeout_same_pid": before.pid,
                "unresponsive_start_safely_refused": True,
                "recovery_after_owner_death": "PASS",
            }

        scenario("transient-and-community-unresponsive-worker", timeouts)

        def competitors():
            start()
            first = inspect().runtime
            saved = runtime_path.read_bytes()
            env = child_process_env()
            env["PYTHONPATH"] = str(wheel.resolve())
            challenger = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "endstone_endkeep.worker",
                    "--storage-root",
                    str(storage),
                    "--runtime-file",
                    str(runtime_path),
                    "--priority",
                    "background",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert challenger.returncode != 0, challenger.stdout
            assert runtime_path.read_bytes() == saved and writer_count() == 1
            with_repository_lock = False
            try:
                with RepositoryLock(storage / "worker-control"):
                    with_repository_lock = True
            except RepositoryLockError:
                pass
            assert not with_repository_lock, "active Worker did not own OS lock"
            assert ping()["worker"]["pid"] == first.pid
            stop()
            return {
                "challenger_returncode": challenger.returncode,
                "worker_count": 1,
                "OS_lock_exclusive": True,
                **verify_restore("competitors"),
            }

        scenario("second-worker-OS-lock-exclusion", competitors)
        evidence["status"] = "PASS"
        return 0
    except BaseException as exc:
        evidence["status"] = "FAIL"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["traceback"] = traceback.format_exc()
        print(evidence["traceback"], file=sys.stderr, flush=True)
        return 1
    finally:
        # Runner hygiene only, after verdict: this can never turn a scenario into PASS.
        if suspended is not None:
            try:
                suspended.resume()
            except psutil.Error:
                pass
        if server is not None:
            if server.is_alive():
                server.force_kill_tree()
                evidence["cleanup"].append("forced server tree cleanup after FAIL")
            server.close()
        for pid, identity in known_workers.items():
            if process_alive(pid, identity):
                evidence["cleanup"].append(
                    {"worker_pid": pid, "reason": "residual after FAIL"}
                )
                psutil.Process(pid).kill()
        if storage.exists():
            for name in ("worker.log", "worker-results.json"):
                if (storage / name).exists():
                    shutil.copy2(storage / name, root / ("endkeep-" + name))
        save()


if __name__ == "__main__":
    raise SystemExit(main())
