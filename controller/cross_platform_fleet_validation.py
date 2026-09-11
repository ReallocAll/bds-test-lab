from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from controller.final_profiler_matrix import (
    ALLOCATION_DIAGNOSTICS,
    EXECUTION_DIAGNOSTICS,
    _require_bool,
    _require_nonnegative_int,
)
from controller.fleet_spark_validation import (
    PLAYER_COUNT_RE,
    FleetBotProcess,
    FleetSparkValidation,
)
from controller.python_profile_payload import (
    fetch_viewer_payload,
    parse_sampler_data,
    profile_summary,
)
from controller.run_test import IntegrationTest, child_process_env, now_iso, write_json


def validate_provenance_env() -> None:
    for key in ("EXPECTED_SPARK_SHA", "EXPECTED_ENDSTONE_SHA", "BOT_REF"):
        value = os.environ.get(key, "")
        if value and not re.fullmatch(r"[0-9a-fA-F]{40}", value):
            raise ValueError(f"{key} must be a 40-character hexadecimal SHA")
    for key in ("EXPECTED_SPARK_RUN_ID", "EXPECTED_SPARK_ARTIFACT_ID"):
        value = os.environ.get(key, "")
        if value and not re.fullmatch(r"[0-9]+", value):
            raise ValueError(f"{key} must contain only digits")


def profile_quality(
    profile, mode: str, seconds: int, player_window_valid: bool
) -> dict:
    summary = profile_summary(profile)
    required = ALLOCATION_DIAGNOSTICS if mode == "allocation" else EXECUTION_DIAGNOSTICS
    raw = {
        key: value
        for key, value in profile.extra_metadata.items()
        if key.startswith(("Execution ", "Allocation "))
        or any(
            term in key.lower()
            for term in ("drop", "incomplete", "overflow", "exhausted", "truncated")
        )
    }
    missing = [key for key in required if key not in raw]
    failures = []
    drops, flags = {}, {}
    for key, value in raw.items():
        try:
            if any(
                term in key.lower() for term in ("incomplete", "exhausted", "truncated")
            ):
                flags[key] = _require_bool(value, key)
            elif any(term in key.lower() for term in ("drop", "overflow")):
                drops[key] = _require_nonnegative_int(value, key)
        except ValueError as exc:
            failures.append(str(exc))
    if profile.sampler_mode != (1 if mode == "allocation" else 0):
        failures.append("payload sampler mode does not match requested mode")
    weight = summary["root_weight"]
    if not profile.threads or not math.isfinite(weight) or weight <= 0:
        failures.append("empty threads or nonpositive/nonfinite root weight")
    if any(
        not math.isfinite(value) or value < 0
        for thread in profile.threads
        for value in [*thread.times, *(v for node in thread.nodes for v in node.times)]
    ):
        failures.append("invalid sample weight")
    if profile.duration_seconds < seconds * 0.8:
        failures.append("profile duration below 80% of requested duration")
    if not player_window_valid:
        failures.append("current player measurement window is invalid")
    status = (
        "FAIL"
        if failures
        else "UNVERIFIED"
        if missing
        else "DEGRADED"
        if any(drops.values()) or any(flags.values())
        else "PASS"
    )
    return {
        "status": status,
        "failures": failures,
        "missing_diagnostics": missing,
        "observed": summary,
        "diagnostics": {"raw": raw, "drops": drops, "incomplete_flags": flags},
    }


