#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pathlib
import time

from controller.cross_platform_fleet_validation import CrossPlatformFleetSparkValidation
from controller.run_test import now_iso


class AllocationRateScenarioValidation(CrossPlatformFleetSparkValidation):
    def __init__(
        self,
        platform_name: str,
        bot_binary: pathlib.Path,
        scenario: str,
        profile_seconds: int,
    ) -> None:
        super().__init__(platform_name, bot_binary, 20, scenario, profile_seconds)
        self.result.update(
            {
                "test_kind": "spark-allocation-rate-scenario-comparison",
                "health_upload_viewer_url": None,
            }
        )
        self._write_results()

    def run_public_health_upload(self) -> str:
        assert self.server is not None
        start = self.server.command("spark health upload")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            lines = self.server.snapshot()
            recent = lines[start:]
            if any("health report upload failed" in line.casefold() for line in recent):
                raise RuntimeError("Spark health upload failed: " + " | ".join(recent[-40:]))
            url = self._viewer_url(lines, start)
            if url and any("health report uploaded!" in line.casefold() for line in recent):
                self.result["health_upload_viewer_url"] = url
                self._write_results()
                self.check(
                    "allocation-rate-scenario-health-upload",
                    "PASS",
                    f"health upload captured with 20 {self.scenario} players online",
                    viewer_url=url,
                )
                return url
            if not self.server.is_alive():
                raise RuntimeError("BDS exited while waiting for scenario health upload")
            time.sleep(0.5)
        raise RuntimeError("scenario health upload produced no viewer URL")

    def profile_execution(self) -> tuple[str, list[int]]:
        # The inherited execute() calls this after a 20-second settle and a
        # confirmed 20/20 BDS list. Capture health immediately before the
        # execution profile so allocation-rate and load metrics describe the
        # same online scenario.
        self.run_public_health_upload()
        return super().profile_execution()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--scenario", required=True, choices=["idle", "chunk-walk"])
    parser.add_argument("--profile-seconds", type=int, default=30)
    args = parser.parse_args()

    validator = AllocationRateScenarioValidation(
        args.platform,
        pathlib.Path(args.bot),
        args.scenario,
        args.profile_seconds,
    )
    code = validator.execute()
    validator.result["comparison_completed_at"] = now_iso()
    validator._write_results()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
