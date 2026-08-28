#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Any

from controller.cross_platform_fleet_validation import CrossPlatformFleetBotProcess
from controller.fleet_spark_validation import PLAYER_COUNT_RE, set_server_property
from controller.bot_validation import list_players, patch_server_properties
from controller.python_profile_payload import (
    contains_python_chain,
    fetch_viewer_payload,
    parse_sampler_data,
    profile_summary,
    python_nodes,
)
from controller.run_test import IntegrationTest, run_checked, write_json


PLUGIN_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "spark-python-hotspot-test"
EXPECTED_MODULE = "endstone_spark_python_hotspot_test"
EXPECTED_SOURCE = "spark-python-hotspot-test"


def _metadata_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, str) else value
    except json.JSONDecodeError:
        return value


class PythonAttributionValidation(IntegrationTest):
    def __init__(
        self,
        platform_name: str,
        bot_binary: pathlib.Path | None,
        count: int,
        scenario: str,
        mode: str,
        profile_seconds: int,
    ):
        super().__init__(platform_name)
        self.bot_binary = bot_binary.resolve() if bot_binary else None
        self.count = count
        self.scenario = scenario
        self.mode = mode
        self.profile_seconds = max(60, profile_seconds)
        self.bot: CrossPlatformFleetBotProcess | None = None
        self.bot_log = self.root / f"python-attribution-bots-{platform_name}-{count}-{scenario}.log"
        self.evidence_path = self.root / "python-attribution-result.json"
        self.raw_profile_path = self.root / "python-attribution.sparkprofile"
        self.summary_path = self.root / "python-attribution-profile-summary.json"
        self.result.update(
            {
                "test_kind": "spark-python-function-attribution",
                "python_version": sys.version.split()[0],
                "hotspot_mode": mode,
                "bot_count": count,
                "bot_scenario": scenario,
                "profile_seconds": self.profile_seconds,
                "spark_profile_viewer_url": None,
                "profile_summary": None,
            }
        )
        self._write_results()

    def _write_results(self) -> None:
        write_json(self.result_path, self.result)
        write_json(self.evidence_path, self.result)

    def check(self, name: str, status: str, detail: str | None = None, **extra: Any) -> None:
        super().check(name, status, detail, **extra)
        self._write_results()

    def install_artifacts(self) -> None:
        super().install_artifacts()
        wheel_dir = self.root / "hotspot-wheel"
        shutil.rmtree(wheel_dir, ignore_errors=True)
        wheel_dir.mkdir(parents=True, exist_ok=True)
        run_checked(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--disable-pip-version-check",
                "--no-deps",
                "--wheel-dir",
                str(wheel_dir),
                str(PLUGIN_SOURCE),
            ],
            timeout=180,
        )
        wheels = sorted(wheel_dir.glob("endstone_spark_python_hotspot_test-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected one hotspot plugin wheel, got: {wheels}")
        plugin_dir = self.server_dir / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        target = plugin_dir / wheels[0].name
        shutil.copy2(wheels[0], target)
        self.check("python-hotspot-plugin-installed", "PASS", str(target.relative_to(self.root)))

    def wait_plugin(self) -> None:
        assert self.server is not None
        self.server.wait_for(
            lambda lines: any("spark python hotspot test enabled" in line.lower() for line in lines),
            30,
            "Python hotspot plugin enable",
        )
        self.check("python-hotspot-plugin-enabled", "PASS", f"mode={self.mode}")

    def bootstrap_server(self) -> None:
        self.start_server()
        self.wait_plugin()
        assert self.server is not None
        if not self.server.graceful_stop(60):
            self.server.force_kill_tree()
            raise RuntimeError("BDS did not stop after initial server.properties bootstrap")
        self.server.close()
        self.server = None
        properties = self.server_dir / "server.properties"
        patch_server_properties(properties)
        set_server_property(properties, "max-players", "30")
        self.start_server()
        self.wait_plugin()
        self.command_check("world-difficulty", "difficulty normal")
        self.command_check("world-mob-spawning", "gamerule doMobSpawning true")
        self.command_check("world-random-tick", "gamerule randomTickSpeed 1")

    def expected_names(self) -> list[str]:
        if self.count == 1:
            return ["TestBot"]
        width = max(2, len(str(self.count)))
        return [f"TestBot-{index:0{width}d}" for index in range(1, self.count + 1)]

    def wait_player_count(self, expected: int, timeout: float = 45.0) -> list[str]:
        assert self.server is not None
        deadline = time.monotonic() + timeout
        last: list[str] = []
        while time.monotonic() < deadline:
            last = list_players(self.server)
            for line in last:
                match = PLAYER_COUNT_RE.search(line)
                if match and int(match.group(1)) == expected:
                    return last
            time.sleep(0.5)
        raise RuntimeError(f"Expected {expected} online players, last output: {' | '.join(last[-20:])}")

    def start_bots(self) -> None:
        if self.count == 0:
            return
        if self.bot_binary is None:
            raise RuntimeError("bot binary is required when bot_count > 0")
        self.bot = CrossPlatformFleetBotProcess(self.bot_binary, self.bot_log, self.count, self.scenario)
        self.bot.start()
        event = self.bot.wait_event("fleet_online", max(90.0, self.count * 5.0))
        if int(event.get("online", -1)) != self.count:
            raise RuntimeError(f"invalid fleet_online event: {event}")
        output = self.wait_player_count(self.count)
        joined = "\n".join(output).lower()
        missing = [name for name in self.expected_names() if name.lower() not in joined]
        if missing:
            raise RuntimeError(f"players missing from BDS list: {missing}")
        self.check("bots-online", "PASS", count=self.count, scenario=self.scenario)

    def stop_bots(self) -> None:
        if self.bot is None:
            return
        code = self.bot.terminate(20)
        if code != 0:
            raise RuntimeError(f"bot fleet exited with code {code}")
        events = self.bot.event_snapshot()
        shutdown = next((e for e in reversed(events) if e.get("event") == "fleet_shutdown"), None)
        if not shutdown or shutdown.get("graceful_shutdown") is not True:
            raise RuntimeError(f"missing graceful fleet shutdown: {shutdown}")
        self.wait_player_count(0, 30)
        self.check("bots-shutdown", "PASS", shutdown=shutdown)
        self.bot = None

    def run_profile(self) -> str:
        assert self.server is not None
        command = f"spark profiler start --thread * --interval 4 --timeout {self.profile_seconds}"
        start = self.server.command(command)
        deadline = time.monotonic() + self.profile_seconds + 90
        url: str | None = None
        while time.monotonic() < deadline:
            url = self._viewer_url(self.server.snapshot(), start)
            if url:
                break
            if not self.server.is_alive():
                raise RuntimeError("BDS exited during Python attribution profile")
            time.sleep(1)
        if not url:
            stop_at = self.server.command("spark profiler stop")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                url = self._viewer_url(self.server.snapshot(), min(start, stop_at))
                if url:
                    break
                if not self.server.is_alive():
                    raise RuntimeError("BDS exited while finalizing Python attribution profile")
                time.sleep(1)
        if not url:
            raise RuntimeError("Python attribution profiler produced no Spark viewer URL")
        self.result["spark_profile_viewer_url"] = url
        self._write_results()
        return url

    @staticmethod
    def _node_method(node: dict[str, Any]) -> str:
        value = node.get("method")
        return value if isinstance(value, str) else ""

    def validate_payload(self, url: str) -> None:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                response.read(64)
        except Exception as exc:
            raise RuntimeError(f"Spark viewer URL is not openable: {url}: {exc}") from exc
        self.check("viewer-open", "PASS", viewer_url=url)

        raw = fetch_viewer_payload(url)
        if len(raw) < 64:
            raise RuntimeError(f"raw Spark payload is unexpectedly small: {len(raw)} bytes")
        self.raw_profile_path.write_bytes(raw)
        self.check("raw-payload-nonempty", "PASS", bytes=len(raw))

        profile = parse_sampler_data(raw)
        summary = profile_summary(profile)
        metadata = profile["metadata"]
        duration = (metadata["end_time_ms"] - metadata["start_time_ms"]) / 1000.0
        if metadata["mode"] != 0:
            raise RuntimeError(f"expected execution profile mode=0, got {metadata['mode']}")
        if duration < max(50.0, self.profile_seconds - 10.0):
            raise RuntimeError(f"profile duration too short: {duration:.3f}s")
        if not profile["threads"] or summary["root_weight"] <= 0:
            raise RuntimeError("profile thread tree is empty or has zero root weight")
        self.check(
            "profile-shape",
            "PASS",
            duration_seconds=round(duration, 3),
            thread_count=len(profile["threads"]),
            root_weight=summary["root_weight"],
        )

        extra = metadata["extra"]
        backend = _metadata_text(extra.get("Python attribution backend"))
        python_version = _metadata_text(extra.get("Python version"))
        if backend != "PEP669":
            raise RuntimeError(f"unexpected Python attribution backend: {backend!r}")
        if not python_version.startswith("3.13"):
            raise RuntimeError(f"unexpected embedded Python version: {python_version!r}")
        if extra.get("Python function attribution enabled") != "true":
            raise RuntimeError("Python function attribution is not enabled in profile diagnostics")
        if int(extra.get("Python PY_START events", "0")) <= 0:
            raise RuntimeError("profile recorded no PY_START events")
        if int(extra.get("Python shadow snapshot attempts", "0")) <= 0:
            raise RuntimeError("profile recorded no Python shadow snapshot attempts")
        self.check("pep669-diagnostics", "PASS", backend=backend, python_version=python_version)

        nodes = python_nodes(profile)
        if not nodes:
            raise RuntimeError("profile contains no [Python] function nodes")
        plugin_nodes = [node for node in nodes if node.get("class") == f"[Python] {EXPECTED_MODULE}"]
        if not plugin_nodes:
            raise RuntimeError(f"profile contains no Python nodes for module {EXPECTED_MODULE}")
        if not any(EXPECTED_MODULE in str(node.get("descriptor", "")) for node in plugin_nodes):
            raise RuntimeError("plugin Python nodes do not carry source file attribution")
        if not all(isinstance(node.get("line"), int) and int(node["line"]) > 0 for node in plugin_nodes):
            raise RuntimeError("plugin Python nodes do not carry reliable first-line metadata")
        source = profile["class_sources"].get(f"[Python] {EXPECTED_MODULE}")
        if source != EXPECTED_SOURCE:
            raise RuntimeError(f"plugin class source mismatch: expected {EXPECTED_SOURCE!r}, got {source!r}")
        self.check("plugin-function-metadata", "PASS", source=source, plugin_nodes=len(plugin_nodes))

        methods = {self._node_method(node) for node in plugin_nodes}
        expected_for_mode = {
            "off": {"HotspotPlugin.light_tick"},
            "single": {"HotspotPlugin.light_tick", "HotspotPlugin.cpu_hotspot", "HotspotPlugin.integer_hash_loop"},
            "nested": {
                "HotspotPlugin.light_tick",
                "HotspotPlugin.nested_hotspot",
                "HotspotPlugin.level_one",
                "HotspotPlugin.level_two",
                "HotspotPlugin.level_three",
                "HotspotPlugin.cpu_leaf",
            },
            "dual": {"HotspotPlugin.dual_hotspot", "HotspotPlugin.hotspot_a", "HotspotPlugin.hotspot_b"},
            "mixed": {
                "HotspotPlugin.stdlib_hotspot",
                "HotspotPlugin.exception_hotspot",
                "HotspotPlugin.generator_hotspot",
                "HotspotPlugin.async_hotspot",
                "HotspotPlugin.worker_thread_hotspot",
            },
            "fleet": {"HotspotPlugin.event_callback_hotspot", "HotspotPlugin.worker_thread_hotspot"},
        }[self.mode]
        missing = sorted(expected_for_mode - methods)
        if missing:
            raise RuntimeError(f"expected Python hotspot methods missing from profile: {missing}")

        if self.mode == "nested":
            chain = [
                "HotspotPlugin.light_tick",
                "HotspotPlugin.nested_hotspot",
                "HotspotPlugin.level_one",
                "HotspotPlugin.level_two",
                "HotspotPlugin.level_three",
                "HotspotPlugin.cpu_leaf",
            ]
            if not contains_python_chain(profile, chain):
                raise RuntimeError("nested Python parent/child chain was not preserved in the real profile")
            self.check("nested-call-tree", "PASS", chain=chain)

        if self.mode == "dual":
            weights = summary["python_methods_ms"]
            a = float(weights.get("HotspotPlugin.hotspot_a", 0.0))
            b = float(weights.get("HotspotPlugin.hotspot_b", 0.0))
            if a <= 0 or b <= 0 or a <= b:
                raise RuntimeError(f"70/30 hotspot direction not observed: hotspot_a={a}, hotspot_b={b}")
            self.check("dual-hotspot-direction", "PASS", hotspot_a_ms=a, hotspot_b_ms=b)

        if self.mode in {"mixed", "fleet"}:
            if summary["python_threads"] < 2:
                raise RuntimeError(f"worker-thread Python attribution missing: python_threads={summary['python_threads']}")
            self.check("python-worker-thread", "PASS", python_threads=summary["python_threads"])

        self.result["profile_summary"] = summary
        self.summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        self._write_results()

    def execute(self) -> int:
        stage = "initialization"
        try:
            os.environ["SPARK_PYTHON_HOTSPOT_MODE"] = self.mode
            os.environ["SPARK_PYTHON_HOTSPOT_ITERATIONS"] = "12000"
            stage = "artifact-install"
            self.install_artifacts()
            stage = "server-bootstrap"
            self.bootstrap_server()
            stage = "basic-commands"
            self.run_basic_commands()
            stage = "bots-connect"
            self.start_bots()
            time.sleep(15)
            stage = "execution-profile"
            url = self.run_profile()
            stage = "payload-validation"
            self.validate_payload(url)
            stage = "bots-disconnect"
            self.stop_bots()
            stage = "shutdown"
            self.shutdown()
            self.result["status"] = "PASS"
            self.result["state"] = "completed"
            self.result["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._write_results()
            return 0
        except Exception as exc:
            self.result["status"] = "FAIL"
            self.result["state"] = "failed"
            self.result["failed_stage"] = stage
            self.result["error_summary"] = f"{type(exc).__name__}: {exc}"
            self._write_results()
            print(f"VALIDATION FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            try:
                self.stop_bots()
            except Exception:
                pass
            try:
                self.shutdown()
            except Exception:
                pass
            return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--bot", default=None)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--scenario", default="chunk-walk")
    parser.add_argument("--mode", required=True, choices=["off", "single", "nested", "dual", "mixed", "fleet"])
    parser.add_argument("--profile-seconds", type=int, default=60)
    args = parser.parse_args()
    validator = PythonAttributionValidation(
        args.platform,
        pathlib.Path(args.bot) if args.bot else None,
        args.count,
        args.scenario,
        args.mode,
        args.profile_seconds,
    )
    code = validator.execute()
    print(json.dumps(validator.result, indent=2, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
