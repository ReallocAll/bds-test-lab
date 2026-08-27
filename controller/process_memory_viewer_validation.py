#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

from controller.block_actor_validation import _fields, _last_message
from controller.combined_pack_gamerule_fleet_validation import CombinedPackGameruleFleetValidation


def decode_process_memory(health_data: bytes) -> dict[str, int | None]:
    metadata = _last_message(health_data, 1)  # HealthData.metadata
    platform_statistics = _last_message(metadata, 3)  # HealthMetadata.platform_statistics
    memory = _last_message(platform_statistics, 1)  # PlatformStatistics.memory
    heap = _last_message(memory, 1)  # PlatformStatistics.Memory.heap / MemoryUsage
    values = {
        number: int(value)
        for number, wire, value in _fields(heap)
        if wire == 0 and isinstance(value, int)
    }
    return {
        "used": values.get(1),
        "committed": values.get(2),
        "max": values.get(4),
    }


class ProcessMemoryViewerValidation(CombinedPackGameruleFleetValidation):
    def validate_local_metadata_with_20_players(self) -> None:
        super().validate_local_metadata_with_20_players()

        # Capture one more payload after the combined metadata has converged so
        # this assertion is made under the same real 20-player, behavior-pack,
        # and modified-gamerule load. The upstream viewer divides heap.used by
        # heap.committed for its top-level Memory(process) widget, so committed
        # must be a finite nonzero denominator at this compatibility surface.
        process_memory = decode_process_memory(self.capture_health_payload())
        used = process_memory["used"]
        committed = process_memory["committed"]
        maximum = process_memory["max"]
        if used is None or used <= 0:
            raise RuntimeError(f"process memory used must be positive, got {used!r}")
        if committed is None or committed <= 0:
            raise RuntimeError(
                "process memory viewer denominator is missing/zero; "
                f"used={used!r}, committed={committed!r}, max={maximum!r}"
            )
        if committed < used:
            raise RuntimeError(
                "process memory viewer denominator is smaller than current RSS; "
                f"used={used}, committed={committed}, max={maximum!r}"
            )
        if maximum is not None and maximum <= 0:
            raise RuntimeError(f"present process memory max must be positive, got {maximum}")

        self.result["process_memory_viewer"] = process_memory
        self._write_results()
        self.check(
            "combined-process-memory-viewer-denominator",
            "PASS",
            "PlatformStatistics.memory.heap has a finite nonzero viewer denominator under 20-player load",
            **process_memory,
            percent=round(100.0 * used / committed, 3),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()
    validator = ProcessMemoryViewerValidation(
        args.platform,
        pathlib.Path(args.bot),
        args.profile_seconds,
    )
    code = validator.execute_combined()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
