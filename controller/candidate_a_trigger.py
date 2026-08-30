"""Resolve Candidate A benchmark trigger inputs without changing experiment semantics."""

from __future__ import annotations

import argparse
import pathlib

from controller.candidate_a_blocked_benchmark import BLOCK_SIZE, LEGAL_START_BLOCKS


class TriggerConfigurationError(ValueError):
    """Raised when a controlled benchmark trigger is incomplete or inconsistent."""


def parse_trigger_file(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in values:
            raise TriggerConfigurationError(f"duplicate trigger key: {key}")
        values[key] = value
    return values


def normalize_trigger(*, start_block: str, batch_size: str, prior_run_ids: str) -> dict[str, str]:
    try:
        start = int(start_block)
    except (TypeError, ValueError) as exc:
        raise TriggerConfigurationError(f"invalid start_block: {start_block!r}") from exc
    if start not in LEGAL_START_BLOCKS:
        raise TriggerConfigurationError(f"start_block must be one of {LEGAL_START_BLOCKS}: {start}")
    try:
        batch = int(batch_size)
    except (TypeError, ValueError) as exc:
        raise TriggerConfigurationError(f"invalid batch_size: {batch_size!r}") from exc
    if batch != BLOCK_SIZE:
        raise TriggerConfigurationError(f"batch_size is fixed at {BLOCK_SIZE}: {batch}")

    run_ids = [item.strip() for item in prior_run_ids.split(",") if item.strip()]
    if len(run_ids) > 4:
        raise TriggerConfigurationError("at most four prior run IDs are allowed")
    if len(set(run_ids)) != len(run_ids):
        raise TriggerConfigurationError("prior run IDs must be unique")
    for run_id in run_ids:
        if not run_id.isdigit() or int(run_id) <= 0:
            raise TriggerConfigurationError(f"invalid prior run ID: {run_id!r}")

    expected_prior_runs = (start - 1) // BLOCK_SIZE
    if len(run_ids) != expected_prior_runs:
        raise TriggerConfigurationError(
            f"start_block={start} requires exactly {expected_prior_runs} prior run IDs; got {len(run_ids)}"
        )
    return {
        "start_block": str(start),
        "batch_size": str(batch),
        "prior_run_ids": ",".join(run_ids),
    }


def resolve_trigger(
    *,
    event_name: str,
    trigger_file: pathlib.Path,
    input_start_block: str,
    input_batch_size: str,
    input_prior_run_ids: str,
) -> dict[str, str]:
    if event_name == "push":
        values = parse_trigger_file(trigger_file)
        return normalize_trigger(
            start_block=values.get("start_block", ""),
            batch_size=values.get("batch_size", ""),
            prior_run_ids=values.get("prior_run_ids", ""),
        )
    if event_name == "workflow_dispatch":
        return normalize_trigger(
            start_block=input_start_block,
            batch_size=input_batch_size,
            prior_run_ids=input_prior_run_ids,
        )
    raise TriggerConfigurationError(f"unsupported event_name: {event_name!r}")


def write_github_output(path: pathlib.Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for key in ("start_block", "batch_size", "prior_run_ids"):
            stream.write(f"{key}={values[key]}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True, choices=("push", "workflow_dispatch"))
    parser.add_argument("--trigger-file", type=pathlib.Path, required=True)
    parser.add_argument("--input-start-block", default="")
    parser.add_argument("--input-batch-size", default="")
    parser.add_argument("--input-prior-run-ids", default="")
    parser.add_argument("--github-output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    values = resolve_trigger(
        event_name=args.event_name,
        trigger_file=args.trigger_file,
        input_start_block=args.input_start_block,
        input_batch_size=args.input_batch_size,
        input_prior_run_ids=args.input_prior_run_ids,
    )
    write_github_output(args.github_output, values)
    print(
        f"resolved Candidate A trigger: start_block={values['start_block']} "
        f"batch_size={values['batch_size']} prior_run_ids={values['prior_run_ids'] or '<none>'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
