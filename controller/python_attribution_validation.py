#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from controller.bot_validation import list_players, patch_server_properties
from controller.cross_platform_fleet_validation import CrossPlatformFleetBotProcess
from controller.fleet_spark_validation import PLAYER_COUNT_RE, set_server_property
from controller.python_profile_payload import (
    contains_python_chain,
    fetch_viewer_payload,
    parse_sampler_data,
    profile_summary,
    python_nodes,
)
from controller.run_test import (
    READY_HINTS,
    SPARK_LOAD_HINTS,
    IntegrationTest,
    ServerProcess,
    run_checked,
    write_json,
)


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

    def start_server(self) -> None:
        """Use a larger startup budget only for the slow Windows validation runners."""
        cmd = [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(self.server_dir)]
        self.server = ServerProcess(cmd, self.root, self.log_path)
        self.server.start()
        ready_timeout = 420 if self.platform == "windows" else 240
        self.server.wait_for(
            lambda lines: any(any(hint in line.lower() for hint in READY_HINTS) for line in lines),
            ready_timeout,
            "BDS ready",
        )
        self.check("bds-start", "PASS")
        self.check("ready", "PASS")
        self.server.wait_for(
            lambda lines: any(
                "spark" in line.lower() and any(hint in line.lower() for hint in SPARK_LOAD_HINTS) for line in lines
            ),
            30,
            "Spark enable",
        )
        self.check("spark-load-enable", "PASS")
        version_file = self.server_dir / "version.txt"
        if version_file.exists():
            self.result["bds_version"] = version_file.read_text(encoding="utf-8").strip()
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
        shutdown = next((event for event in reversed(events) if event.get("event") == "fleet_shutdown"), None)
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
        if url is None:
            stop_at = self.server.command("spark profiler stop")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                url = self._viewer_url(self.server.snapshot(), min(start, stop_at))
                if url:
                    break
                if not self.server.is_alive():
                    raise RuntimeError("BDS exited while finalizing Python attribution profile")
                time.sleep(1)
        if not url:
            raise RuntimeError("Python attribution profile produced no viewer URL")
        self.result["spark_profile_viewer_url"] = url
        self._write_results()
        return url

    def record_viewer_frontend_status(self, url: str) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "bds-test-lab/python-attribution-validation"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read(4096)
                if response.status == 200 and body:
                    self.check("viewer-frontend-open", "PASS", viewer_url=url, http_status=response.status)
                    return
                self.check(
                    "viewer-frontend-open",
                    "WARN",
                    f"viewer frontend returned HTTP {response.status} or an empty body; raw payload remains authoritative",
                    viewer_url=url,
                    http_status=response.status,
                )
        except urllib.error.HTTPError as exc:
            self.check(
                "viewer-frontend-open",
                "WARN",
                f"viewer frontend probe returned HTTP {exc.code} from the CI runner; raw payload remains authoritative",
                viewer_url=url,
                http_status=exc.code,
            )
        except Exception as exc:
            self.check(
                "viewer-frontend-open",
                "WARN",
                f"viewer frontend probe failed from the CI runner: {type(exc).__name__}: {exc}",
                viewer_url=url,
            )

    def validate_profile(self, url: str) -> dict[str, object]:
        self.check("viewer-url-emitted", "PASS", viewer_url=url)
        raw = fetch_viewer_payload(url)
        if len(raw) < 64:
            raise RuntimeError(f"raw Spark payload is unexpectedly small: {len(raw)} bytes")
        self.raw_profile_path.write_bytes(raw)
        self.check("raw-payload-open", "PASS", bytes=len(raw), viewer_url=url)
        self.record_viewer_frontend_status(url)

        profile = parse_sampler_data(raw)
        summary = profile_summary(profile)
        self.summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

        if profile.sampler_mode != 0:
            raise RuntimeError(f"expected execution sampler mode 0, got {profile.sampler_mode}")
        if profile.duration_seconds < self.profile_seconds - 5:
            raise RuntimeError(
                f"profile too short: {profile.duration_seconds:.3f}s for requested {self.profile_seconds}s"
            )
        root_weight = sum(thread.weight for thread in profile.threads)
        if not profile.threads or root_weight <= 0:
            raise RuntimeError("profile thread tree is empty or has zero root weight")
        self.check(
            "profile-shape",
            "PASS",
            duration_seconds=profile.duration_seconds,
            thread_count=len(profile.threads),
            root_weight=root_weight,
        )

        diagnostics = profile.extra_metadata
        version = _metadata_text(diagnostics.get("Python version"))
        backend = _metadata_text(diagnostics.get("Python attribution backend"))
        enabled = diagnostics.get("Python function attribution enabled", "false")
        if sys.version_info >= (3, 12):
            if backend != "PEP669" or enabled != "true":
                raise RuntimeError(f"PEP669 attribution not enabled: backend={backend!r} enabled={enabled!r}")
            if self.mode == "off":
                self.check(
                    "python-off-baseline",
                    "PASS",
                    python_version=version,
                    backend=backend,
                    py_start_events=int(diagnostics.get("Python PY_START events", "0")),
                    snapshot_attempts=int(diagnostics.get("Python shadow snapshot attempts", "0")),
                )
                return summary
            if int(diagnostics.get("Python PY_START events", "0")) <= 0:
                raise RuntimeError("PEP669 diagnostics report no PY_START events")
            if int(diagnostics.get("Python shadow snapshot attempts", "0")) <= 0:
                raise RuntimeError("Python shadow stack was never sampled")
        else:
            reason = _metadata_text(diagnostics.get("Python attribution unavailable reason"))
            if backend != "native-only" or enabled != "false" or "3.12" not in reason:
                raise RuntimeError(
                    f"invalid Python 3.11 fallback: backend={backend!r} enabled={enabled!r} reason={reason!r}"
                )
            self.check("python-311-native-only-fallback", "PASS", backend=backend, reason=reason)
            return summary
        self.check("pep669-diagnostics", "PASS", python_version=version, backend=backend)

        nodes = python_nodes(profile)
        if not nodes:
            raise RuntimeError("no Python nodes were emitted into the real profile tree")
        plugin_nodes = [
            node
            for _thread, node in nodes
            if node.class_name == f"[Python] {EXPECTED_MODULE}" and EXPECTED_MODULE in node.method_desc
        ]
        if not plugin_nodes:
            raise RuntimeError("profile contains Python nodes but no hotspot plugin module/file attribution")
        if not any(node.line_number > 0 for node in plugin_nodes):
            raise RuntimeError("hotspot plugin Python nodes are missing co_firstlineno")
        source = profile.class_sources.get(f"[Python] {EXPECTED_MODULE}")
        if source != EXPECTED_SOURCE:
            raise RuntimeError(f"plugin class source mismatch: expected {EXPECTED_SOURCE!r}, got {source!r}")
        self.check("python-plugin-metadata", "PASS", source=source, plugin_nodes=len(plugin_nodes))

        chains: list[list[str]] = []
        if self.mode == "single":
            chains = [["HotspotPlugin.light_tick", "HotspotPlugin.cpu_hotspot", "HotspotPlugin.integer_hash_loop"]]
        elif self.mode == "nested":
            chains = [[
                "HotspotPlugin.light_tick",
                "HotspotPlugin.nested_hotspot",
                "HotspotPlugin.level_one",
                "HotspotPlugin.level_two",
                "HotspotPlugin.level_three",
                "HotspotPlugin.cpu_leaf",
            ]]
        elif self.mode == "dual":
            chains = [
                [
                    "HotspotPlugin.light_tick",
                    "HotspotPlugin.dual_hotspot",
                    "HotspotPlugin.hotspot_a",
                    "HotspotPlugin.integer_hash_loop",
                ],
                [
                    "HotspotPlugin.light_tick",
                    "HotspotPlugin.dual_hotspot",
                    "HotspotPlugin.hotspot_b",
                    "HotspotPlugin.integer_hash_loop",
                ],
            ]
        elif self.mode in {"mixed", "fleet"}:
            chains = [[
                "HotspotPlugin.light_tick",
                "HotspotPlugin.nested_hotspot",
                "HotspotPlugin.level_one",
                "HotspotPlugin.level_two",
                "HotspotPlugin.level_three",
                "HotspotPlugin.cpu_leaf",
            ]]
            required = {
                "HotspotPlugin.exception_hotspot",
                "HotspotPlugin.generator_hotspot",
                "HotspotPlugin.async_hotspot",
                "HotspotPlugin._async_leaf",
            }
            present = {node.method_name for _thread, node in nodes}
            missing = sorted(required - present)
            if missing:
                raise RuntimeError(f"mixed lifecycle workload missing Python nodes: {missing}")
        for chain in chains:
            if not contains_python_chain(profile, chain):
                raise RuntimeError("missing Python parent/child chain: " + " -> ".join(chain))
            self.check("python-chain", "PASS", chain=" -> ".join(chain))

        if self.mode == "dual":
            weights: dict[str, float] = {}
            for _thread, node in nodes:
                weights[node.method_name] = weights.get(node.method_name, 0.0) + node.weight
            hot_a = weights.get("HotspotPlugin.hotspot_a", 0.0)
            hot_b = weights.get("HotspotPlugin.hotspot_b", 0.0)
            if hot_a <= hot_b or hot_a <= 0 or hot_b <= 0:
                raise RuntimeError(f"dual hotspot direction is wrong: hotspot_a={hot_a}, hotspot_b={hot_b}")
            self.check("dual-hotspot-direction", "PASS", hotspot_a_ms=hot_a, hotspot_b_ms=hot_b)

        if self.mode in {"worker", "mixed", "fleet"}:
            worker_threads = {
                thread for thread, node in nodes if node.method_name == "HotspotPlugin.worker_thread_hotspot"
            }
            if not worker_threads:
                raise RuntimeError("worker-thread Python attribution was not observed")
            self.check("python-worker-thread", "PASS", threads=sorted(worker_threads))

        return summary

    def execute(self) -> int:
        stage = "initialization"
        try:
            os.environ["SPARK_PYTHON_HOTSPOT_MODE"] = self.mode
            os.environ.setdefault("SPARK_PYTHON_HOTSPOT_ITERATIONS", "12000")
            stage = "artifact-install"
            self.install_artifacts()
            stage = "server-bootstrap"
            self.bootstrap_server()
            stage = "spark-sanity"
            self.run_basic_commands()
            stage = "bots-connect"
            self.start_bots()
            if self.count:
                time.sleep(15)
            stage = "profile"
            url = self.run_profile()
            stage = "payload-validation"
            summary = self.validate_profile(url)
            self.result["profile_summary"] = summary
            self._write_results()
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
            print(f"VALIDATION FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
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
    parser.add_argument("--bot")
    parser.add_argument("--count", type=int, default=0, choices=[0, 1, 5])
    parser.add_argument("--scenario", default="chunk-walk", choices=["idle", "chunk-walk", "chunk-fly"])
    parser.add_argument("--mode", required=True, choices=["off", "single", "nested", "dual", "mixed", "worker", "fleet"])
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