class CrossPlatformFleetBotProcess(FleetBotProcess):
    def start(self) -> None:
        cmd = [
            str(self.binary),
            "--host",
            "127.0.0.1",
            "--port",
            "19132",
            "--count",
            str(self.count),
            "--name-prefix",
            self.name_prefix,
        ]
        scenario_file = os.environ.get("BDS_TEST_BOT_SCENARIO_FILE", "").strip()
        if scenario_file:
            scenario_path = pathlib.Path(scenario_file).resolve()
            if not scenario_path.is_file():
                raise FileNotFoundError(
                    f"Configured bot scenario file does not exist: {scenario_path}"
                )
            cmd.extend(["--scenario-file", str(scenario_path)])
        else:
            cmd.extend(["--scenario", self.scenario])
        cmd.extend(
            [
                "--login-stagger",
                "250ms",
                "--chunk-radius",
                "8",
                "--connect-timeout",
                "20s",
                "--spawn-timeout",
                "45s",
                "--json",
            ]
        )
        print("+", " ".join(cmd), flush=True)
        self._log = self.log_path.open("w", encoding="utf-8")
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        )
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            env=child_process_env(),
        )
        self.capture_process_identity()
        self._reader = threading.Thread(
            target=self._read_loop, name="fleet-bot-log-reader", daemon=True
        )
        self._reader.start()

    def _complete_linux_sigterm_shutdown(self, code: int) -> bool:
        """Recognize a semantically graceful fleet shutdown despite waitpid(SIGTERM).

        The exact Go bot catches SIGTERM, drains every instance, emits one
        ``bot_stats`` event per launched bot, then emits ``fleet_shutdown``.
        On some hosted Linux runners ``waitpid`` has nevertheless reported the
        wrapper process as signal-terminated. Do not accept that exit code by
        itself: require the complete application-level shutdown contract first.
        """

        if sys.platform != "linux" or code != -int(signal.SIGTERM):
            return False
        events = self.event_snapshot()
        shutdown = next(
            (
                event
                for event in reversed(events)
                if event.get("event") == "fleet_shutdown"
            ),
            None,
        )
        if not isinstance(shutdown, dict):
            return False
        if (
            shutdown.get("graceful_shutdown") is not True
            or shutdown.get("reason") != "signal"
        ):
            return False
        if "error" in shutdown:
            return False
        try:
            launched = int(shutdown.get("launched", -1))
            online = int(shutdown.get("online", -1))
        except (TypeError, ValueError):
            return False
        if launched != self.count or online != self.count:
            return False

        stats = [event for event in events if event.get("event") == "bot_stats"]
        if len(stats) != self.count:
            return False
        indexes: set[int] = set()
        for event in stats:
            if event.get("online") is not True or "error" in event:
                return False
            try:
                index = int(event.get("index", -1))
            except (TypeError, ValueError):
                return False
            if index < 1 or index > self.count or index in indexes:
                return False
            indexes.add(index)
        return indexes == set(range(1, self.count + 1))

    def terminate(self, timeout: float = 15.0) -> int:
        code = super().terminate(timeout)
        if code == 0:
            return 0
        if self._complete_linux_sigterm_shutdown(code):
            print(
                "[bot] normalized Linux -SIGTERM exit after complete graceful fleet shutdown evidence",
                flush=True,
            )
            return 0
        return code

    def graceful_stop(self, timeout: float = 15.0) -> dict:
        outcome = super().graceful_stop(timeout)
        if self._complete_linux_sigterm_shutdown(outcome.get("returncode")):
            outcome.update(outcome="graceful", success=True, returncode=0)
        return outcome


