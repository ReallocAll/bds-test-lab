from __future__ import annotations

import copy
import json
import math
import os
import pathlib
import re
import time
import traceback
from typing import Any

# Importing the combined Windows runner installs the exact artifact,
# lifecycle, bootstrap, and shutdown adapters before the candidate flow runs.
from controller import combined_windows_final_runner  # noqa: F401
from controller.bot_validation import list_players
from controller.combined_pack_gamerule_fleet_validation import (
    BOT_COUNT,
    BOT_SCENARIO,
    CombinedPackGameruleFleetValidation,
)
from controller.python_evidence_provenance import (
    validate_component_provenance,
    validate_endstone_runtime_version,
)
from controller.run_test import ServerProcess, now_iso

SPARK_CANDIDATE_SHA = "25edd777495eeebb3532d989e6e1ab3fb093936e"
RELOAD_CYCLES = 3
ALLOCATION_INTERVAL_BYTES = 4096
CPU_BASELINE_KIND = "cpu-baseline"
CPU_LOAD_KIND = "20-player-load"
ALLOCATION_KIND = "allocation-4096"
POST_RELOAD_KIND = "post-reload-cycle-3"
PROFILE_KINDS = (CPU_BASELINE_KIND, CPU_LOAD_KIND, ALLOCATION_KIND, POST_RELOAD_KIND)
CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT = 75.0
PENDING_COMMAND_DRAIN_TIMEOUT = 5.0
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RELOAD_DISABLE_RE = re.compile(r"\[(?:endstone|spark)\]\s+disabling\s+spark(?:\s|$)", re.IGNORECASE)
_RELOAD_ENABLE_RE = re.compile(r"\[(?:endstone|spark)\]\s+enabling\s+spark(?:\s|$)", re.IGNORECASE)
_RELOAD_COMPLETE_RE = re.compile(r"(?:^|\]\s*:?\s*)reload\s+complete\.\s*$", re.IGNORECASE)
_RELOAD_FAILURE_RE = re.compile(
    r"(?:\b(?:failed|failure|error|unable|cannot|could\s+not|not|exception|rejected)\b|"
    r"\bdispatch\s+result\s*:\s*false\b)",
    re.IGNORECASE,
)


