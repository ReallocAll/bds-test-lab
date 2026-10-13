"""Validate an exact VeinMiner CI Wheel using a real BDS process; no player is simulated."""

from __future__ import annotations

import json
import shutil
import sys
import traceback
from pathlib import Path

from controller.bstats import write_disabled_bstats_config
from controller.run_test import READY_HINTS, ServerProcess


def main() -> int:
    root = Path.cwd()
    server_dir = root / "work" / "veinminer-runtime" / "bedrock_server"
    plugins = server_dir / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    wheels = list((root / "veinminer-wheel").glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one VeinMiner wheel: {wheels}")
    wheel = wheels[0]
    shutil.copy2(wheel, plugins / wheel.name)
    write_disabled_bstats_config(server_dir)
    assert not list(server_dir.rglob("experimental.json")), "Unexpected legacy safety gate"

    from endstone_vein_miner.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS == {
        "enabled": False,
        "require_sneak": True,
        "ores": True,
        "logs": False,
        "leaves": False,
    }

    evidence: dict[str, object] = {
        "status": "running",
        "wheel": wheel.name,
        "python": sys.version,
        "checks": [],
        "limitations": "No actual player mining or native Territory/Tianyan plugin was simulated.",
    }
    log = root / "veinminer-bds.log"
    server = ServerProcess(
        [sys.executable, "-m", "endstone", "--yes", "--server-folder", str(server_dir)],
        root,
        log,
    )

    def record(name: str, detail: str = "") -> None:
        evidence["checks"].append({"name": name, "detail": detail})
        (root / "veinminer-results.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[VeinMiner] {name}: {detail}", flush=True)

    try:
        server.start()
        server.wait_for(
            lambda rows: any(any(hint in line.lower() for hint in READY_HINTS) for line in rows),
            240,
            "BDS ready",
        )
        record("bds-ready")
        server.wait_for(
            lambda rows: any("enabling vein_miner" in line.lower() for line in rows),
            50,
            "VeinMiner enabled",
        )
        server.wait_for(
            lambda rows: any("连锁挖掘功能已启用" in line for line in rows),
            30,
            "server-level mining enabled by default",
        )
        assert not any("额外连锁破坏仍禁用" in line for line in server.snapshot())
        record("server-default-enabled", "No experimental.json; server feature is active")

        def command(cmd: str, expected: str) -> None:
            cursor = server.command(cmd)
            output = "\n".join(server.wait_command_output(cursor, 18))
            if expected not in output:
                raise AssertionError(f"{cmd} missing {expected}: {output[-1500:]}")
            if not server.is_alive():
                raise RuntimeError(f"BDS exited during {cmd}")
            record(f"command-{cmd}", output[-300:])

        command("vm reload", "玩家设置已重新加载")
        command("vm on", "该指令只能由玩家使用")

        if not server.graceful_stop(timeout=70):
            raise RuntimeError(f"BDS failed clean shutdown: {server.lifecycle_diagnostic}")
        record("clean-shutdown", json.dumps(server.lifecycle_diagnostic, default=str)[-900:])
        evidence["status"] = "PASS"
        return 0
    except BaseException as exc:
        evidence["status"] = "FAIL"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["traceback"] = traceback.format_exc()
        print(evidence["traceback"], file=sys.stderr, flush=True)
        return 1
    finally:
        if server.is_alive():
            server.force_kill_tree()
        server.close()
        (root / "veinminer-results.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    raise SystemExit(main())
