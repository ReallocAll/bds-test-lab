#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from typing import Any

import psutil

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from controller.bstats import (
    B_STATS_CONFIG_RELATIVE_PATH,
    BStatsConfigError,
    copy_bstats_evidence,
    write_disabled_bstats_config,
)
from providers.artifact_provider import resolve_artifacts

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
VIEWER_RE = re.compile(r"https://spark\.lucko\.me/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
READY_HINTS = ("server started.", "server started in", "server started")
SPARK_LOAD_HINTS = ("enabling spark", "enabled spark", "spark v", "loaded spark")
_CHILD_SECRET_ENV_NAMES = frozenset({"GH_TOKEN", "REPO_PAT"})


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_line(line: str) -> str:
    return ANSI_RE.sub("", line).strip()


def write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def child_process_env() -> dict[str, str]:
    """Copy the controller environment without GitHub API credentials."""

    environment = os.environ.copy()
    for key in list(environment):
        if key.upper() in _CHILD_SECRET_ENV_NAMES:
            environment.pop(key, None)
    return environment


def run_checked(cmd: list[str], timeout: int = 300, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(str(x) for x in cmd), flush=True)
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, timeout=timeout, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def locate_one(root: pathlib.Path, patterns: list[str]) -> pathlib.Path:
    matches: list[pathlib.Path] = []
    for pattern in patterns:
        matches.extend(root.rglob(pattern))
    matches = [p for p in matches if p.is_file()]
    if not matches:
        raise FileNotFoundError(f"No file matching {patterns} under {root}")
    matches.sort(key=lambda p: (len(p.parts), str(p)))
    return matches[0]


class ServerProcess:
    def __init__(self, cmd: list[str], cwd: pathlib.Path, log_path: pathlib.Path):
        self.cmd = [str(argument) for argument in cmd]
        self.cwd = cwd
        self.log_path = log_path
        self.process: subprocess.Popen[str] | None = None
        self.pid: int | None = None
        self.create_time: float | None = None
        self.started_command: list[str] | None = None
        self.lines: list[str] = []
        self.lifecycle_diagnostic: dict[str, Any] = {}
        self._forced = False
        self._managed_processes: dict[int, float | None] = {}
        self._root_identity_status = "unknown"
        self._root_identity_evidence: dict[str, Any] = {}
        self._process_tree_error: str | None = None
        self._unverified_processes: dict[int, str] = {}
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._log = None

    def start(self) -> None:
        kwargs: dict[str, Any] = {"env": child_process_env()}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self._log = self.log_path.open("a", encoding="utf-8", errors="replace")
        self.started_command = list(self.cmd)
        self.process = subprocess.Popen(self.cmd, cwd=str(self.cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
        self.pid = self.process.pid
        try:
            self.create_time = psutil.Process(self.pid).create_time()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            self.create_time = None
        self._managed_processes = {self.pid: self.create_time}
        self._reader = threading.Thread(target=self._read_loop, name="bds-log-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for raw in self.process.stdout:
            line = clean_line(raw)
            with self._lock:
                self.lines.append(line)
            if self._log is not None:
                self._log.write(raw)
                self._log.flush()
            print(line, flush=True)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def was_forced(self) -> bool:
        return self._forced

    @staticmethod
    def _same_process_identity(expected: float | None, actual: float | None) -> bool:
        return expected is not None and actual is not None and expected == actual

    def process_tree_snapshot(self) -> list[dict[str, Any]]:
        """Return liveness for the wrapper and descendants known to this server."""

        self._process_tree_error = None
        if self.pid is None:
            self._root_identity_status = "unknown"
            self._root_identity_evidence = {}
            self._process_tree_error = "wrapper-pid-unavailable"
            self._unverified_processes = {}
            return []
        expected_root = self._managed_processes.get(self.pid)
        if expected_root is None:
            expected_root = self.create_time
        if expected_root is not None:
            self._managed_processes[self.pid] = expected_root
        process_ids = set(self._managed_processes)
        unverified_processes = getattr(self, "_unverified_processes", {})
        self._unverified_processes = unverified_processes
        process_ids.update(unverified_processes)
        root = None
        observed_root: float | None = None
        root_status = "unknown"
        try:
            root = psutil.Process(self.pid)
        except psutil.NoSuchProcess:
            process_exited = self.process is None or self.process.poll() is not None
            root_status = "absent" if expected_root is not None and process_exited else "unknown"
        except (psutil.AccessDenied, OSError) as exc:
            self._process_tree_error = type(exc).__name__
        else:
            try:
                observed_root = root.create_time()
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError) as exc:
                self._process_tree_error = type(exc).__name__
            else:
                if expected_root is None:
                    root_status = "unknown"
                elif self._same_process_identity(expected_root, observed_root):
                    root_status = "verified"
                else:
                    root_status = "mismatch"
        self._root_identity_status = root_status
        self._root_identity_evidence = {
            "status": root_status,
            "expected_create_time": expected_root,
            "observed_create_time": observed_root,
        }

        if root_status == "verified" and root is not None:
            try:
                descendants = root.children(recursive=True)
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError) as exc:
                self._process_tree_error = type(exc).__name__
                descendants = []
            for process in [root, *descendants]:
                try:
                    create_time = process.create_time()
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    if process.pid != self.pid:
                        process_ids.add(process.pid)
                        self._unverified_processes[process.pid] = "creation-time-unavailable"
                    continue
                expected = self._managed_processes.get(process.pid)
                if process.pid == self.pid:
                    expected = expected_root
                if expected is not None and expected != create_time:
                    if process.pid != self.pid:
                        process_ids.add(process.pid)
                        self._unverified_processes[process.pid] = "identity-mismatch"
                    continue
                if process.pid != self.pid and create_time is None:
                    process_ids.add(process.pid)
                    self._unverified_processes[process.pid] = "creation-time-unavailable"
                    continue
                if create_time is not None:
                    self._managed_processes[process.pid] = create_time
                    self._unverified_processes.pop(process.pid, None)
                process_ids.add(process.pid)
        elif root_status not in ("absent",):
            process_ids.add(self.pid)

        records: list[dict[str, Any]] = []
        for pid in sorted(process_ids):
            expected = self._managed_processes.get(pid)
            record: dict[str, Any] = {
                "pid": pid,
                "is_wrapper": pid == self.pid,
                "create_time": expected,
                "alive": None if root_status not in ("verified", "absent") else False,
                "identity_match": False,
            }
            if pid in self._unverified_processes or root_status not in ("verified", "absent"):
                record["error"] = self._unverified_processes.get(pid, f"wrapper-identity-{root_status}")
                records.append(record)
                continue
            if pid == self.pid and root_status == "absent":
                record["identity_match"] = True
                record["alive"] = False
                records.append(record)
                continue
            try:
                process = psutil.Process(pid)
                actual = process.create_time()
                record["create_time"] = actual
                record["identity_match"] = self._same_process_identity(expected, actual)
                if not record["identity_match"]:
                    record["error"] = "identity-mismatch"
                    records.append(record)
                    continue
                try:
                    record["name"] = process.name()
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    record["name"] = None
                try:
                    record["alive"] = process.is_running()
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    record["alive"] = None
            except psutil.NoSuchProcess:
                record["identity_match"] = True
                record["alive"] = False
            except (psutil.AccessDenied, OSError) as exc:
                record["error"] = type(exc).__name__
                record["alive"] = None
            records.append(record)
        return records

    def managed_residual_processes(self) -> list[str]:
        return [
            f"{record.get('name') or 'unknown'} (pid={record['pid']})"
            for record in self.process_tree_snapshot()
            if record.get("alive") is True and record.get("identity_match") is True
        ]

    @staticmethod
    def _bds_child_liveness(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            record
            for record in records
            if not record.get("is_wrapper") and "bedrock_server" in str(record.get("name") or "").lower()
        ]

    def _process_tree_cleanup_outcome(self, records: list[dict[str, Any]]) -> str:
        if (
            getattr(self, "_root_identity_status", "unknown") not in ("verified", "absent")
            or getattr(self, "_process_tree_error", None) is not None
            or getattr(self, "_unverified_processes", {})
            or any(record.get("alive") is None for record in records)
            or any(record.get("identity_match") is not True for record in records)
        ):
            return "verification-failed"
        if any(record.get("alive") is True and record.get("identity_match") is True for record in records):
            return "residual-processes"
        return "clean"

    def _windows_root_target_status(self, records: list[dict[str, Any]]) -> str:
        status = getattr(self, "_root_identity_status", "unknown")
        if status == "mismatch":
            return status
        wrapper = next((record for record in records if record.get("is_wrapper")), None)
        if status == "absent":
            return "absent" if wrapper is None or wrapper.get("identity_match") is True else "unknown"
        if status == "verified":
            if wrapper is None or wrapper.get("identity_match") is not True:
                return "unknown"
            if wrapper.get("alive") is True:
                return "verified"
            if wrapper.get("alive") is False:
                return "absent"
            return "unknown"
        if wrapper is not None and wrapper.get("identity_match") is True:
            if wrapper.get("alive") is True:
                return "verified"
            if wrapper.get("alive") is False:
                return "absent"
        return status

    def _begin_lifecycle(self, method: str, command: str, timeout: float) -> dict[str, Any]:
        before = self.process_tree_snapshot()
        diagnostic: dict[str, Any] = {
            "method": method,
            "stop_method": method,
            "command": command,
            "wrapper_pid": self.pid,
            "pid": self.pid,
            "timeout_seconds": timeout,
            "alive_before": self.is_alive(),
            "wrapper_identity": copy.deepcopy(getattr(self, "_root_identity_evidence", {})),
            "process_tree_before": before,
            "bds_child_liveness_before": self._bds_child_liveness(before),
            "acknowledgement_evidence": {"command_sent": False, "observed": False},
            "timeout_reason": None,
            "cleanup_outcome": "not_attempted",
        }
        self.lifecycle_diagnostic = diagnostic
        return diagnostic

    def _finish_lifecycle(
        self,
        diagnostic: dict[str, Any],
        *,
        outcome: str,
        returncode: int | None = None,
        timeout_reason: str | None = None,
    ) -> None:
        after = self.process_tree_snapshot()
        diagnostic["outcome"] = outcome
        diagnostic["returncode"] = returncode
        diagnostic["return_code"] = returncode
        diagnostic["process_tree_after"] = after
        diagnostic["bds_child_liveness_after"] = self._bds_child_liveness(after)
        diagnostic["process_tree_verification"] = self._process_tree_cleanup_outcome(after)
        if timeout_reason is not None:
            diagnostic["timeout_reason"] = timeout_reason

    def wait_for(self, predicate, timeout: float, description: str) -> list[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.snapshot()
            if predicate(lines):
                return lines
            if not self.is_alive():
                code = self.process.poll() if self.process else None
                raise RuntimeError(f"Server exited with code {code} while waiting for {description}")
            time.sleep(0.5)
        raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for {description}")

    def command(self, command: str) -> int:
        if not self.is_alive() or self.process is None or self.process.stdin is None:
            raise RuntimeError(f"Cannot send command to stopped server: {command}")
        start = len(self.snapshot())
        print(f"> {command}", flush=True)
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        return start

    def wait_command_output(self, start_index: int, timeout: float = 8.0) -> list[str]:
        deadline = time.monotonic() + timeout
        previous_count = start_index
        stable_since: float | None = None
        while time.monotonic() < deadline:
            if not self.is_alive():
                break
            lines = self.snapshot()
            count = len(lines)
            if count > previous_count:
                previous_count = count
                stable_since = time.monotonic()
            elif count > start_index and stable_since is not None and time.monotonic() - stable_since >= 1.0:
                break
            time.sleep(0.25)
        return self.snapshot()[start_index:]

    def graceful_stop(self, timeout: float = 60.0) -> bool:
        if self._forced:
            diagnostic = self.lifecycle_diagnostic or self._begin_lifecycle("native-stop", "stop", timeout)
            self._finish_lifecycle(diagnostic, outcome="forced", returncode=self.process.returncode if self.process else None)
            diagnostic["cleanup_outcome"] = "forced"
            return False
        diagnostic = self._begin_lifecycle("native-stop", "stop", timeout)
        if not diagnostic["alive_before"]:
            if os.name == "nt" and diagnostic["wrapper_identity"].get("status") != "absent":
                self._finish_lifecycle(diagnostic, outcome="verification-failed", returncode=self.process.returncode if self.process else None)
                diagnostic["cleanup_outcome"] = "verification-failed"
                return False
            self._finish_lifecycle(diagnostic, outcome="already-exited", returncode=self.process.returncode if self.process else None)
            return os.name != "nt" or diagnostic["process_tree_verification"] == "clean"
        if os.name == "nt" and diagnostic["wrapper_identity"].get("status") != "verified":
            self._finish_lifecycle(diagnostic, outcome="verification-failed", returncode=self.process.returncode if self.process else None)
            diagnostic["cleanup_outcome"] = "verification-failed"
            return False
        try:
            self.command("stop")
            diagnostic["acknowledgement_evidence"]["command_sent"] = True
        except Exception:  # noqa: BLE001 - shutdown must report failure without hiding it
            self._finish_lifecycle(diagnostic, outcome="command-failed", returncode=self.process.returncode if self.process else None)
            return False
        assert self.process is not None
        try:
            self.process.wait(timeout=timeout)
            returncode = self.process.returncode
            self._finish_lifecycle(diagnostic, outcome="exited" if returncode == 0 else "nonzero-exit", returncode=returncode)
            if os.name == "nt" and diagnostic["process_tree_verification"] != "clean":
                diagnostic["outcome"] = diagnostic["process_tree_verification"]
                diagnostic["cleanup_outcome"] = diagnostic["process_tree_verification"]
                return False
            diagnostic["cleanup_outcome"] = "graceful-exit" if returncode == 0 else "nonzero-exit"
            return returncode == 0
        except subprocess.TimeoutExpired:
            self._finish_lifecycle(
                diagnostic,
                outcome="timeout",
                returncode=self.process.returncode,
                timeout_reason=f"process did not exit within {timeout:.1f}s",
            )
            return False

    def force_kill_tree(self) -> None:
        self._forced = True
        diagnostic = self.lifecycle_diagnostic
        if not diagnostic:
            diagnostic = self._begin_lifecycle("force-kill-tree", "taskkill" if os.name == "nt" else "SIGKILL", 15.0)
        before = self.process_tree_snapshot()
        diagnostic["forced"] = True
        diagnostic["process_tree_before_force"] = before
        alive_records = [
            record
            for record in before
            if record.get("alive") is True and record.get("identity_match") is True
        ]
        root_target_status = self._windows_root_target_status(before)
        if root_target_status in ("verified", "absent"):
            self._root_identity_status = root_target_status
        if self.process is not None and self.process.poll() is None:
            pid = self.process.pid
            if os.name == "nt" and root_target_status == "verified":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30, check=False)
            else:
                if os.name != "nt":
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if os.name != "nt" or root_target_status == "verified":
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired as exc:
                        diagnostic["forced_wait_timeout"] = str(exc)
        elif os.name == "nt" and root_target_status in ("verified", "absent"):
            for record in alive_records:
                if record["pid"] == self.pid:
                    continue
                subprocess.run(["taskkill", "/PID", str(record["pid"]), "/T", "/F"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30, check=False)
        after = self.process_tree_snapshot()
        if os.name == "nt" and root_target_status in ("verified", "absent"):
            residual_records = [
                record
                for record in after
                if record.get("alive") is True
                and record.get("identity_match") is True
                and record.get("pid") != self.pid
            ]
            for record in residual_records:
                subprocess.run(["taskkill", "/PID", str(record["pid"]), "/T", "/F"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30, check=False)
            if residual_records:
                after = self.process_tree_snapshot()
        diagnostic["process_tree_after_force"] = after
        diagnostic["bds_child_liveness_after_force"] = self._bds_child_liveness(after)
        diagnostic["cleanup_outcome"] = self._process_tree_cleanup_outcome(after)
        diagnostic["outcome"] = "forced"
        diagnostic["returncode"] = self.process.returncode if self.process is not None else None
        diagnostic["return_code"] = diagnostic["returncode"]

    def close(self) -> None:
        if self._reader is not None:
            self._reader.join(timeout=3)
        if self.process is not None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
        if self._log is not None:
            self._log.close()
            self._log = None


class IntegrationTest:
    disable_bstats = False

    def __init__(self, platform_name: str):
        self.platform = platform_name
        self.root = pathlib.Path.cwd()
        self.downloads = self.root / "downloads"
        self.server_dir = self.root / "work" / platform_name / "bedrock_server"
        self.log_path = self.root / "bds.log"
        self.result_path = self.root / "test-results.json"
        self.metadata_path = self.root / "metadata.json"
        self.endstone_log = self.root / "endstone.log"
        self.spark_log = self.root / "spark.log"
        self.diagnostics = self.root / "failure-diagnostics.txt"
        self.server: ServerProcess | None = None
        self.metadata: dict[str, Any] = {}
        self.result: dict[str, Any] = {
            "platform": platform_name,
            "status": "running",
            "state": "running",
            "started_at": now_iso(),
            "completed_at": None,
            "failed_stage": None,
            "error_summary": None,
            "bds_version": None,
            "checks": [],
            "execution_profile_viewer_url": None,
            "allocation_profile_viewer_url": None,
            "shutdown_status": "not_started",
            "shutdown_lifecycle_events": [],
        }
        write_json(self.result_path, self.result)

    def check(self, name: str, status: str, detail: str | None = None, **extra: Any) -> None:
        item: dict[str, Any] = {"name": name, "status": status}
        if detail:
            item["detail"] = detail
        item.update(extra)
        self.result["checks"].append(item)
        write_json(self.result_path, self.result)

    def record_server_lifecycle(self) -> None:
        diagnostic = getattr(self.server, "lifecycle_diagnostic", None) if self.server is not None else None
        if diagnostic:
            snapshot = copy.deepcopy(diagnostic)
            self.result["shutdown_lifecycle"] = snapshot
            events = self.result.setdefault("shutdown_lifecycle_events", [])
            if not events or events[-1] != snapshot:
                events.append(snapshot)
            write_json(self.result_path, self.result)

    def install_artifacts(self) -> None:
        self.metadata = resolve_artifacts(self.platform, self.downloads, self.metadata_path)
        self.check("artifact-discovery", "PASS")
        endstone_root = self.downloads / "endstone" / "payload"
        wheel = locate_one(endstone_root, ["endstone-*-cp313-cp313-*.whl", "endstone-*.whl"])
        self.check("endstone-wheel-located", "PASS", str(wheel.relative_to(self.root)))
        run_checked([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall", str(wheel)], timeout=300)
        spark_root = self.downloads / "spark" / "payload"
        spark_binary = locate_one(spark_root, ["endstone_spark.so"] if self.platform == "linux" else ["endstone_spark.dll"])
        self.server_dir.mkdir(parents=True, exist_ok=True)
        plugin_dir = self.server_dir / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        self._prepare_bstats_before_start()
        target = plugin_dir / spark_binary.name
        shutil.copy2(spark_binary, target)
        self.check("spark-plugin-deployed", "PASS", str(target.relative_to(self.root)))
        if self.platform == "windows":
            allocation_shim = locate_one(spark_root, ["spark_allocation_shim.dll"])
            shim_target = plugin_dir / allocation_shim.name
            shutil.copy2(allocation_shim, shim_target)
            self.check("spark-allocation-shim-deployed", "PASS", str(shim_target.relative_to(self.root)))

    def _prepare_bstats_before_start(self) -> None:
        if not getattr(self, "disable_bstats", False):
            return
        evidence = write_disabled_bstats_config(self.server_dir)
        source = self.server_dir / pathlib.PurePosixPath(B_STATS_CONFIG_RELATIVE_PATH)
        copied = copy_bstats_evidence(self.root, source)
        if copied != evidence:
            raise BStatsConfigError("copied bStats evidence does not match the server config")
        self.bstats_config = evidence
        self.result["bstats_config"] = evidence
        self.check(
            "bstats-disabled",
            "PASS",
            "canonical bStats config installed before BDS startup",
            relative_path=evidence["relative_path"],
            evidence_path=evidence["evidence_path"],
            bytes=evidence["bytes"],
            sha256=evidence["sha256"],
            canonical_enabled=evidence["canonical_enabled"],
        )

    def start_server(self) -> None:
        cmd = [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(self.server_dir)]
        self.server = ServerProcess(cmd, self.root, self.log_path)
        self._prepare_bstats_before_start()
        self.server.start()
        self.server.wait_for(lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines), 240, "BDS ready")
        self.check("bds-start", "PASS")
        self.check("ready", "PASS")
        self.server.wait_for(lambda lines: any("spark" in line.lower() and any(hint in line.lower() for hint in SPARK_LOAD_HINTS) for line in lines), 30, "Spark enable")
        self.check("spark-load-enable", "PASS")
        version_file = self.server_dir / "version.txt"
        if version_file.exists():
            self.result["bds_version"] = version_file.read_text(encoding="utf-8").strip()
            write_json(self.result_path, self.result)

    def command_check(self, name: str, command: str, timeout: float = 8.0) -> list[str]:
        assert self.server is not None
        start = self.server.command(command)
        output = self.server.wait_command_output(start, timeout)
        joined = "\n".join(output).lower()
        if not self.server.is_alive():
            raise RuntimeError(f"Server exited while running console command: {command}")
        if "unknown command" in joined or "command not found" in joined:
            raise RuntimeError(f"Spark command rejected: {command}\n" + "\n".join(output[-20:]))
        self.check(name, "PASS", command)
        return output

    def run_basic_commands(self) -> None:
        self.command_check("spark-profiler-info", "spark profiler info")
        self.command_check("spark-tps", "spark tps")
        self.command_check("spark-health", "spark health")
        self.command_check("spark-activity", "spark activity")

    @staticmethod
    def _viewer_url(lines: list[str], start_index: int) -> str | None:
        for line in lines[start_index:]:
            match = VIEWER_RE.search(line)
            if match:
                return match.group(0).rstrip(").,]")
        return None

    def run_profiler(self, allocation: bool) -> str | None:
        assert self.server is not None
        mode = "allocation" if allocation else "execution"
        command = "spark profiler start --timeout 12" + (" --alloc" if allocation else "")
        start = self.server.command(command)
        deadline = time.monotonic() + 55
        url: str | None = None
        while time.monotonic() < deadline:
            url = self._viewer_url(self.server.snapshot(), start)
            if url:
                break
            if not self.server.is_alive():
                raise RuntimeError(f"Server exited during {mode} profiler")
            time.sleep(1)
        if url is None:
            stop_at = self.server.command("spark profiler stop")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                url = self._viewer_url(self.server.snapshot(), min(start, stop_at))
                if url:
                    break
                if not self.server.is_alive():
                    raise RuntimeError(f"Server exited while finalizing {mode} profiler")
                time.sleep(1)
        if url is None:
            raise RuntimeError(f"{mode.capitalize()} profiler produced no spark viewer URL")
        field = "allocation_profile_viewer_url" if allocation else "execution_profile_viewer_url"
        self.result[field] = url
        write_json(self.result_path, self.result)
        self.check(f"{mode}-profiler", "PASS", viewer_url=url)
        return url

    def run_recovery_probe(self) -> None:
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            self.record_server_lifecycle()
            raise RuntimeError("Server failed to stop gracefully before recovery probe")
        self.record_server_lifecycle()
        self.server.close()
        self.result["shutdown_status"] = "graceful"
        write_json(self.result_path, self.result)
        self.server = None
        self.start_server()
        self.check("recovery", "PASS", "clean restart succeeded; abrupt crash-replay path not forced in headless CI", crash_replay="not_forced")

    def residual_processes(self) -> list[str]:
        if self.platform == "linux":
            result = subprocess.run(["pgrep", "-af", "bedrock_server"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
            return [line for line in result.stdout.splitlines() if str(self.server_dir) in line]
        return self.server.managed_residual_processes() if self.server is not None else []

    def shutdown(self) -> None:
        if self.server is None:
            return
        graceful = self.server.graceful_stop(60)
        self.record_server_lifecycle()
        if not graceful:
            self.server.force_kill_tree()
            self.record_server_lifecycle()
            self.result["shutdown_status"] = "forced"
            write_json(self.result_path, self.result)
            raise RuntimeError("BDS did not shut down gracefully within timeout")
        self.server.close()
        leftovers = self.residual_processes()
        if leftovers:
            self.record_server_lifecycle()
            raise RuntimeError("Residual BDS process detected after shutdown: " + " | ".join(leftovers[:5]))
        self.result["shutdown_status"] = "graceful"
        self.record_server_lifecycle()
        self.check("shutdown", "PASS", "graceful; no residual BDS process")

    def split_logs(self) -> None:
        if not self.log_path.exists():
            self.endstone_log.write_text("", encoding="utf-8")
            self.spark_log.write_text("", encoding="utf-8")
            return
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        endstone = [line for line in lines if "endstone" in clean_line(line).lower()]
        spark = [line for line in lines if "spark" in clean_line(line).lower()]
        self.endstone_log.write_text("\n".join(endstone) + ("\n" if endstone else ""), encoding="utf-8")
        self.spark_log.write_text("\n".join(spark) + ("\n" if spark else ""), encoding="utf-8")

    def execute(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            stage = "bds-start"
            self.start_server()
            stage = "spark-basic-commands"
            self.run_basic_commands()
            stage = "execution-profiler"
            self.run_profiler(allocation=False)
            stage = "allocation-profiler"
            self.run_profiler(allocation=True)
            stage = "recovery"
            self.run_recovery_probe()
            stage = "shutdown"
            self.shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            return 0
        except Exception as exc:  # noqa: BLE001 - integration failures become result evidence
            self.result["status"] = "FAIL"
            self.result["state"] = "completed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1200]
            diagnostic = traceback.format_exc()
            try:
                if self.server is not None and (self.server.is_alive() or getattr(self.server, "was_forced", False)):
                    self.server.force_kill_tree()
                    self.record_server_lifecycle()
                    self.result["shutdown_status"] = "forced_after_failure"
                    self.server.close()
            except Exception:  # noqa: BLE001 - cleanup failure is appended to diagnostics
                diagnostic += "\n\nCleanup failure:\n" + traceback.format_exc()
            last_lines = self.server.snapshot()[-200:] if self.server is not None else []
            self.diagnostics.write_text(diagnostic + "\n\nLast log lines:\n" + "\n".join(last_lines), encoding="utf-8")
            return 1
        finally:
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)
            print(json.dumps(self.result, indent=2), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    args = parser.parse_args()
    return IntegrationTest(args.platform).execute()


if __name__ == "__main__":
    raise SystemExit(main())
