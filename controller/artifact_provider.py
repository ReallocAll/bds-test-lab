#!/usr/bin/env python3
"""Artifact provider abstraction for CI-produced binaries."""

from __future__ import annotations

import os


class ArtifactProvider:
    def __init__(self) -> None:
        self.token = os.environ.get("REPO_PAT", "")

    def describe(self) -> dict[str, str]:
        return {
            "spark_source": "ReallocAll/spark",
            "requires_token": "true",
            "token_available": str(bool(self.token)),
        }
