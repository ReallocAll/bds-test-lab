from __future__ import annotations

import argparse
import json
import os
from typing import Any

COMBINED_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "platform": "linux",
        "os": "ubuntu-24.04",
        "bot_name": "bds-test-bot",
        "endstone_artifact_id": "9616075557",
    },
    {
        "platform": "windows",
        "os": "windows-2022",
        "bot_name": "bds-test-bot.exe",
        "endstone_artifact_id": "9616071559",
    },
)

PYTHON_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "platform": "linux",
        "os": "ubuntu-24.04",
        "bot_name": "bds-test-bot",
        "endstone_artifact_id": "9616075557",
        "mode": "off",
        "scenario": "chunk-walk",
        "scenario_label": "mixed-actions",
        "scenario_file": "bot-src/scenarios/mixed-actions.json",
        "iterations": "12000",
        "count": 1,
    },
    {
        "platform": "linux",
        "os": "ubuntu-24.04",
        "bot_name": "bds-test-bot",
        "endstone_artifact_id": "9616075557",
        "mode": "single",
        "scenario": "chunk-walk",
        "scenario_label": "mixed-actions",
        "scenario_file": "bot-src/scenarios/mixed-actions.json",
        "iterations": "12000",
        "count": 1,
    },
    {
        "platform": "linux",
        "os": "ubuntu-24.04",
        "bot_name": "bds-test-bot",
        "endstone_artifact_id": "9616075557",
        "mode": "nested",
        "scenario": "chunk-walk",
        "scenario_label": "mixed-actions",
        "scenario_file": "bot-src/scenarios/mixed-actions.json",
        "iterations": "12000",
        "count": 1,
    },
    {
        "platform": "linux",
        "os": "ubuntu-24.04",
        "bot_name": "bds-test-bot",
        "endstone_artifact_id": "9616075557",
        "mode": "dual",
        "scenario": "chunk-walk",
        "scenario_label": "mixed-actions",
        "scenario_file": "bot-src/scenarios/mixed-actions.json",
        "iterations": "12000",
        "count": 1,
    },
    {
        "platform": "linux",
        "os": "ubuntu-24.04",
        "bot_name": "bds-test-bot",
        "endstone_artifact_id": "9616075557",
        "mode": "mixed",
        "scenario": "chunk-walk",
        "scenario_label": "mixed-actions",
        "scenario_file": "bot-src/scenarios/mixed-actions.json",
        "iterations": "12000",
        "count": 1,
    },
    {
        "platform": "linux",
        "os": "ubuntu-24.04",
        "bot_name": "bds-test-bot",
        "endstone_artifact_id": "9616075557",
        "mode": "fleet",
        "scenario": "chunk-walk",
        "scenario_label": "mixed-actions",
        "scenario_file": "bot-src/scenarios/mixed-actions.json",
        "iterations": "12000",
        "count": 5,
    },
    {
        "platform": "linux",
        "os": "ubuntu-24.04",
        "bot_name": "bds-test-bot",
        "endstone_artifact_id": "9616075557",
        "mode": "single",
        "scenario": "chunk-walk",
        "scenario_label": "chunk-walk",
        "scenario_file": "",
        "iterations": "12000",
        "count": 1,
    },
    {
        "platform": "linux",
        "os": "ubuntu-24.04",
        "bot_name": "bds-test-bot",
        "endstone_artifact_id": "9616075557",
        "mode": "nested",
        "scenario": "chunk-fly",
        "scenario_label": "chunk-fly",
        "scenario_file": "",
        "iterations": "12000",
        "count": 1,
    },
    {
        "platform": "windows",
        "os": "windows-2022",
        "bot_name": "bds-test-bot.exe",
        "endstone_artifact_id": "9616071559",
        "mode": "single",
        "scenario": "chunk-walk",
        "scenario_label": "mixed-actions",
        "scenario_file": "bot-src/scenarios/mixed-actions.json",
        "iterations": "48000",
        "count": 1,
    },
    {
        "platform": "windows",
        "os": "windows-2022",
        "bot_name": "bds-test-bot.exe",
        "endstone_artifact_id": "9616071559",
        "mode": "dual",
        "scenario": "chunk-walk",
        "scenario_label": "mixed-actions",
        "scenario_file": "bot-src/scenarios/mixed-actions.json",
        "iterations": "48000",
        "count": 1,
    },
    {
        "platform": "windows",
        "os": "windows-2022",
        "bot_name": "bds-test-bot.exe",
        "endstone_artifact_id": "9616071559",
        "mode": "fleet",
        "scenario": "chunk-walk",
        "scenario_label": "mixed-actions",
        "scenario_file": "bot-src/scenarios/mixed-actions.json",
        "iterations": "48000",
        "count": 5,
    },
)

_TARGETS = {
    "combined": {"all", "windows"},
    "python": {"all", "windows-dual", "windows-fleet"},
}


def resolve_matrix(workflow: str, event_name: str, target: str | None) -> dict[str, list[dict[str, Any]]]:
    allowed = _TARGETS.get(workflow)
    if allowed is None:
        raise ValueError(f"unsupported workflow: {workflow}")
    event = event_name.strip()
    selected = (target or "").strip() or "all"
    if event not in {"push", "workflow_dispatch"}:
        raise ValueError(f"unsupported GitHub event: {event}")
    if selected not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"target must be {choices}")
    if event == "push" and selected != "all":
        raise ValueError("push events must use the complete matrix")

    rows = list(COMBINED_MATRIX if workflow == "combined" else PYTHON_MATRIX)
    if selected == "windows":
        rows = [row for row in rows if row["platform"] == "windows"]
    elif selected == "windows-dual":
        rows = [row for row in rows if row["platform"] == "windows" and row.get("mode") == "dual"]
    elif selected == "windows-fleet":
        rows = [row for row in rows if row["platform"] == "windows" and row.get("mode") == "fleet"]
    if not rows:
        raise ValueError(f"target selected no matrix entries: {selected}")
    return {"include": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", required=True, choices=sorted(_TARGETS))
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--target", default="")
    args = parser.parse_args()
    try:
        matrix = resolve_matrix(args.workflow, args.event_name, args.target)
    except ValueError as exc:
        parser.error(str(exc))
    encoded = json.dumps(matrix, separators=(",", ":"), sort_keys=True)
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        print(encoded)
        return 0
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"matrix={encoded}\n")
    print(f"resolved {args.workflow} target {args.target or 'all'}: {len(matrix['include'])} matrix entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
