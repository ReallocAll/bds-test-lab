from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

from endstone.plugin import Plugin


class ElfRescanProbe(Plugin):
    """Run the production ElfImportHooks scan path inside the live BDS process."""

    api_version = "0.11"

    def on_enable(self) -> None:
        raw_path = os.environ.get("SPARK_ELF_RESCAN_HELPER", "").strip()
        if not raw_path:
            raise RuntimeError("SPARK_ELF_RESCAN_HELPER is required")
        helper_path = Path(raw_path).resolve()
        if not helper_path.is_file():
            raise RuntimeError(f"ELF rescan helper is missing: {helper_path}")
        helper = ctypes.CDLL(str(helper_path))
        run = helper.sparkElfRescanProbeRun
        run.argtypes = [ctypes.c_uint, ctypes.c_uint]
        run.restype = ctypes.c_char_p
        payload = run(3, 31)
        if payload is None:
            raise RuntimeError("ELF rescan helper returned null")
        decoded = payload.decode("utf-8", errors="strict")
        evidence = json.loads(decoded)
        if evidence.get("status") != "PASS":
            raise RuntimeError(f"ELF rescan helper failed: {evidence}")
        self.logger.info("SPARK_ELF_RESCAN_RESULT " + json.dumps(evidence, sort_keys=True, separators=(",", ":")))
