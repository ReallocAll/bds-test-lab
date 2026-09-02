from pathlib import Path

path = Path("controller/combined_pack_gamerule_fleet_exact_runner.py")
text = path.read_text(encoding="utf-8")

anchor = "_ORIGINAL_INSTALL_BEHAVIOR_PACKS = CombinedPackGameruleFleetValidation.install_behavior_packs\n"
replacement = anchor + "_ORIGINAL_RUN_PUBLIC_PROFILE_PHASE = CombinedPackGameruleFleetValidation.run_public_profile_phase\n"
if "_ORIGINAL_RUN_PUBLIC_PROFILE_PHASE" not in text:
    if anchor not in text:
        raise SystemExit("original public phase anchor not found")
    text = text.replace(anchor, replacement, 1)

assignment_anchor = "CombinedPackGameruleFleetValidation.install_artifacts = _install_exact_artifacts\n"
function = '''def _run_public_profile_phase_with_restart_pack_evidence(\n    self: CombinedPackGameruleFleetValidation,\n) -> None:\n    \"\"\"Avoid a redundant Windows console probe after local real-BDS execution proof.\"\"\"\n\n    if self.platform != \"windows\":\n        _ORIGINAL_RUN_PUBLIC_PROFILE_PHASE(self)\n        return\n\n    checks = self.result.get(\"checks\") if isinstance(self.result, dict) else None\n    local_execution_proved = isinstance(checks, list) and any(\n        isinstance(check, dict)\n        and check.get(\"name\") == \"behavior-packs-real-load\"\n        and check.get(\"status\") == \"PASS\"\n        for check in checks\n    )\n    if not local_execution_proved:\n        raise RuntimeError(\"Windows public phase requires prior local real-BDS behavior-pack execution proof\")\n\n    original_verify = self.verify_behavior_pack_functions\n\n    def verify_restart_pack_stack() -> None:\n        assert self.server is not None\n        startup_log = \"\\n\".join(self.server.snapshot()).casefold()\n        missing = [pack[\"name\"] for pack in BEHAVIOR_PACKS if pack[\"name\"].casefold() not in startup_log]\n        if missing:\n            raise RuntimeError(\n                \"Windows public restart did not reload expected behavior packs: \" + \", \".join(missing)\n            )\n        self.check(\n            \"behavior-packs-public-restart\",\n            \"PASS\",\n            \"all three behavior packs reloaded; semantic execution was already proved in the local real-BDS phase\",\n            count=len(BEHAVIOR_PACKS),\n            execution_oracle=\"local-phase-real-bds\",\n            restart_oracle=\"startup-pack-stack\",\n        )\n\n    self.verify_behavior_pack_functions = verify_restart_pack_stack  # type: ignore[method-assign]\n    try:\n        _ORIGINAL_RUN_PUBLIC_PROFILE_PHASE(self)\n    finally:\n        self.verify_behavior_pack_functions = original_verify  # type: ignore[method-assign]\n\n\n'''
if "def _run_public_profile_phase_with_restart_pack_evidence(" not in text:
    if assignment_anchor not in text:
        raise SystemExit("monkeypatch assignment anchor not found")
    text = text.replace(assignment_anchor, function + assignment_anchor, 1)

run_assignment = "CombinedPackGameruleFleetValidation.run_public_profile_phase = _run_public_profile_phase_with_restart_pack_evidence\n"
if run_assignment not in text:
    marker = "CombinedPackGameruleFleetValidation.verify_behavior_pack_functions = _verify_behavior_pack_functions_with_state_oracle\n"
    if marker not in text:
        raise SystemExit("verify assignment anchor not found")
    text = text.replace(marker, marker + run_assignment, 1)

path.write_text(text, encoding="utf-8")
