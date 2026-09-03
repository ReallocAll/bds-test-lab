from __future__ import annotations

import pathlib
import shutil
import sys
from typing import Any

from providers import artifact_provider


_ORIGINAL_ARTIFACT_SCORE = artifact_provider._artifact_score


def _pr49_artifact_score(component: str, platform_name: str, artifact: dict[str, Any]) -> int:
    """Require the dedicated real-plugin no-shim artifact for PR #49 Windows validation."""

    score = _ORIGINAL_ARTIFACT_SCORE(component, platform_name, artifact)
    if component != "spark" or platform_name != "windows":
        return score
    name = str(artifact.get("name", "")).lower()
    if "no-shim" not in name:
        return -10_000
    return score + 1_000


artifact_provider._artifact_score = _pr49_artifact_score

from controller import run_test  # noqa: E402  # patch artifact policy before loading the Windows exact runner


def _install_shimless_windows_artifacts(self: run_test.IntegrationTest) -> None:
    if self.platform != "windows":
        raise RuntimeError("PR49 no-shim runner is Windows-only")

    self.metadata = run_test.resolve_artifacts(self.platform, self.downloads, self.metadata_path)
    self.check("artifact-discovery", "PASS")

    spark_component = (self.metadata.get("components") or {}).get("spark") or {}
    spark_artifact = spark_component.get("artifact") or {}
    artifact_name = str(spark_artifact.get("name") or "")
    if "no-shim" not in artifact_name.lower():
        raise RuntimeError(f"resolved Spark artifact is not the dedicated no-shim artifact: {artifact_name!r}")
    self.check("spark-no-shim-artifact-selected", "PASS", artifact_name)

    endstone_root = self.downloads / "endstone" / "payload"
    wheel = run_test.locate_one(endstone_root, ["endstone-*-cp313-cp313-*.whl", "endstone-*.whl"])
    self.check("endstone-wheel-located", "PASS", str(wheel.relative_to(self.root)))
    run_test.run_checked(
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

    spark_root = self.downloads / "spark" / "payload"
    spark_binary = run_test.locate_one(spark_root, ["endstone_spark.dll"])
    shim_files = sorted(path for path in spark_root.rglob("spark_allocation_shim.dll") if path.is_file())
    if shim_files:
        relative = [str(path.relative_to(self.root)) for path in shim_files]
        raise RuntimeError(f"no-shim Spark artifact unexpectedly contains spark_allocation_shim.dll: {relative}")

    self.server_dir.mkdir(parents=True, exist_ok=True)
    plugin_dir = self.server_dir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    self._prepare_bstats_before_start()

    target = plugin_dir / spark_binary.name
    shutil.copy2(spark_binary, target)
    self.check("spark-plugin-deployed", "PASS", str(target.relative_to(self.root)))

    deployed_shim = plugin_dir / "spark_allocation_shim.dll"
    if deployed_shim.exists():
        raise RuntimeError(f"no-shim deployment directory unexpectedly contains {deployed_shim}")
    self.check(
        "spark-allocation-shim-absent",
        "PASS",
        "dedicated PR49 artifact and deployed plugin directory contain no spark_allocation_shim.dll",
    )


run_test.IntegrationTest.install_artifacts = _install_shimless_windows_artifacts

# Import only after the base artifact installer is replaced. The exact runner
# captures the current CombinedPackGameruleFleetValidation.install_artifacts
# implementation during import, so this order makes every measured lifecycle use
# the strict no-shim deployment contract without changing the production lab path.
from controller import combined_windows_final_runner as combined  # noqa: E402


_ORIGINAL_ASSERT_20_PLAYERS = combined.CombinedPackGameruleFleetValidation.assert_20_players


def _assert_20_players_with_real_hot_reload(
    self: combined.CombinedPackGameruleFleetValidation,
    phase: str,
) -> None:
    _ORIGINAL_ASSERT_20_PLAYERS(self, phase)
    if self.platform != "windows" or phase != "before-public-reports" or self.result.get("hot_reload_completed"):
        return

    if self.server is None or not self.server.is_alive():
        raise RuntimeError("BDS is not alive before PR49 real hot-reload stress")

    cycles = 10
    evidence: list[dict[str, Any]] = []
    for cycle in range(1, cycles + 1):
        start = self.server.command("reload")
        self.server.wait_command_output(start, timeout=20)

        def spark_reenabled(lines: list[str]) -> bool:
            return any(
                "spark" in line.lower() and any(hint in line.lower() for hint in run_test.SPARK_LOAD_HINTS)
                for line in lines[start:]
            )

        lines = self.server.wait_for(spark_reenabled, 90, f"Spark re-enable after /reload cycle {cycle}")
        recent = lines[start:]
        lowered = "\n".join(recent).casefold()
        fatal_markers = (
            "failed to load c++ plugin",
            "access violation",
            "unhandled exception",
            "fatal error",
        )
        observed_fatal = next((marker for marker in fatal_markers if marker in lowered), None)
        if observed_fatal is not None:
            raise RuntimeError(f"fatal marker after /reload cycle {cycle}: {observed_fatal}")
        if not self.server.is_alive():
            raise RuntimeError(f"BDS exited during /reload cycle {cycle}")

        self.command_check(
            f"windows-no-shim-hot-reload-{cycle}-spark-command",
            "spark profiler info",
            timeout=15,
        )
        _ORIGINAL_ASSERT_20_PLAYERS(self, f"hot-reload-{cycle}")
        evidence.append(
            {
                "cycle": cycle,
                "spark_reenabled": True,
                "players": 20,
                "server_alive": True,
            }
        )

    self.result["hot_reload_completed"] = True
    self.result["hot_reload_cycles"] = cycles
    self.result["hot_reload_evidence"] = evidence
    self._write_results()
    self.check(
        "windows-no-shim-real-dll-hot-reload",
        "PASS",
        "10 Endstone /reload cycles completed under 20-player chunk-walk load; each cycle re-enabled Spark and retained all players",
        cycles=cycles,
        mechanism="Endstone reload -> clearPlugins -> CppPluginLoader owner destruction -> FreeLibrary -> loadPlugins",
    )


combined.CombinedPackGameruleFleetValidation.assert_20_players = _assert_20_players_with_real_hot_reload


if __name__ == "__main__":
    raise SystemExit(combined.exact.main())
