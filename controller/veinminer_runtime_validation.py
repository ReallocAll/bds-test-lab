"""Validate an exact VeinMiner CI Wheel using a real BDS process; no player is simulated."""

from __future__ import annotations

import ast
import json
import zipfile
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

    with zipfile.ZipFile(wheel) as archive:
        source = archive.read("endstone_vein_miner/settings.py").decode("utf-8")
        plugin_source = archive.read("endstone_vein_miner/plugin.py").decode("utf-8")
        menu_source = archive.read("endstone_vein_miner/gui.py").decode("utf-8")
        assert "先开启总开关及所需连锁类型" in menu_source
        assert "默认潜行触发，可关闭" in menu_source
        assert "树叶连锁（斧/剪刀）" in menu_source
        assert "农作物；挖上层保留根部。" in menu_source
        mining_source = archive.read("endstone_vein_miner/mining.py").decode("utf-8")
        assert "find_vertical_plant" in mining_source
        assert "queue.append((nx, nz, *bounds))" in mining_source
        assert "harvest_roots = sy == origin[0]" in mining_source
        assert "TianyanAudit" not in plugin_source
        assert "self._audit" not in plugin_source
        assert "ServerLimits" in plugin_source
    assignments = [
        node for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DEFAULT_SETTINGS" for target in node.targets)
    ]
    assert len(assignments) == 1
    default_settings = ast.literal_eval(assignments[0].value)
    assert default_settings == {
        "enabled": False,
        "require_sneak": True,
        "ores": True,
        "logs": False,
        "leaves": False,
        "plants": False,
    }

    evidence: dict[str, object] = {
        "status": "running",
        "wheel": wheel.name,
        "python": sys.version,
        "checks": [],
        "limitations": "No actual touch-player mining or native Territory integration was simulated.",
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
        startup_lines = server.snapshot()
        assert sum("loading vein_miner" in line.lower() for line in startup_lines) == 1
        assert sum("enabling vein_miner" in line.lower() for line in startup_lines) == 1
        assert any("enabling vein_miner v2.1.3" in line.lower() for line in startup_lines)
        assert not any(
            "ambiguous plugin name" in line.lower()
            or ("failed to load" in line.lower() and "vein" in line.lower())
            or "连锁挖掘功能已启用" in line
            or "额外连锁破坏仍禁用" in line
            for line in startup_lines
        )
        record("concise-startup", "No redundant startup description or legacy safety-gate message")

        def command(cmd: str, expected: str) -> None:
            cursor = server.command(cmd)
            output = "\n".join(server.wait_command_output(cursor, 18))
            if expected not in output:
                raise AssertionError(f"{cmd} missing {expected}: {output[-1500:]}")
            if not server.is_alive():
                raise RuntimeError(f"BDS exited during {cmd}")
            record(f"command-{cmd}", output[-300:])

        command("vm reload", "设置已重载")
        command("vm on", "仅限玩家使用")

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
