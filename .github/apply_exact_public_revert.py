from pathlib import Path

path = Path("controller/combined_pack_gamerule_fleet_exact_runner.py")
text = path.read_text(encoding="utf-8")
text = text.replace(
    "_ORIGINAL_RUN_PUBLIC_PROFILE_PHASE = CombinedPackGameruleFleetValidation.run_public_profile_phase\n",
    "",
    1,
)
start_marker = "def _run_public_profile_phase_with_restart_pack_evidence(\n"
if start_marker in text:
    start = text.index(start_marker)
    end_marker = "CombinedPackGameruleFleetValidation.install_artifacts = _install_exact_artifacts\n"
    end = text.index(end_marker, start)
    text = text[:start] + text[end:]
text = text.replace(
    "CombinedPackGameruleFleetValidation.run_public_profile_phase = _run_public_profile_phase_with_restart_pack_evidence\n",
    "",
    1,
)
if "_run_public_profile_phase_with_restart_pack_evidence" in text or "_ORIGINAL_RUN_PUBLIC_PROFILE_PHASE" in text:
    raise SystemExit("public phase adapter was not fully removed")
path.write_text(text, encoding="utf-8")