class CrossPlatformFleetSparkValidation(FleetSparkValidation):
    disable_bstats = True

    def __init__(
        self,
        platform_name: str,
        bot_binary: pathlib.Path,
        count: int,
        scenario: str,
        profile_seconds: int,
        profiler_mode: str = "execution",
    ):
        if profiler_mode not in ("execution", "allocation"):
            raise ValueError("profiler_mode must be execution or allocation")
        validate_provenance_env()
        IntegrationTest.__init__(self, platform_name)
        self.bot_binary = bot_binary.resolve()
        self.count = count
        self.scenario = scenario
        self.profile_seconds = max(30, profile_seconds)
        self.profiler_mode = profiler_mode
        self.case_identity = f"{platform_name}-{scenario}-{count}-{profiler_mode}"
        self.generation = uuid.uuid4().hex
        self.bot_log = self.root / f"fleet-{self.case_identity}.log"
        self.fleet_result = self.root / "fleet-spark-result.json"
        self.bot: FleetBotProcess | None = None
        self.result.update(
            {
                "test_kind": "spark-cross-platform-real-player-load",
                "platform": platform_name,
                "bot_count": count,
                "scenario": scenario,
                "profile_seconds": self.profile_seconds,
                "profiler_mode": profiler_mode,
                "case_identity": self.case_identity,
                "generation": self.generation,
                "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
                "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "player_snapshots": [],
                "spark_profile_viewer_url": None,
                "metrics": None,
                "fleet_online_event": None,
                "fleet_shutdown_event": None,
                "bot_stats": [],
            }
        )
        self._write_results()

    def player_snapshot(self, phase: str, started: float | None = None) -> dict:
        assert self.server is not None
        sent_at = now_iso()
        sent_offset = time.monotonic() - started if started is not None else None
        identity_before = self.server.bds_identity_snapshot(self.server_dir)
        index = self.server.command("list")
        output = self.server.wait_command_output(index, 8)
        identity_after = self.server.bds_identity_snapshot(self.server_dir)
        headers = [(i, PLAYER_COUNT_RE.search(line)) for i, line in enumerate(output)]
        headers = [(i, match) for i, match in headers if match]
        names: list[str] = []
        count = None
        if len(headers) == 1:
            i, match = headers[0]
            count = int(match.group(1))
            tail = output[i][match.end() :].lstrip(" :")
            if not tail and i + 1 < len(output):
                tail = re.sub(r"^(?:\[[^\]]*\]\s*)+", "", output[i + 1]).strip()
            names = [name.strip() for name in tail.split(",") if name.strip()]
        snapshot = {
            "phase": phase,
            "generation": self.generation,
            "workflow_run_id": self.result.get("workflow_run_id"),
            "server_pid": getattr(getattr(self.server, "process", None), "pid", None),
            "process_identity_before": identity_before,
            "process_identity_after": identity_after,
            "sent_at": sent_at,
            "completed_at": now_iso(),
            "command_start_index": index,
            "sent_offset_seconds": sent_offset,
            "completed_offset_seconds": time.monotonic() - started
            if started is not None
            else None,
            "count": count,
            "names": names,
            "output": output,
            "valid": count == self.count
            and sorted(name.lower() for name in names)
            == sorted(name.lower() for name in self.expected_names()),
        }
        self.result["player_snapshots"].append(snapshot)
        self._write_results()
        return snapshot

    def player_window_valid(self) -> bool:
        snapshots = self.result["player_snapshots"]
        identities = [s.get(key) for s in snapshots for key in ("process_identity_before", "process_identity_after")]
        if not identities or any(
            not isinstance(identity, dict) or identity.get("status") != "VERIFIED"
            or not isinstance(identity.get("launch"), dict) or not isinstance(identity.get("bds"), dict)
            or identity["launch"].get("pid") is None or identity["launch"].get("create_time") is None
            or identity["bds"].get("pid") is None or identity["bds"].get("create_time") is None
            or identity["bds"].get("owned") is not True or not identity["bds"].get("binary_path")
            or identity["bds"].get("source") not in ("executable", "module")
            or not identity["bds"].get("binary_identity")
            for identity in identities
        ):
            return False
        if any(s.get("server_pid") != identities[0]["launch"]["pid"] for s in snapshots):
            return False
        if any((identity["launch"], identity["bds"]) != (identities[0]["launch"], identities[0]["bds"])
               for identity in identities):
            return False
        during = [
            s
            for s in snapshots
            if s["phase"] == "during"
            and 0
            <= s["sent_offset_seconds"]
            < s["completed_offset_seconds"]
            <= self.profile_seconds
        ]
        return (
            len(during) >= 2
            and all(
                s["valid"] and s["generation"] == self.generation for s in snapshots
            )
            and any(s["phase"] == "before" for s in snapshots)
            and any(s["phase"] == "after" for s in snapshots)
            and len({s["command_start_index"] for s in during}) == len(during)
        )

    def collect_payload(self, url: str) -> None:
        self.result["spark_profile_viewer_url"] = url
        self.result["quality"] = {
            "status": "FAIL",
            "failures": ["payload collection not completed"],
        }
        self._write_results()
        try:
            raw = fetch_viewer_payload(url)
            path = self.root / f"{self.case_identity}-raw.sparkprofile"
            with path.open("xb") as stream:
                stream.write(raw)
            digest = hashlib.sha256(raw).hexdigest()
            self.result["profile"] = {
                "raw_path": path.name,
                "raw_sha256": digest,
                "raw_bytes": len(raw),
                "viewer_url": url,
                "generation": self.generation,
            }
            if path.read_bytes() != raw:
                raise ValueError("persisted profile bytes changed")
            profile = parse_sampler_data(raw)
            self.result["quality"] = profile_quality(
                profile,
                self.profiler_mode,
                self.profile_seconds,
                self.player_window_valid(),
            )
        except Exception as exc:  # noqa: BLE001 - persist evidence before graceful cleanup
            self.result["quality"] = {
                "status": "FAIL",
                "failures": [f"{type(exc).__name__}: {exc}"],
            }
        self._write_results()

    def profile_execution(self) -> tuple[str, list[int]]:
        assert self.server is not None
        self.player_snapshot("before")
        samples: list[int] = []
        command = f"spark profiler start --timeout {self.profile_seconds}"
        if self.profiler_mode == "allocation":
            command += " --alloc"
        self.result["profile_command"] = command
        started = time.monotonic()
        self.result["profile_started_at"] = now_iso()
        start = self.server.command(command)
        deadline = started + self.profile_seconds + 75
        next_probe = started + 1
        probes = 0
        url = None
        while time.monotonic() < deadline:
            url = self._viewer_url(self.server.snapshot(), start)
            if url:
                break
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while collecting fleet Spark profile")
            samples.append(self.bds_rss_bytes())
            if probes < 2 and time.monotonic() >= next_probe:
                self.player_snapshot("during", started)
                probes += 1
                next_probe = started + self.profile_seconds / 2
            time.sleep(1)
        if url is None:
            self.server.command("spark profiler stop")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                url = self._viewer_url(self.server.snapshot(), start)
                if url:
                    break
                if not self.server.is_alive():
                    raise RuntimeError(
                        "BDS exited while finalizing fleet Spark profile"
                    )
                time.sleep(1)
        if url is None:
            raise RuntimeError("Fleet Spark profiler produced no viewer URL")
        self.result["profile_url_observed_at"] = now_iso()
        self.player_snapshot("after")
        self.collect_payload(url)
        return url, samples

    def execute(self) -> int:
        code = super().execute()
        if code == 0 and self.result.get("quality", {}).get("status") not in (
            "PASS",
            "DEGRADED",
        ):
            self.result.update(status="FAIL", failed_stage="profile-quality")
            self._write_results()
            return 1
        return code

    def _write_results(self) -> None:
        write_json(self.result_path, self.result)
        write_json(self.fleet_result, self.result)

    def start_fleet(self) -> None:
        assert self.server is not None
        self.bot = CrossPlatformFleetBotProcess(
            self.bot_binary, self.bot_log, self.count, self.scenario
        )
        self.bot.start()
        online = self.bot.wait_event("fleet_online", max(90.0, self.count * 5.0))
        if (
            int(online.get("online", -1)) != self.count
            or int(online.get("count", -1)) != self.count
        ):
            raise RuntimeError(f"Invalid fleet_online event: {online}")
        self.result["fleet_online_event"] = online
        output, convergence = self.wait_player_count(self.count)
        joined = "\n".join(output).lower()
        missing = [name for name in self.expected_names() if name.lower() not in joined]
        if missing:
            raise RuntimeError(
                f"BDS list reached {self.count} players but names are missing: {missing}"
            )
        self.check(
            "fleet-all-online",
            "PASS",
            f"{self.count} independent players visible in BDS",
            convergence_seconds=round(convergence, 3),
            fleet_online_event=online,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--count", required=True, type=int, choices=[1, 5])
    parser.add_argument("--scenario", required=True, choices=["idle", "chunk-walk"])
    parser.add_argument("--profile-seconds", type=int, default=30)
    parser.add_argument(
        "--profiler-mode", choices=["execution", "allocation"], default="execution"
    )
    args = parser.parse_args()
    validator = CrossPlatformFleetSparkValidation(
        args.platform,
        pathlib.Path(args.bot),
        args.count,
        args.scenario,
        args.profile_seconds,
        args.profiler_mode,
    )
    code = validator.execute()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
