#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import traceback

import providers.artifact_provider as artifact_provider
from controller.extended_validation import ExtendedIntegrationTest
from controller.run_test import now_iso, write_json

ENDSTONE_SHA = "27cc2e04d843bd70f089b0814ddba3054d4c55ef"


class RecoveryMultiSessionValidation(ExtendedIntegrationTest):
    disable_bstats = True

    def __init__(self, platform_name: str) -> None:
        super().__init__(platform_name, 30)
        self.result["test_kind"] = "spark-recovery-multi-session"
        self.result["soak"]["duration_minutes"] = 0
        write_json(self.result_path, self.result)

    def install_artifacts(self) -> None:
        original_discover = artifact_provider.discover

        def pinned_discover(component: str, platform_name: str, expected_sha: str | None = None):
            if component == "endstone":
                return original_discover(component, platform_name, expected_sha=ENDSTONE_SHA)
            return original_discover(component, platform_name, expected_sha=expected_sha)

        artifact_provider.discover = pinned_discover
        try:
            super().install_artifacts()
        finally:
            artifact_provider.discover = original_discover

    def execute_multi_session(self) -> int:
        stage = "initialization"
        try:
            stage = "artifact-discovery"
            self.install_artifacts()
            stage = "bds-start"
            self.start_server()
            assert self.server is not None

            # Complete two independent execution sessions first. The third session
            # is the controlled-crash probe inherited from ExtendedIntegrationTest.
            # This reproduces the ordering window where module definitions from
            # earlier producer activity must still be durable before later samples.
            for index in range(2):
                stage = f"completed-session-{index + 1}"
                url = self.run_profiler(allocation=False)
                if not url:
                    raise RuntimeError(f"execution session {index + 1} produced no viewer URL")
                self.record_online_profile(
                    f"execution-pre-crash-{index + 1}",
                    url,
                    12,
                    "Completed execution session before the controlled crash-recovery session.",
                )

            stage = "multi-session-crash-recovery"
            self.run_crash_recovery_probe()
            if self.result["recovery"].get("crash_replay") != "PASS":
                raise RuntimeError("multi-session crash recovery did not reach PASS")

            stage = "post-recovery-command"
            self.command_check("post-recovery-command", "spark profiler info")
            stage = "shutdown"
            self.shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            return 0
        except Exception as exc:
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
            pathlib.Path("failure-diagnostics.txt").write_text(diagnostic, encoding="utf-8")
            return 1
        finally:
            self.result["completed_at"] = now_iso()
            self.split_logs()
            write_json(self.result_path, self.result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["linux", "windows"], default="linux")
    args = parser.parse_args()
    return RecoveryMultiSessionValidation(args.platform).execute_multi_session()


if __name__ == "__main__":
    raise SystemExit(main())
