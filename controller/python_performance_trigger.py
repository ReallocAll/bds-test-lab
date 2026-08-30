from __future__ import annotations

import argparse
import pathlib
import re
from dataclasses import dataclass

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MIN_DURATION_SECONDS = 180
MAX_DURATION_SECONDS = 900
_ALLOWED_KEYS = {"spark_sha", "duration_seconds"}


class TriggerConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class PerformanceTrigger:
    spark_sha: str
    duration_seconds: int


def _validate(spark_sha: str, duration_seconds: str | int) -> PerformanceTrigger:
    sha = str(spark_sha).strip().lower()
    if not SHA_RE.fullmatch(sha):
        raise TriggerConfigurationError(f"spark_sha must be an exact lowercase 40-character SHA: {spark_sha!r}")
    raw_duration = str(duration_seconds).strip()
    if not raw_duration.isdigit():
        raise TriggerConfigurationError(f"duration_seconds must be an integer: {duration_seconds!r}")
    duration = int(raw_duration)
    if duration < MIN_DURATION_SECONDS or duration > MAX_DURATION_SECONDS:
        raise TriggerConfigurationError(
            f"duration_seconds must be in {MIN_DURATION_SECONDS}..{MAX_DURATION_SECONDS}: {duration}"
        )
    return PerformanceTrigger(sha, duration)


def parse_trigger_file(path: pathlib.Path) -> PerformanceTrigger:
    if not path.is_file():
        raise TriggerConfigurationError(f"trigger file does not exist: {path}")
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise TriggerConfigurationError(f"trigger line {line_number} is not key=value: {raw!r}")
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in _ALLOWED_KEYS:
            raise TriggerConfigurationError(f"trigger line {line_number} has unknown key {key!r}")
        if key in values:
            raise TriggerConfigurationError(f"trigger line {line_number} duplicates key {key!r}")
        if not value:
            raise TriggerConfigurationError(f"trigger line {line_number} has an empty value for {key!r}")
        values[key] = value
    missing = sorted(_ALLOWED_KEYS - values.keys())
    if missing:
        raise TriggerConfigurationError(f"trigger file is missing keys: {missing}")
    return _validate(values["spark_sha"], values["duration_seconds"])


def resolve_trigger(
    *,
    event_name: str,
    trigger_path: pathlib.Path,
    requested_spark_sha: str,
    requested_duration_seconds: str,
) -> PerformanceTrigger:
    if event_name == "push":
        return parse_trigger_file(trigger_path)
    if event_name == "workflow_dispatch":
        return _validate(requested_spark_sha, requested_duration_seconds)
    raise TriggerConfigurationError(f"unsupported GitHub event: {event_name!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--trigger-path", type=pathlib.Path, default=pathlib.Path("triggers/python-attribution-performance.txt"))
    parser.add_argument("--requested-spark-sha", default="")
    parser.add_argument("--requested-duration-seconds", default="")
    args = parser.parse_args()
    resolved = resolve_trigger(
        event_name=args.event_name,
        trigger_path=args.trigger_path,
        requested_spark_sha=args.requested_spark_sha,
        requested_duration_seconds=args.requested_duration_seconds,
    )
    print(f"EXPECTED_SPARK_SHA={resolved.spark_sha}")
    print(f"DURATION_SECONDS={resolved.duration_seconds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
