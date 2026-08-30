#!/usr/bin/env python3
from __future__ import annotations

import argparse
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

from providers.artifact_provider import resolve_artifacts

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
VIEWER_RE = re.compile(r"https://spark\.lucko\.me/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
READY_HINTS = ("server started.", "server started in", "server started")
SPARK_LOAD_HINTS = ("enabling spark", "enabled spark", "spark v", "loaded spark")


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_line(line: str) -> str:
    return ANSI_RE.sub("", line).strip()


def write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


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
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._log = None

    def start(self) -> None:
        kwargs: dict[str, Any] = {}
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
        if not self.is_alive():
            return True
        try:
            self.command("stop")
        except Exception:  # noqa: BLE001 - shutdown must report failure without hiding it
            return False
        assert self.process is not None
        try:
            self.process.wait(timeout=timeout)
            return self.process.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def force_kill_tree(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        pid = self.process.pid
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30, check=False)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()

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
        }
        write_json(self.result_path, self.result)

    def check(self, name: str, status: str, detail: str | None = None, **extra: Any) -> None:
        item: dict[str, Any] = {"name": name, "status": status}
        if detail:
            item["detail"] = detail
        item.update(extra)
        self.result["checks"].append(item)
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
        target = plugin_dir / spark_binary.name
        shutil.copy2(spark_binary, target)
        self.check("spark-plugin-deployed", "PASS", str(target.relative_to(self.root)))
        if self.platform == "windows":
            allocation_shim = locate_one(spark_root, ["spark_allocation_shim.dll"])
            shim_target = plugin_dir / allocation_shim.name
            shutil.copy2(allocation_shim, shim_target)
            self.check("spark-allocation-shim-deployed", "PASS", str(shim_target.relative_to(self.root)))

    def start_server(self) -> None:
        cmd = [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(self.server_dir)]
        self.server = ServerProcess(cmd, self.root, self.log_path)
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
            raise RuntimeError("Server failed to stop gracefully before recovery probe")
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
        result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq bedrock_server.exe", "/FO", "CSV", "/NH"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", check=False)
        return [line for line in result.stdout.splitlines() if "bedrock_server.exe" in line.lower()]

    def shutdown(self) -> None:
        if self.server is None:
            return
        graceful = self.server.graceful_stop(60)
        if not graceful:
            self.server.force_kill_tree()
            self.result["shutdown_status"] = "forced"
            write_json(self.result_path, self.result)
            raise RuntimeError("BDS did not shut down gracefully within timeout")
        self.server.close()
        self.result["shutdown_status"] = "graceful"
        write_json(self.result_path, self.result)
        leftovers = self.residual_processes()
        if leftovers:
            raise RuntimeError("Residual BDS process detected after shutdown: " + " | ".join(leftovers[:5]))
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
                if self.server is not None and self.server.is_alive():
                    self.server.force_kill_tree()
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
