#!/usr/bin/env python3
"""Headless BDS test controller entry point."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def write_result(**kwargs: object) -> None:
    result = {
        "platform": kwargs.get("platform", ""),
        "bds_version": kwargs.get("bds_version", ""),
        "endstone_sha": kwargs.get("endstone_sha", ""),
        "spark_sha": kwargs.get("spark_sha", ""),
        "server_ready": False,
        "plugin_loaded": False,
        "execution_profile": {},
        "allocation_profile": {},
        "recovery": {},
        "shutdown": {},
    }
    (ROOT / "test-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    write_result()
