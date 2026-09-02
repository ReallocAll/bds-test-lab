from pathlib import Path

exact_path = Path("controller/combined_pack_gamerule_fleet_exact_runner.py")
text = exact_path.read_text(encoding="utf-8")
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
exact_path.write_text(text, encoding="utf-8")

workflow_path = Path(".github/workflows/combined-pack-gamerule-20p-checks.yml")
workflow = workflow_path.read_text(encoding="utf-8")
if "tests.test_combined_windows_final_runner" not in workflow:
    workflow = workflow.replace(
        "          tests.test_combined_pack_exact_runner\n",
        "          tests.test_combined_pack_exact_runner\n          tests.test_combined_windows_final_runner\n",
        1,
    )
if "          controller/combined_windows_final_runner.py\n" not in workflow:
    workflow = workflow.replace(
        "          controller/combined_pack_gamerule_fleet_exact_runner.py\n",
        "          controller/combined_pack_gamerule_fleet_exact_runner.py\n          controller/combined_windows_final_runner.py\n",
        1,
    )
if "          tests/test_combined_windows_final_runner.py\n" not in workflow:
    workflow = workflow.replace(
        "          tests/test_combined_pack_exact_runner.py\n",
        "          tests/test_combined_pack_exact_runner.py\n          tests/test_combined_windows_final_runner.py\n",
        1,
    )
ruff_anchor = "          controller/combined_pack_gamerule_fleet_exact_runner.py\n          controller/windows_evidence_matrix.py\n"
if "          controller/combined_windows_final_runner.py\n          controller/windows_evidence_matrix.py\n" not in workflow:
    workflow = workflow.replace(
        ruff_anchor,
        "          controller/combined_pack_gamerule_fleet_exact_runner.py\n          controller/combined_windows_final_runner.py\n          controller/windows_evidence_matrix.py\n",
        1,
    )
ruff_test_anchor = "          tests/test_combined_pack_exact_runner.py\n\n      - name: Parse 20-player workflows"
if "          tests/test_combined_windows_final_runner.py\n\n      - name: Parse 20-player workflows" not in workflow:
    workflow = workflow.replace(
        ruff_test_anchor,
        "          tests/test_combined_pack_exact_runner.py\n          tests/test_combined_windows_final_runner.py\n\n      - name: Parse 20-player workflows",
        1,
    )
workflow_path.write_text(workflow, encoding="utf-8")
