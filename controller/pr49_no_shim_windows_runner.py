from __future__ import annotations

import os
import pathlib
import shutil
import sys
import time
from typing import Any

# Importing the final Windows runner first installs the exact-artifact lifecycle,
# behavior-pack state oracle, restart-safe command transport, and one-bootstrap
# Windows adapter. This module replaces only artifact installation and adds
# PR49-specific allocation semantic gates.
from controller import combined_windows_final_runner  # noqa: F401
from controller.combined_pack_gamerule_fleet_validation import CombinedPackGameruleFleetValidation, main
from controller.python_evidence_provenance import (
    validate_component_provenance,
    validate_endstone_runtime_version,
)
from controller.run_test import locate_one, run_checked
from providers import artifact_provider

SPARK_REPOSITORY = "ReallocAll/spark"
NO_SHIM_WORKFLOW = "Windows No-Shim Real Plugin Experiment"
_ORIGINAL_RUN_PROFILER = CombinedPackGameruleFleetValidation.run_profiler


def _positive_env(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} is required for PR #49 no-shim validation")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _expected_spark_sha() -> str:
    value = os.environ.get("EXPECTED_SPARK_SHA", "").strip().lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise RuntimeError("EXPECTED_SPARK_SHA must be an exact 40-character hexadecimal commit SHA")
    return value


def _validate_no_shim_run(run: dict[str, Any], *, expected_sha: str, expected_run_id: int) -> None:
    if int(run.get("id") or 0) != expected_run_id:
        raise RuntimeError(f"Spark workflow run mismatch: observed={run.get('id')!r} expected={expected_run_id}")
    if str(run.get("head_sha") or "").strip().lower() != expected_sha:
        raise RuntimeError(
            f"Spark workflow SHA mismatch: observed={run.get('head_sha')!r} expected={expected_sha!r}"
        )
    if run.get("conclusion") != "success":
        raise RuntimeError(f"Spark no-shim workflow is not successful: conclusion={run.get('conclusion')!r}")
    if run.get("name") != NO_SHIM_WORKFLOW:
        raise RuntimeError(
            f"Spark workflow mismatch: observed={run.get('name')!r} expected={NO_SHIM_WORKFLOW!r}"
        )


def _select_no_shim_artifact(
    artifacts: list[dict[str, Any]], *, expected_sha: str, expected_artifact_id: int
) -> dict[str, Any]:
    matches = [artifact for artifact in artifacts if int(artifact.get("id") or 0) == expected_artifact_id]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one Spark artifact id={expected_artifact_id}, observed={len(matches)}"
        )
    artifact = matches[0]
    expected_name = f"spark-windows-no-shim-{expected_sha}"
    if artifact.get("name") != expected_name:
        raise RuntimeError(
            f"Spark artifact name mismatch: observed={artifact.get('name')!r} expected={expected_name!r}"
        )
    if bool(artifact.get("expired")):
        raise RuntimeError(f"Spark no-shim artifact {expected_artifact_id} is expired")
    return artifact


def _resolve_exact_no_shim_spark() -> tuple[dict[str, Any], dict[str, Any]]:
    expected_sha = _expected_spark_sha()
    expected_run_id = _positive_env("EXPECTED_SPARK_RUN_ID")
    expected_artifact_id = _positive_env("EXPECTED_SPARK_ARTIFACT_ID")

    run = artifact_provider._get_json(f"/repos/{SPARK_REPOSITORY}/actions/runs/{expected_run_id}")
    _validate_no_shim_run(run, expected_sha=expected_sha, expected_run_id=expected_run_id)
    artifacts = artifact_provider._get_json(
        f"/repos/{SPARK_REPOSITORY}/actions/runs/{expected_run_id}/artifacts?per_page=100"
    ).get("artifacts") or []
    artifact = _select_no_shim_artifact(
        artifacts,
        expected_sha=expected_sha,
        expected_artifact_id=expected_artifact_id,
    )
    return run, artifact