def _positive_int(value: object, name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got {value!r}")
    return parsed


def _required_lab_run_id() -> int:
    raw = os.environ.get("LAB_RUN_ID", "").strip() or os.environ.get("GITHUB_RUN_ID", "").strip()
    return _positive_int(raw, "LAB_RUN_ID/GITHUB_RUN_ID")


def _expected_spark_sha() -> str:
    observed = os.environ.get("EXPECTED_SPARK_SHA", "").strip().lower()
    if observed != SPARK_CANDIDATE_SHA:
        raise RuntimeError(
            f"Spark candidate SHA mismatch: observed={observed!r} expected={SPARK_CANDIDATE_SHA!r}"
        )
    return observed


def _artifact_record(component: str, observed: dict[str, Any]) -> dict[str, Any]:
    artifact = observed.get("artifact")
    if not isinstance(artifact, dict):
        raise RuntimeError(f"{component} provenance is missing artifact metadata")
    repository_value = observed.get("repository")
    repository = repository_value.strip() if isinstance(repository_value, str) else ""
    if not repository:
        raise RuntimeError(f"{component} provenance is missing repository identity")
    sha = str(observed.get("sha") or "").strip().lower()
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
        raise RuntimeError(f"{component} provenance has no exact source SHA: {sha!r}")
    run_id = _positive_int(observed.get("run_id"), f"{component} artifact run")
    artifact_id = _positive_int(artifact.get("id"), f"{component} artifact ID")
    artifact_name_value = artifact.get("name")
    artifact_name = artifact_name_value.strip() if isinstance(artifact_name_value, str) else ""
    if not artifact_name:
        raise RuntimeError(f"{component} artifact {artifact_id} has no artifact name")
    run_url_value = observed.get("run_url")
    run_url = run_url_value.strip() if isinstance(run_url_value, str) else ""
    if not run_url:
        raise RuntimeError(f"{component} artifact run {run_id} has no run URL")
    digest_value = artifact.get("digest")
    digest = digest_value.strip() if isinstance(digest_value, str) else ""
    if not _DIGEST_RE.fullmatch(digest):
        raise RuntimeError(f"{component} artifact {artifact_id} has invalid API digest: {digest!r}")
    return {
        "repository": repository,
        "sha": sha,
        "run_id": run_id,
        "run_url": run_url,
        "workflow": observed.get("workflow"),
        "artifact_id": artifact_id,
        "artifact_name": artifact_name,
        "artifact_digest": digest,
        "artifact_size_in_bytes": artifact.get("size_in_bytes"),
    }


def _select_live_bds_identity(records: list[dict[str, Any]]) -> tuple[int, float]:
    candidates = [
        record
        for record in records
        if record.get("alive") is True
        and record.get("identity_match") is True
        and "bedrock_server" in str(record.get("name") or "").casefold()
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"expected exactly one verified live bedrock_server process, observed={candidates!r}")
    candidate = candidates[0]
    pid = _positive_int(candidate.get("pid"), "bedrock_server PID")
    try:
        create_time = float(candidate.get("create_time"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid bedrock_server create time: {candidate!r}") from exc
    if not math.isfinite(create_time) or create_time <= 0:
        raise RuntimeError(f"invalid bedrock_server create time: {candidate!r}")
    return pid, create_time


def _ordered_reload_evidence(lines: list[str], cycle: int) -> tuple[str, str, str]:
    if any(_RELOAD_FAILURE_RE.search(line) for line in lines):
        raise RuntimeError(f"Spark reported an enable failure during /reload cycle {cycle}")

    disable = [index for index, line in enumerate(lines) if _RELOAD_DISABLE_RE.search(line)]
    enable = [index for index, line in enumerate(lines) if _RELOAD_ENABLE_RE.search(line)]
    complete = [index for index, line in enumerate(lines) if _RELOAD_COMPLETE_RE.search(line)]
    if not disable:
        raise RuntimeError(f"Spark disable evidence is missing during /reload cycle {cycle}")
    if not enable:
        raise RuntimeError(f"Spark enable evidence is missing during /reload cycle {cycle}")
    if not complete:
        raise RuntimeError(f"Endstone /reload cycle {cycle} did not report completion")
    disable_index = disable[0]
    enable_after = [index for index in enable if index > disable_index]
    if not enable_after:
        raise RuntimeError(f"Spark reload evidence is out of order during /reload cycle {cycle}")
    enable_index = enable_after[0]
    complete_after = [index for index in complete if index > enable_index]
    if not complete_after:
        raise RuntimeError(f"Spark reload evidence is out of order during /reload cycle {cycle}")
    complete_index = complete_after[0]
    return lines[disable_index], lines[enable_index], lines[complete_index]


def _validate_reload_output(lines: list[str], cycle: int) -> str:
    return _ordered_reload_evidence(lines, cycle)[2]


def _validate_command_ack(lines: list[str], command: str) -> str:
    for line in lines:
        lowered = line.casefold()
        if "ci command dispatch completed" in lowered and "dispatched=true" in lowered:
            return line
    raise RuntimeError(f"Endstone did not acknowledge CI dispatch for {command!r}")


class Spark51WindowsBdsValidation(CombinedPackGameruleFleetValidation):
    """Run the exact Spark #51 Windows profile and same-process reload matrix."""

    enable_ci_diagnostics = True

    def __init__(self, bot_binary: pathlib.Path, profile_seconds: int) -> None:
        super().__init__("windows", bot_binary, profile_seconds)
        self.enable_ci_diagnostics = True
        self.allow_missing_windows_allocation_shim = True
        self._profile_active = False
        self._fleet_stopped = False
        self._candidate_provenance: dict[str, Any] = {}
        self.result.update(
            {
                "test_kind": "spark51-windows-exact-candidate-bds-e2e",
                "spark_sha": SPARK_CANDIDATE_SHA,
                "lab_run_id": None,
                "bds_full_version": os.environ.get("EXPECTED_BDS_VERSION", "").strip() or None,
                "endstone_version": None,
                "endstone_sha": None,
                "artifact_provenance": {},
                "profiles": [],
                "plugin_reload_cycles": [],
                "shutdown_evidence": None,
                "cleanup_errors": [],
            }
        )
        self._write_results()

    def _record_candidate_provenance(self) -> None:
        expected_sha = _expected_spark_sha()
        lab_run_id = _required_lab_run_id()
        spark = _artifact_record("spark", validate_component_provenance(self.metadata, "spark"))
        endstone = _artifact_record("endstone", validate_component_provenance(self.metadata, "endstone"))
        if spark["sha"] != expected_sha:
            raise RuntimeError(f"Spark artifact SHA mismatch: {spark['sha']!r} != {expected_sha!r}")
        endstone_version = validate_endstone_runtime_version()
        if not endstone_version:
            raise RuntimeError("exact Endstone runtime version evidence is required")

        self._candidate_provenance = {
            "spark": spark,
            "endstone": endstone,
            "lab_run_id": lab_run_id,
        }
        self.result.update(
            {
                "spark_sha": expected_sha,
                "lab_run_id": lab_run_id,
                "endstone_version": endstone_version,
                "endstone_sha": endstone["sha"],
                "artifact_provenance": copy.deepcopy(self._candidate_provenance),
            }
        )
        self.check(
            "spark51-exact-provenance",
            "PASS",
            "exact Spark candidate and Endstone artifact provenance recorded",
            spark_sha=spark["sha"],
            spark_run_id=spark["run_id"],
            spark_artifact_id=spark["artifact_id"],
            spark_artifact_digest=spark["artifact_digest"],
            endstone_sha=endstone["sha"],
            endstone_run_id=endstone["run_id"],
            endstone_artifact_id=endstone["artifact_id"],
            endstone_artifact_digest=endstone["artifact_digest"],
            endstone_version=endstone_version,
            lab_run_id=lab_run_id,
        )

    def install_artifacts(self) -> None:
        self.allow_missing_windows_allocation_shim = True
        super().install_artifacts()
        self._record_candidate_provenance()

    def _common_profile_metadata(
        self,
        *,
        kind: str,
        viewer_url: str,
        player_count: int,
        reload_cycle: int,
        allocation_interval: int | None,
        command: str,
    ) -> dict[str, Any]:
        if not self._candidate_provenance:
            raise RuntimeError("profile provenance was not initialized")
        bds_protocol = str(self.result.get("bds_version") or "").strip()
        bds_full = str(self.result.get("bds_full_version") or "").strip()
        if not bds_protocol or not bds_full:
            raise RuntimeError("exact BDS version evidence is missing")
        spark = self._candidate_provenance["spark"]
        endstone = self._candidate_provenance["endstone"]
        return {
            "kind": kind,
            "viewer_url": viewer_url,
            "spark_sha": spark["sha"],
            "lab_run_id": self._candidate_provenance["lab_run_id"],
            "bds_version": bds_protocol,
            "bds_full_version": bds_full,
            "bds": {"protocol": bds_protocol, "version": bds_full},
            "endstone_version": self.result["endstone_version"],
            "endstone_sha": endstone["sha"],
            "endstone": {"version": self.result["endstone_version"], "sha": endstone["sha"]},
            "spark_artifact_id": spark["artifact_id"],
            "spark_artifact_digest": spark["artifact_digest"],
            "spark_artifact_run_id": spark["run_id"],
            "endstone_artifact_id": endstone["artifact_id"],
            "endstone_artifact_digest": endstone["artifact_digest"],
            "endstone_artifact_run_id": endstone["run_id"],
            "player_count": player_count,
            "reload_cycle": reload_cycle,
            "allocation_interval": allocation_interval,
            "command": command,
        }

    def _dispatch(self, command: str, timeout: float = 15.0) -> tuple[int, list[str], str]:
        if self.server is None:
            raise RuntimeError(f"Cannot dispatch {command!r} without a live BDS")
        start = self.server.command(command)
        output = self.server.wait_command_output(start, timeout)
        acknowledgement = _validate_command_ack(output, command)
        return start, output, acknowledgement

    def _profile(self, kind: str, player_count: int, reload_cycle: int, allocation_interval: int | None) -> str:
        if kind not in PROFILE_KINDS:
            raise ValueError(f"unsupported profile kind: {kind}")
        if self._profile_active:
            raise RuntimeError(f"profile {kind} started while another profile was active")
        if self.server is None:
            raise RuntimeError(f"cannot run profile {kind} without a live BDS")
        if allocation_interval is None:
            command = f"spark profiler start --timeout {self.profile_seconds}"
        else:
            command = (
                f"spark profiler start --timeout {self.profile_seconds} --alloc "
                f"--interval {allocation_interval}"
            )

        self._profile_active = True
        try:
            start, _, _ = self._dispatch(command, timeout=CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT)
            deadline = time.monotonic() + self.profile_seconds + 90
            url: str | None = None
            while time.monotonic() < deadline:
                url = self._viewer_url(self.server.snapshot(), start)
                if url:
                    break
                recent = "\n".join(self.server.snapshot()[start:]).casefold()
                if "profiler status: failed" in recent or "incomplete profile data was discarded" in recent:
                    raise RuntimeError(f"Spark rejected {kind} profile")
                if not self.server.is_alive():
                    raise RuntimeError(f"BDS exited during {kind} profile")
                time.sleep(0.5)
            if url is None:
                stop_start, _, _ = self._dispatch(
                    "spark profiler stop",
                    timeout=CANDIDATE_PROFILE_COMMAND_ACK_TIMEOUT,
                )
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    url = self._viewer_url(self.server.snapshot(), min(start, stop_start))
                    if url:
                        break
                    if not self.server.is_alive():
                        raise RuntimeError(f"BDS exited while finalizing {kind} profile")
                    time.sleep(0.5)
            if not url:
                raise RuntimeError(f"{kind} profile produced no Spark viewer URL")
            profiles = self.result["profiles"]
            if any(profile.get("viewer_url") == url for profile in profiles):
                raise RuntimeError(f"profile {kind} reused viewer URL {url}")
            if player_count == 0:
                self.wait_player_count(0, timeout=20)
            else:
                self.assert_20_players(f"before-{kind}")
            profiles.append(
                self._common_profile_metadata(
                    kind=kind,
                    viewer_url=url,
                    player_count=player_count,
                    reload_cycle=reload_cycle,
                    allocation_interval=allocation_interval,
                    command=command,
                )
            )
            self._write_results()
            self.check(
                f"profile-{kind}",
                "PASS",
                "distinct Spark viewer profile URL recorded with exact provenance",
                viewer_url=url,
                player_count=player_count,
                reload_cycle=reload_cycle,
                allocation_interval=allocation_interval,
                command=command,
            )
            return url
        finally:
            self._profile_active = False

    def _wait_reload_complete(self, start: int, cycle: int) -> tuple[list[str], str, str, str]:
        if self.server is None:
            raise RuntimeError(f"BDS disappeared during /reload cycle {cycle}")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            lines = self.server.snapshot()[start:]
            if any(_RELOAD_COMPLETE_RE.search(line) for line in lines):
                disable, enable, completion = _ordered_reload_evidence(lines, cycle)
                return lines, disable, enable, completion
            if not self.server.is_alive():
                raise RuntimeError(f"BDS exited during Endstone /reload cycle {cycle}")
            time.sleep(0.25)
        raise RuntimeError(f"Endstone /reload cycle {cycle} did not complete within 60s")

    def _reload(self, cycle: int, baseline_identity: tuple[int, float]) -> None:
        if self._profile_active:
            raise RuntimeError(f"cannot reload while a profile is active (cycle {cycle})")
        if self.server is None:
            raise RuntimeError(f"cannot reload without a live BDS (cycle {cycle})")
        before_identity = _select_live_bds_identity(self.server.process_tree_snapshot())
        if before_identity != baseline_identity:
            raise RuntimeError(
                f"BDS identity changed before reload cycle {cycle}: "
                f"baseline={baseline_identity!r} observed={before_identity!r}"
            )
        self.assert_20_players(f"before-reload-{cycle}")
        start = ServerProcess.command(self.server, "reload")
        reload_lines, spark_disable, spark_enable, completion = self._wait_reload_complete(start, cycle)
        after_identity = _select_live_bds_identity(self.server.process_tree_snapshot())
        if after_identity != baseline_identity:
            raise RuntimeError(
                f"BDS identity changed across reload cycle {cycle}: "
                f"baseline={baseline_identity!r} observed={after_identity!r}"
            )
        self.assert_20_players(f"after-reload-{cycle}")
        record = {
            "cycle": cycle,
            "command": "reload",
            "transport": "stdin",
            "command_published": True,
            "dispatch_acknowledged": False,
            "dispatch_acknowledgement": None,
            "command_acknowledged": False,
            "command_acknowledgement": None,
            "reload_complete": True,
            "reload_completion": completion,
            "spark_enabled": True,
            "spark_disable_evidence": spark_disable,
            "spark_enable_evidence": spark_enable,
            "reload_evidence_order": ["spark-disable", "spark-enable", "reload-complete"],
            "reload_evidence_lines": [spark_disable, spark_enable, completion],
            "bds_pid": after_identity[0],
            "bds_create_time": after_identity[1],
            "before_bds_pid": before_identity[0],
            "before_bds_create_time": before_identity[1],
            "same_bds_identity": True,
            "player_count": BOT_COUNT,
            "reload_output_tail": reload_lines[-20:],
        }
        self.result["plugin_reload_cycles"].append(record)
        self._write_results()

    def _run_reload_cycles(self) -> None:
        if self.server is None:
            raise RuntimeError("cannot run reload cycles without a live BDS")
        baseline_identity = _select_live_bds_identity(self.server.process_tree_snapshot())
        self.result["plugin_reload_cycles"] = []
        for cycle in range(1, RELOAD_CYCLES + 1):
            self._reload(cycle, baseline_identity)
        if len(self.result["plugin_reload_cycles"]) != RELOAD_CYCLES:
            raise RuntimeError("reload cycle oracle did not record exactly three cycles")
        identities = {
            (record.get("bds_pid"), record.get("bds_create_time"))
            for record in self.result["plugin_reload_cycles"]
        }
        if identities != {(baseline_identity[0], baseline_identity[1])}:
            raise RuntimeError(f"reload cycle identities are not unchanged: {identities!r}")
        self.check(
            "three-true-reload-cycles",
            "PASS",
            "three serialized Endstone /reload cycles kept one BDS process, re-enabled Spark, and retained 20 players",
            cycles=RELOAD_CYCLES,
            bds_pid=baseline_identity[0],
            bds_create_time=baseline_identity[1],
        )

    def _set_shutdown_evidence(self) -> None:
        events = copy.deepcopy(self.result.get("shutdown_lifecycle_events") or [])
        final = events[-1] if events else {}
        all_events_clean = bool(events) and all(self._lifecycle_event_is_clean(event) for event in events)
        graceful = (
            self.result.get("shutdown_status") == "graceful"
            and all_events_clean
            and final.get("phase_name") == "candidate-final-shutdown"
        )
        self.result["shutdown_evidence"] = {
            "status": self.result.get("shutdown_status"),
            "graceful": graceful,
            "all_events_clean": all_events_clean,
            "lifecycle_events": events,
        }
        self._write_results()

    @staticmethod
    def _lifecycle_event_is_clean(event: object) -> bool:
        if not isinstance(event, dict):
            return False
        if event.get("wrapper_return_code") != 0:
            return False
        if event.get("returncode") not in (None, 0):
            return False
        if event.get("wrapper_outcome") not in (None, "exited"):
            return False
        if event.get("process_tree_verification") != "clean" or event.get("forced") is not False:
            return False
        acknowledgement = event.get("acknowledgement_evidence")
        observed = isinstance(acknowledgement, dict) and acknowledgement.get("observed") is True
        observed = observed or event.get("acknowledgement_observed") is True
        observed = observed or event.get("acknowledged") is True
        return observed

    def _validate_workload_success(self) -> None:
        profiles = self.result.get("profiles") or []
        if [profile.get("kind") for profile in profiles] != list(PROFILE_KINDS):
            raise RuntimeError(f"profile kinds are not exact: {profiles!r}")
        urls = [str(profile.get("viewer_url") or "").strip() for profile in profiles]
        if len(urls) != 4 or any(not url for url in urls) or len(set(urls)) != 4:
            raise RuntimeError(f"expected four distinct nonempty profile URLs, got {urls!r}")
        expected_profiles = (
            (CPU_BASELINE_KIND, 0, 0, None, f"spark profiler start --timeout {self.profile_seconds}"),
            (CPU_LOAD_KIND, BOT_COUNT, 0, None, f"spark profiler start --timeout {self.profile_seconds}"),
            (
                ALLOCATION_KIND,
                BOT_COUNT,
                0,
                ALLOCATION_INTERVAL_BYTES,
                f"spark profiler start --timeout {self.profile_seconds} --alloc --interval {ALLOCATION_INTERVAL_BYTES}",
            ),
            (
                POST_RELOAD_KIND,
                BOT_COUNT,
                RELOAD_CYCLES,
                None,
                f"spark profiler start --timeout {self.profile_seconds}",
            ),
        )
        for profile, expected in zip(profiles, expected_profiles):
            actual = (
                profile.get("kind"),
                profile.get("player_count"),
                profile.get("reload_cycle"),
                profile.get("allocation_interval"),
                profile.get("command"),
            )
            if actual != expected:
                raise RuntimeError(f"profile metadata is not exact: expected={expected!r} observed={actual!r}")
        reloads = self.result.get("plugin_reload_cycles") or []
        if len(reloads) != RELOAD_CYCLES:
            raise RuntimeError(f"expected exactly three published reload records, got {reloads!r}")
        if [record.get("cycle") for record in reloads] != list(range(1, RELOAD_CYCLES + 1)):
            raise RuntimeError(f"reload cycles are not serialized in order: {reloads!r}")
        identities: set[tuple[int, float]] = set()
        for record in reloads:
            if (
                record.get("command") != "reload"
                or record.get("transport") != "stdin"
                or record.get("command_published") is not True
                or record.get("dispatch_acknowledged") is not False
                or record.get("dispatch_acknowledgement") is not None
                or record.get("command_acknowledged") is not False
                or record.get("command_acknowledgement") is not None
                or record.get("reload_complete") is not True
                or record.get("spark_enabled") is not True
                or record.get("same_bds_identity") is not True
                or record.get("player_count") != BOT_COUNT
                or record.get("reload_evidence_order") != ["spark-disable", "spark-enable", "reload-complete"]
                or not record.get("spark_disable_evidence")
                or not record.get("spark_enable_evidence")
                or not record.get("reload_completion")
            ):
                raise RuntimeError("reload records do not satisfy the ordered same-process/enable/player oracle")
            evidence_lines = record.get("reload_evidence_lines")
            if not isinstance(evidence_lines, list) or len(evidence_lines) != 3:
                raise RuntimeError(f"reload record is missing ordered log evidence: {record!r}")
            try:
                observed_evidence = _ordered_reload_evidence(evidence_lines, int(record["cycle"]))
            except (KeyError, RuntimeError) as exc:
                raise RuntimeError(f"reload record has invalid ordered log evidence: {record!r}") from exc
            if observed_evidence != (
                record["spark_disable_evidence"],
                record["spark_enable_evidence"],
                record["reload_completion"],
            ):
                raise RuntimeError(f"reload record evidence fields do not match logs: {record!r}")
            try:
                pid = _positive_int(record.get("bds_pid"), "reload BDS PID")
                create_time = float(record.get("bds_create_time"))
                before_pid = _positive_int(record.get("before_bds_pid"), "reload before BDS PID")
                before_create_time = float(record.get("before_bds_create_time"))
            except (TypeError, ValueError, RuntimeError) as exc:
                raise RuntimeError(f"reload record has invalid BDS identity: {record!r}") from exc
            if (
                not math.isfinite(create_time)
                or create_time <= 0
                or not math.isfinite(before_create_time)
                or before_create_time <= 0
                or (pid, create_time) != (before_pid, before_create_time)
            ):
                raise RuntimeError(f"reload record changed BDS identity: {record!r}")
            identities.add((pid, create_time))
        if len(identities) != 1:
            raise RuntimeError(f"reload records do not share one BDS identity: {identities!r}")

    def _validate_final_shutdown(self) -> None:
        evidence = self.result.get("shutdown_evidence") or {}
        if evidence.get("graceful") is not True:
            raise RuntimeError(f"clean graceful shutdown evidence is missing: {evidence!r}")
        events = evidence.get("lifecycle_events") or []
        if not events or any(not self._lifecycle_event_is_clean(event) for event in events):
            raise RuntimeError(f"lifecycle events are not all clean and acknowledged: {events!r}")
        if events[-1].get("phase_name") != "candidate-final-shutdown":
            raise RuntimeError(f"final lifecycle phase is not candidate-final-shutdown: {events[-1]!r}")

    def _validate_success(self) -> None:
        self._validate_workload_success()
        self._validate_final_shutdown()

    def _stop_fleet_once(self) -> None:
        if self.bot is None or self._fleet_stopped:
            return
        self.stop_fleet()
        self._fleet_stopped = True

    def _record_cleanup_error(self, operation: str, exc: Exception) -> str:
        diagnostic = traceback.format_exc()
        self.result.setdefault("cleanup_errors", []).append(
            {
                "operation": operation,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": diagnostic,
            }
        )
        return f"\n\n{operation} cleanup failure:\n{diagnostic}"

    def _cleanup_step(self, operation: str, action: Any) -> str:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - cleanup must continue after secondary failures
            return self._record_cleanup_error(operation, exc)
        return ""

    def _cleanup_is_alive(self, owner: Any, operation: str) -> bool:
        try:
            return bool(owner.is_alive())
        except Exception as exc:  # noqa: BLE001 - a failed probe must not skip cleanup
            self._record_cleanup_error(f"{operation} is_alive", exc)
            return True

    def _clear_terminal_pending_file_commands(
        self,
        pending_commands: dict[int, str],
        pending_before: dict[int, str],
        diagnostic: dict[str, Any],
    ) -> str:
        command_path = getattr(self.server, "lifecycle_command_path", None) if self.server is not None else None
        diagnostic["status"] = "terminated"
        diagnostic["request_path"] = str(command_path) if command_path is not None else None
        diagnostic["request_removed"] = command_path is None
        cleanup = ""
        try:
            if command_path is not None:
                pathlib.Path(command_path).unlink(missing_ok=True)
                diagnostic["request_removed"] = True
        except Exception as exc:  # noqa: BLE001 - terminal cleanup must preserve primary failure
            diagnostic["request_removed"] = False
            diagnostic["request_removal_error"] = f"{type(exc).__name__}: {exc}"
            cleanup += self._record_cleanup_error("pending CI command request removal", exc)
        finally:
            for start_index in pending_before:
                pending_commands.pop(start_index, None)
        return cleanup

    def _cleanup_after_failure(self, diagnostic: str) -> str:
        cleanup = diagnostic
        try:
            self._stop_fleet_once()
        except Exception as exc:  # noqa: BLE001 - cleanup must continue after secondary failures
            cleanup += self._record_cleanup_error("fleet graceful stop", exc)
            if self.bot is not None and self._cleanup_is_alive(self.bot, "bot force_close"):
                cleanup += self._cleanup_step("bot force_close", self.bot.force_close)
        pending_commands = getattr(self.server, "_pending_file_commands", {}) if self.server is not None else {}
        if pending_commands:
            pending_before = dict(pending_commands)
            drain = getattr(self.server, "wait_for_pending_file_commands", None)
            drained = False
            if callable(drain):
                try:
                    drained = drain(PENDING_COMMAND_DRAIN_TIMEOUT) is True
                except Exception as exc:  # noqa: BLE001 - force cleanup must follow a failed drain
                    cleanup += self._record_cleanup_error("pending CI command acknowledgement", exc)
            pending_diagnostic = {
                "operation": "pending CI command acknowledgement",
                "status": "drained" if drained else "unresolved",
                "pending": pending_before,
                "timeout": PENDING_COMMAND_DRAIN_TIMEOUT,
            }
            self.result.setdefault("cleanup_diagnostics", []).append(pending_diagnostic)
            if pending_commands and not drained:
                force_cleanup = self._cleanup_step(
                    "BDS force_kill_tree after pending CI command",
                    self.server.force_kill_tree,
                )
                cleanup += force_cleanup
                if not force_cleanup and not self._cleanup_is_alive(self.server, "terminal pending CI command cleanup"):
                    cleanup += self._clear_terminal_pending_file_commands(
                        pending_commands,
                        pending_before,
                        pending_diagnostic,
                    )
                elif force_cleanup:
                    pending_diagnostic["status"] = "force-cleanup-failed"
                else:
                    pending_diagnostic["status"] = "force-cleanup-incomplete"
                cleanup += self._cleanup_step("shutdown evidence", self._set_shutdown_evidence)
                return cleanup
        if self.server is not None and self._cleanup_is_alive(self.server, "BDS graceful shutdown"):
            cleanup += self._cleanup_step(
                "shutdown phase context",
                lambda: self._set_phase_shutdown_context("candidate-failure-cleanup"),
            )
            cleanup += self._cleanup_step("BDS graceful shutdown", self.shutdown)
        if self.server is not None and self._cleanup_is_alive(self.server, "BDS force_kill_tree"):
            cleanup += self._cleanup_step("BDS force_kill_tree", self.server.force_kill_tree)
        cleanup += self._cleanup_step("shutdown evidence", self._set_shutdown_evidence)
        return cleanup

    def _finalize_cleanup(self) -> None:
        if self.bot is not None and self._cleanup_is_alive(self.bot, "bot force_close"):
            self._cleanup_step("bot force_close", self.bot.force_close)
        if self.server is not None and self._cleanup_is_alive(self.server, "BDS force_kill_tree"):
            self._cleanup_step("BDS force_kill_tree", self.server.force_kill_tree)
        if self.server is not None:
            self._cleanup_step("server close", self.server.close)
        self.result["completed_at"] = now_iso()
        self._cleanup_step("log splitting", self.split_logs)
        try:
            self._write_results()
        except Exception as exc:  # noqa: BLE001 - final persistence is best effort
            self._record_cleanup_error("result persistence", exc)
            self._cleanup_step("result persistence retry", self._write_results)

    def execute_candidate(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            stage = "world-bootstrap"
            self.bootstrap_scenario_world()
            stage = "bds-start"
            self.start_server()
            self.wait_post_start_initialization()
            self.run_basic_commands()

            stage = "cpu-baseline"
            self.wait_player_count(0, timeout=45)
            self._profile(CPU_BASELINE_KIND, 0, 0, None)

            stage = "20-player-fleet"
            self.start_fleet()
            time.sleep(20)
            self.assert_20_players("before-profiles")

            stage = "20-player-cpu"
            self._profile(CPU_LOAD_KIND, BOT_COUNT, 0, None)
            stage = "20-player-allocation-4096"
            self._profile(ALLOCATION_KIND, BOT_COUNT, 0, ALLOCATION_INTERVAL_BYTES)

            stage = "three-true-reload-cycles"
            self._run_reload_cycles()
            stage = "post-reload-cpu"
            self._profile(POST_RELOAD_KIND, BOT_COUNT, RELOAD_CYCLES, None)
            self._validate_workload_success()

            stage = "graceful-shutdown"
            self._stop_fleet_once()
            self._set_phase_shutdown_context("candidate-final-shutdown")
            self.shutdown()
            self._set_shutdown_evidence()
            self._validate_final_shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self._write_results()
            return 0
        except Exception as exc:  # noqa: BLE001 - integration failures become evidence
            self.result["status"] = "FAIL"
            self.result["state"] = "failed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"[:1200]
            diagnostic = traceback.format_exc()
            diagnostic = self._cleanup_after_failure(diagnostic)
            try:
                last_lines = self.server.snapshot()[-400:] if self.server is not None else []
            except Exception as snapshot_exc:  # noqa: BLE001 - diagnostics cannot block result persistence
                diagnostic += self._record_cleanup_error("failure log snapshot", snapshot_exc)
                last_lines = []
            diagnostic += "\n\nCleanup errors:\n" + json.dumps(
                self.result.get("cleanup_errors", []), indent=2, sort_keys=True
            )
            diagnostic += "\n\nLast BDS log lines:\n" + "\n".join(last_lines)
            diagnostic += "\n\nShutdown evidence:\n" + json.dumps(
                self.result.get("shutdown_evidence"), indent=2, sort_keys=True
            )
            try:
                self.diagnostics.write_text(diagnostic, encoding="utf-8")
            except Exception as diagnostics_exc:  # noqa: BLE001 - final cleanup must continue
                self._record_cleanup_error("failure diagnostics persistence", diagnostics_exc)
            try:
                self._write_results()
            except Exception as result_exc:  # noqa: BLE001 - final cleanup retries persistence
                self._record_cleanup_error("failure result persistence", result_exc)
            return 1
        finally:
            self._finalize_cleanup()
            print(json.dumps(self.result, indent=2, sort_keys=True), flush=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()
    return Spark51WindowsBdsValidation(pathlib.Path(args.bot), args.profile_seconds).execute_candidate()


if __name__ == "__main__":
    raise SystemExit(main())
