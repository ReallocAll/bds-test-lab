#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import pathlib
import time
import urllib.request
from typing import Any

import psutil

from controller.run_test import IntegrationTest, now_iso, write_json

BYTEBIN_URL = "https://spark-usercontent.lucko.me/post"
VIEWER_URL = "https://spark.lucko.me/"
SAMPLER_CONTENT_TYPE = "application/x-spark-sampler"


class ExtendedIntegrationTest(IntegrationTest):
    def __init__(self, platform_name: str, soak_minutes: int):
        super().__init__(platform_name)
        self.soak_minutes = max(30, soak_minutes)
        self.result["profiles"] = []
        self.result["recovery"] = {
            "crash_replay": "not_started",
            "journal_segments_before_restart": 0,
            "recovered_profile_path": None,
            "recovered_profile_sha256": None,
            "recovered_profile_viewer_url": None,
        }
        self.result["soak"] = {
            "duration_minutes": self.soak_minutes,
            "samples": [],
            "rss_start_bytes": None,
            "rss_end_bytes": None,
            "rss_peak_bytes": None,
            "rss_growth_bytes": None,
            "threads_start": None,
            "threads_end": None,
            "threads_peak": None,
            "thread_delta": None,
        }
        write_json(self.result_path, self.result)

    @staticmethod
    def _profile_key(url: str | None) -> str | None:
        if not url:
            return None
        return url.rstrip("/").rsplit("/", 1)[-1]

    def record_online_profile(self, kind: str, url: str | None, duration_seconds: int, notes: str) -> None:
        if not url:
            return
        self.result["profiles"].append(
            {
                "kind": kind,
                "url": url,
                "profile_key": self._profile_key(url),
                "duration_seconds": duration_seconds,
                "server_version": self.result.get("bds_version"),
                "notes": notes,
            }
        )
        write_json(self.result_path, self.result)

    def _recovery_segments(self) -> list[pathlib.Path]:
        return sorted(self.server_dir.rglob("segment-*.jnl"))

    def _saved_profiles(self) -> set[pathlib.Path]:
        return {p.resolve() for p in self.server_dir.rglob("*.sparkprofile") if p.is_file()}

    @staticmethod
    def _upload_profile(path: pathlib.Path) -> tuple[str, str]:
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        compressed = gzip.compress(body)
        request = urllib.request.Request(
            BYTEBIN_URL,
            data=compressed,
            method="POST",
            headers={
                "Content-Type": SAMPLER_CONTENT_TYPE,
                "Content-Encoding": "gzip",
                "User-Agent": "bds-test-lab/recovery-validation",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            location = response.headers.get("Location", "").rstrip("/")
            if not location:
                raise RuntimeError("bytebin returned no Location header for recovered profile")
            key = location.rsplit("/", 1)[-1]
            if not key:
                raise RuntimeError("bytebin returned an empty recovered profile key")
        return digest, VIEWER_URL + key

    def run_crash_recovery_probe(self) -> None:
        assert self.server is not None
        before_profiles = self._saved_profiles()
        start = self.server.command("spark profiler start")
        output = self.server.wait_command_output(start, 8)
        joined = "\n".join(output).lower()
        if "couldn't start" in joined or "isn't available" in joined:
            raise RuntimeError("Unable to start profiler for crash recovery probe: " + " | ".join(output[-20:]))

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if not self.server.is_alive():
                raise RuntimeError("BDS exited before controlled crash recovery probe")
            time.sleep(0.5)

        segments = self._recovery_segments()
        if not segments:
            raise RuntimeError("No RecoveryWriter journal segment existed before controlled BDS crash")
        self.result["recovery"]["journal_segments_before_restart"] = len(segments)
        write_json(self.result_path, self.result)

        self.server.force_kill_tree()
        self.server.close()
        self.server = None
        self.result["shutdown_status"] = "controlled_crash_for_recovery"
        self.result["recovery"]["crash_replay"] = "crashed_waiting_restart"
        write_json(self.result_path, self.result)

        self.start_server()
        assert self.server is not None
        self.server.wait_for(
            lambda lines: any("recovered profile saved to" in line.lower() for line in lines),
            90,
            "Spark crash recovery replay",
        )

        deadline = time.monotonic() + 20
        recovered: list[pathlib.Path] = []
        while time.monotonic() < deadline:
            recovered = [p for p in self._saved_profiles() if p not in before_profiles]
            if recovered:
                break
            time.sleep(0.5)
        if not recovered:
            raise RuntimeError("Spark reported crash recovery but no recovered .sparkprofile was found")
        recovered.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        recovered_path = recovered[0]
        digest, viewer_url = self._upload_profile(recovered_path)
        self.result["recovery"].update(
            {
                "crash_replay": "PASS",
                "recovered_profile_path": str(recovered_path.relative_to(self.root)),
                "recovered_profile_sha256": digest,
                "recovered_profile_viewer_url": viewer_url,
            }
        )
        self.result["profiles"].append(
            {
                "kind": "crash-recovery",
                "url": viewer_url,
                "profile_key": self._profile_key(viewer_url),
                "sha256": digest,
                "duration_seconds": 12,
                "server_version": self.result.get("bds_version"),
                "notes": "Recovered from RecoveryWriter journal after controlled hard kill and uploaded by the lab.",
            }
        )
        self.check(
            "crash-recovery",
            "PASS",
            "controlled hard kill produced a replayable journal and recovered profile",
            viewer_url=viewer_url,
            sha256=digest,
        )

    def _process_metrics(self) -> dict[str, int]:
        assert self.server is not None and self.server.process is not None
        root = psutil.Process(self.server.process.pid)
        processes: list[psutil.Process] = [root]
        try:
            processes.extend(root.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        bedrock: list[psutil.Process] = []
        for process in processes:
            try:
                name = process.name().lower()
                cmdline = " ".join(process.cmdline()).lower()
                if "bedrock_server" in name or "bedrock_server" in cmdline:
                    bedrock.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        selected = bedrock or processes
        rss = 0
        threads = 0
        for process in selected:
            try:
                rss += process.memory_info().rss
                threads += process.num_threads()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"rss_bytes": rss, "threads": threads}

    def run_soak(self) -> None:
        assert self.server is not None
        duration = self.soak_minutes * 60
        started = time.monotonic()
        next_sample = started
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= duration:
                break
            if not self.server.is_alive():
                raise RuntimeError(f"BDS exited during {self.soak_minutes}-minute soak")
            if time.monotonic() >= next_sample:
                metrics = self._process_metrics()
                metrics["elapsed_seconds"] = int(elapsed)
                self.result["soak"]["samples"].append(metrics)
                write_json(self.result_path, self.result)
                next_sample += 60
            time.sleep(min(5.0, max(0.1, duration - elapsed)))

        metrics = self._process_metrics()
        metrics["elapsed_seconds"] = duration
        self.result["soak"]["samples"].append(metrics)
        samples: list[dict[str, Any]] = self.result["soak"]["samples"]
        rss_values = [int(sample["rss_bytes"]) for sample in samples]
        thread_values = [int(sample["threads"]) for sample in samples]
        self.result["soak"].update(
            {
                "rss_start_bytes": rss_values[0],
                "rss_end_bytes": rss_values[-1],
                "rss_peak_bytes": max(rss_values),
                "rss_growth_bytes": rss_values[-1] - rss_values[0],
                "threads_start": thread_values[0],
                "threads_end": thread_values[-1],
                "threads_peak": max(thread_values),
                "thread_delta": thread_values[-1] - thread_values[0],
            }
        )
        self.command_check("post-soak-command", "spark profiler info")
        self.check(
            "soak-stability",
            "PASS",
            f"BDS remained responsive for {self.soak_minutes} minutes",
            rss_growth_bytes=self.result["soak"]["rss_growth_bytes"],
            rss_peak_bytes=self.result["soak"]["rss_peak_bytes"],
            thread_delta=self.result["soak"]["thread_delta"],
            threads_peak=self.result["soak"]["threads_peak"],
        )

    def execute_extended(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            stage = "bds-start"
            self.start_server()
            stage = "spark-basic-commands"
            self.run_basic_commands()
            stage = "execution-profiler"
            execution_url = self.run_profiler(allocation=False)
            self.record_online_profile("execution", execution_url, 12, "Headless execution profiler")
            stage = "allocation-profiler"
            allocation_url = self.run_profiler(allocation=True)
            if allocation_url:
                self.record_online_profile("allocation", allocation_url, 12, "Headless native allocation profiler")
            stage = "crash-recovery"
            self.run_crash_recovery_probe()
            stage = "soak"
            self.run_soak()
            stage = "shutdown"
            self.shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            return 0
        except Exception as exc:
            import traceback

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
            except Exception:
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
    parser.add_argument("--soak-minutes", type=int, default=int(os.environ.get("BDS_SOAK_MINUTES", "30")))
    args = parser.parse_args()
    return ExtendedIntegrationTest(args.platform, args.soak_minutes).execute_extended()


if __name__ == "__main__":
    raise SystemExit(main())