def _assert_no_shim_payload(root: pathlib.Path) -> None:
    forbidden = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.casefold().startswith("spark_allocation_shim.")
    ]
    if forbidden:
        rendered = ", ".join(str(path.relative_to(root)) for path in forbidden[:10])
        raise RuntimeError(f"no-shim artifact unexpectedly contains spark_allocation_shim payload: {rendered}")

    dependents = locate_one(root, ["endstone_spark-dependents.txt"])
    dependency_text = dependents.read_text(encoding="utf-8", errors="replace").casefold()
    if "spark_allocation_shim.dll" in dependency_text:
        raise RuntimeError("endstone_spark.dll dependency evidence still references spark_allocation_shim.dll")

    targets = locate_one(root, ["no-shim-targets.txt"])
    target_text = targets.read_text(encoding="utf-8", errors="replace").casefold()
    if "spark_allocation_shim" in target_text:
        raise RuntimeError("no-shim target evidence still exposes spark_allocation_shim")


def _validate_live_only_start_output(lines: list[str]) -> None:
    joined = "\n".join(lines).casefold()
    required = (
        "retained allocation profiler is now running",
        "only sampled allocations still live when profiling stops",
    )
    missing = [marker for marker in required if marker not in joined]
    if missing:
        raise RuntimeError(f"live-only allocation profiler did not confirm retained mode: missing={missing!r}")


def _validate_live_only_info_output(lines: list[str]) -> None:
    joined = "\n".join(lines).casefold()
    required = (
        "retained allocation profiler is already running",
        "tracked sampled allocations still live process-wide",
        "process-wide tracked lifecycle:",
    )
    missing = [marker for marker in required if marker not in joined]
    if missing:
        raise RuntimeError(f"live-only allocation profiler info did not confirm retained semantics: missing={missing!r}")


def _validate_live_only_post_stop_output(lines: list[str]) -> bool:
    joined = "\n".join(lines).casefold()
    if "results are still being finalized" in joined:
        return False
    if "retained allocation profiler is already running" in joined:
        raise RuntimeError("live-only allocation profiler retained session is still active after finalization")
    if "the profiler isn't running" in joined:
        return True
    if "profiler is already running" in joined and "started automatically when spark enabled" in joined:
        return True
    raise RuntimeError("live-only allocation profiler reached an unexpected profiler state after finalization")


def _run_live_only_allocation_profile(self: CombinedPackGameruleFleetValidation) -> str:
    assert self.server is not None
    start = self.server.command("spark profiler start --alloc-live-only")
    startup = self.server.wait_command_output(start, 12)
    _validate_live_only_start_output(startup)

    info_at = self.server.command("spark profiler info")
    info = self.server.wait_command_output(info_at, 12)
    _validate_live_only_info_output(info)

    time.sleep(5)
    stop_at = self.server.command("spark profiler stop")
    stop_output = self.server.wait_command_output(stop_at, 12)
    stop_joined = "\n".join(stop_output).casefold()
    if "there isn't an active profiler running" in stop_joined:
        raise RuntimeError("live-only allocation profiler stopped before the explicit finalization gate")

    search_start = min(start, stop_at)
    deadline = time.monotonic() + 60
    url: str | None = None
    while time.monotonic() < deadline:
        lines = self.server.snapshot()
        recent = "\n".join(lines[search_start:]).casefold()
        if "allocation profiler status: failed" in recent or "incomplete profile data was discarded" in recent:
            raise RuntimeError("live-only allocation profiler reported backend failure during real BDS validation")
        url = self._viewer_url(lines, search_start)
        if url:
            break
        if not self.server.is_alive():
            raise RuntimeError("BDS exited while finalizing the live-only allocation profile")
        time.sleep(0.5)
    if url is None:
        raise RuntimeError("live-only allocation profiler produced no spark viewer URL")

    settled_deadline = time.monotonic() + 20
    while time.monotonic() < settled_deadline:
        info_at = self.server.command("spark profiler info")
        state = self.server.wait_command_output(info_at, 12)
        if _validate_live_only_post_stop_output(state):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("live-only allocation profiler export did not settle after finalization")

    self.result["allocation_live_only_profile_viewer_url"] = url
    self._write_results()
    self.check(
        "pr49-allocation-live-only-profiler",
        "PASS",
        "retained allocation mode started, reported live lifecycle semantics, finalized, released the retained session, and restored background/idle state",
        viewer_url=url,
    )
    return url


def _run_pr49_profiler(self: CombinedPackGameruleFleetValidation, allocation: bool) -> str | None:
    url = _ORIGINAL_RUN_PROFILER(self, allocation)
    if allocation:
        _run_live_only_allocation_profile(self)
        self.assert_20_players("after-live-only-allocation-profile")
    return url


