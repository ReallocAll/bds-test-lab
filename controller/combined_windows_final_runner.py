from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Importing the exact runner installs the exact-artifact, Windows lifecycle,
# behavior-pack state-oracle, restart-safe command transport, and provenance
# adapters before we replace only the Windows bootstrap below.
from controller import combined_pack_gamerule_fleet_exact_runner as exact
from controller.bot_validation import patch_server_properties
from controller.combined_pack_gamerule_fleet_validation import (
    WORLD_NAME,
    CombinedPackGameruleFleetValidation,
)
from controller.fleet_spark_validation import set_server_property
from controller.python_evidence_provenance import (
    validate_component_provenance,
    validate_endstone_runtime_version,
)
from controller.run_test import locate_one, run_checked
from providers.artifact_provider import resolve_artifacts

_ORIGINAL_BOOTSTRAP_SCENARIO_WORLD = CombinedPackGameruleFleetValidation.bootstrap_scenario_world
_ORIGINAL_EXACT_INSTALL_ARTIFACTS = CombinedPackGameruleFleetValidation.install_artifacts


def _world_directories(server_dir: Path) -> dict[str, Path]:
    worlds_root = server_dir / "worlds"
    if not worlds_root.exists():
        return {}
    return {path.name: path for path in worlds_root.iterdir() if path.is_dir()}


def _install_windows_no_shim_artifacts(self: CombinedPackGameruleFleetValidation) -> None:
    if self.platform != "windows":
        _ORIGINAL_EXACT_INSTALL_ARTIFACTS(self)
        return

    self.disable_bstats = True
    self.metadata = resolve_artifacts(self.platform, self.downloads, self.metadata_path)
    self.check("artifact-discovery", "PASS")

    endstone_root = self.downloads / "endstone" / "payload"
    wheel = locate_one(endstone_root, ["endstone-*-cp313-cp313-*.whl", "endstone-*.whl"])
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

    spark_root = self.downloads / "spark" / "payload"
    spark_binary = locate_one(spark_root, ["endstone_spark.dll"])
    shim_payloads = list(spark_root.rglob("spark_allocation_shim.dll"))
    if shim_payloads:
        raise RuntimeError(
            "shimless Windows research selected an artifact containing spark_allocation_shim.dll: "
            + ", ".join(str(path.relative_to(self.root)) for path in shim_payloads)
        )

    self.server_dir.mkdir(parents=True, exist_ok=True)
    plugin_dir = self.server_dir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    self._prepare_bstats_before_start()
    target = plugin_dir / spark_binary.name
    shutil.copy2(spark_binary, target)
    self.check("spark-plugin-deployed", "PASS", str(target.relative_to(self.root)))
    self.check(
        "spark-allocation-shim-absent",
        "PASS",
        "selected artifact and deployed plugin directory contain no spark_allocation_shim.dll",
    )

    spark = validate_component_provenance(self.metadata, "spark")
    endstone = validate_component_provenance(self.metadata, "endstone")
    runtime_version = validate_endstone_runtime_version()
    self.check(
        "exact-artifact-provenance",
        "PASS",
        spark_sha=spark.get("sha"),
        spark_run_id=spark.get("run_id"),
        spark_workflow=spark.get("workflow"),
        spark_artifact_name=(spark.get("artifact") or {}).get("name"),
        endstone_sha=endstone.get("sha"),
        endstone_run_id=endstone.get("run_id"),
        endstone_artifact_id=(endstone.get("artifact") or {}).get("id"),
        endstone_runtime_version=runtime_version,
    )


def _bootstrap_windows_from_provisioned_world(self: CombinedPackGameruleFleetValidation) -> None:
    """Create one real BDS world, then reuse it for the exact Windows case.

    The former exact Windows path started BDS a second time solely to create a
    differently named fresh world and then immediately asked the interactive
    Endstone wrapper to stop again. Hosted Windows evidence showed that second
    command could remain unacknowledged even though the first shutdown was
    fully graceful. The extra lifecycle is not part of the Spark workload.

    This adapter keeps the strong world oracle: BDS itself must create exactly
    one new world directory during the provisioning boot. Only after a clean
    shutdown do we rename that BDS-created world, apply the same server
    properties, and install the same three behavior packs offline. The final
    measured server still loads the renamed real world and all existing
    behavior-pack, gamerule, 20-player, profile, provenance, bStats, and
    shutdown validators remain unchanged.
    """

    if self.platform != "windows":
        _ORIGINAL_BOOTSTRAP_SCENARIO_WORLD(self)
        return

    before = _world_directories(self.server_dir)
    if WORLD_NAME in before:
        raise RuntimeError(f"target world already exists before bootstrap: {WORLD_NAME}")

    self.start_server()
    self.wait_post_start_initialization()
    self.stop_server_for_phase_change("bootstrap-provisioning")

    after = _world_directories(self.server_dir)
    created_names = sorted(set(after) - set(before))
    if len(created_names) != 1:
        raise RuntimeError(
            "BDS provisioning must create exactly one fresh world directory; "
            f"created={created_names!r}, before={sorted(before)!r}, after={sorted(after)!r}"
        )

    source_world = after[created_names[0]]
    target_world = self.server_dir / "worlds" / WORLD_NAME
    if target_world.exists():
        raise RuntimeError(f"target world unexpectedly exists after bootstrap: {target_world}")
    source_world.rename(target_world)
    if not target_world.is_dir():
        raise RuntimeError(f"failed to preserve BDS-created world at {target_world}")

    properties = self.server_dir / "server.properties"
    patch_server_properties(properties)
    set_server_property(properties, "max-players", "30")
    set_server_property(properties, "level-name", WORLD_NAME)
    set_server_property(properties, "allow-cheats", "true")
    set_server_property(properties, "player-idle-timeout", "0")

    self.install_behavior_packs(target_world)
    self.check(
        "combined-world-bootstrap",
        "PASS",
        "one fresh BDS-provisioned world reused; offline mode, cheats, max-players=30, and three behavior packs configured",
        source_world=created_names[0],
        target_world=WORLD_NAME,
        windows_bootstrap_server_starts=1,
    )


CombinedPackGameruleFleetValidation.install_artifacts = _install_windows_no_shim_artifacts
CombinedPackGameruleFleetValidation.bootstrap_scenario_world = _bootstrap_windows_from_provisioned_world


if __name__ == "__main__":
    raise SystemExit(exact.main())