def _install_pr49_no_shim_artifacts(self: CombinedPackGameruleFleetValidation) -> None:
    if self.platform != "windows":
        raise RuntimeError("PR #49 no-shim validation is Windows-only")

    self.disable_bstats = True
    self.downloads.mkdir(parents=True, exist_ok=True)

    expected_endstone_sha = os.environ.get("EXPECTED_ENDSTONE_SHA", "").strip() or None
    endstone_run, endstone_artifact = artifact_provider.discover(
        "endstone",
        "windows",
        expected_sha=expected_endstone_sha,
    )
    endstone_payload = artifact_provider._download_artifact(
        artifact_provider.COMPONENTS["endstone"]["repo"],
        endstone_artifact,
        self.downloads / "endstone",
    )

    spark_run, spark_artifact = _resolve_exact_no_shim_spark()
    spark_payload = artifact_provider._download_artifact(
        SPARK_REPOSITORY,
        spark_artifact,
        self.downloads / "spark",
    )
    _assert_no_shim_payload(spark_payload)

    self.metadata = {
        "platform": "windows",
        "components": {
            "endstone": artifact_provider._metadata(
                "endstone",
                artifact_provider.COMPONENTS["endstone"]["repo"],
                endstone_run,
                endstone_artifact,
            ),
            "spark": artifact_provider._metadata(
                "spark",
                SPARK_REPOSITORY,
                spark_run,
                spark_artifact,
            ),
        },
    }
    self.metadata["components"]["endstone"]["payload_dir"] = str(endstone_payload)
    self.metadata["components"]["spark"]["payload_dir"] = str(spark_payload)
    artifact_provider.save_metadata(self.metadata, self.metadata_path)
    self.check(
        "artifact-discovery",
        "PASS",
        "exact no-shim Spark workflow run and artifact selected",
        spark_run_id=spark_run.get("id"),
        spark_artifact_id=spark_artifact.get("id"),
        spark_artifact_name=spark_artifact.get("name"),
        spark_workflow=spark_run.get("name"),
    )

    wheel = locate_one(endstone_payload, ["endstone-*-cp313-cp313-*.whl", "endstone-*.whl"])
    self.check("endstone-wheel-located", "PASS", str(wheel.relative_to(self.root)))
    run_checked(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--force-reinstall",
            str(wheel),
        ],
        timeout=300,
    )

    spark_binary = locate_one(spark_payload, ["endstone_spark.dll"])
    self.server_dir.mkdir(parents=True, exist_ok=True)
    plugin_dir = self.server_dir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    self._prepare_bstats_before_start()
    target = plugin_dir / spark_binary.name
    shutil.copy2(spark_binary, target)
    if any(path.name.casefold().startswith("spark_allocation_shim.") for path in plugin_dir.rglob("*")):
        raise RuntimeError("spark_allocation_shim payload appeared in the deployed plugin directory")
    self.check("spark-plugin-deployed", "PASS", str(target.relative_to(self.root)))
    self.check(
        "spark-no-shim-deployed",
        "PASS",
        "endstone_spark.dll deployed with no shim payload or shim dependency evidence",
        spark_run_id=spark_run.get("id"),
        spark_artifact_id=spark_artifact.get("id"),
    )

    spark = validate_component_provenance(self.metadata, "spark")
    endstone = validate_component_provenance(self.metadata, "endstone")
    runtime_version = validate_endstone_runtime_version()
    self.check(
        "exact-artifact-provenance",
        "PASS",
        spark_sha=spark.get("sha"),
        spark_run_id=spark.get("run_id"),
        spark_artifact_id=(spark.get("artifact") or {}).get("id"),
        spark_workflow=spark.get("workflow"),
        endstone_sha=endstone.get("sha"),
        endstone_run_id=endstone.get("run_id"),
        endstone_artifact_id=(endstone.get("artifact") or {}).get("id"),
        endstone_runtime_version=runtime_version,
        allocation_shim="absent",
    )


CombinedPackGameruleFleetValidation.install_artifacts = _install_pr49_no_shim_artifacts
CombinedPackGameruleFleetValidation.run_profiler = _run_pr49_profiler


if __name__ == "__main__":
    raise SystemExit(main())
