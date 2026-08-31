"""Safe bStats configuration for controlled Endstone benchmark cases."""

from __future__ import annotations

import hashlib
import os
import pathlib
import tempfile
from typing import Any

import tomllib

B_STATS_CONFIG_RELATIVE_PATH = "plugins/bstats/config.toml"
B_STATS_EVIDENCE_PATH = "bstats-config.toml"
B_STATS_CANONICAL_TOML = "enabled = false"
B_STATS_CONFIG_BYTES = (B_STATS_CANONICAL_TOML + "\n").encode("utf-8")


class BStatsConfigError(RuntimeError):
    """Raised when the bStats configuration is missing or not disabled."""


def _safe_directory(root: pathlib.Path, name: str) -> pathlib.Path:
    target = root / name
    if target.is_symlink():
        raise BStatsConfigError(f"bStats path component is a symlink: {target}")
    try:
        target.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise BStatsConfigError(f"bStats path component escapes server directory: {target}") from exc
    try:
        target.mkdir(exist_ok=True)
    except OSError as exc:
        raise BStatsConfigError(f"unable to create bStats directory: {target}: {exc}") from exc
    if not target.is_dir() or target.is_symlink():
        raise BStatsConfigError(f"bStats path component is not a directory: {target}")
    return target


def _config_path(server_dir: pathlib.Path) -> pathlib.Path:
    raw_root = pathlib.Path(server_dir)
    if raw_root.is_symlink():
        raise BStatsConfigError(f"server directory is a symlink: {raw_root}")
    root = raw_root.resolve()
    if not root.is_dir():
        raise BStatsConfigError(f"server directory is unavailable: {root}")
    plugins = _safe_directory(root, "plugins")
    bstats = _safe_directory(plugins, "bstats")
    target = bstats / "config.toml"
    if target.is_symlink():
        raise BStatsConfigError(f"bStats config is a symlink: {target}")
    try:
        target.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BStatsConfigError(f"bStats config escapes server directory: {target}") from exc
    return target


def _parse_disabled(path: pathlib.Path, raw: bytes) -> None:
    if raw != B_STATS_CONFIG_BYTES:
        raise BStatsConfigError(f"bStats config is not canonical: {path}")
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BStatsConfigError(f"bStats config is malformed: {path}") from exc
    if parsed.get("enabled") is not False:
        raise BStatsConfigError(f"bStats config enabled value is not false: {path}")


def inspect_bstats_config(path: pathlib.Path, *, expected_relative_path: str | None = None) -> dict[str, Any]:
    """Verify a canonical disabled config and return reproducible file evidence."""

    config_path = pathlib.Path(path)
    if config_path.is_symlink() or not config_path.is_file():
        raise BStatsConfigError(f"bStats config is missing or symlinked: {config_path}")
    try:
        raw = config_path.read_bytes()
        size = config_path.stat().st_size
    except OSError as exc:
        raise BStatsConfigError(f"unable to read bStats config: {config_path}: {exc}") from exc
    _parse_disabled(config_path, raw)
    try:
        relative_path = (
            config_path.resolve().relative_to(pathlib.Path(expected_relative_path).resolve()).as_posix()
            if expected_relative_path is not None
            else B_STATS_CONFIG_RELATIVE_PATH
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise BStatsConfigError(f"bStats config path escapes its expected root: {config_path}") from exc
    if relative_path != B_STATS_CONFIG_RELATIVE_PATH:
        raise BStatsConfigError(f"bStats config relative path mismatch: {relative_path!r}")
    return {
        "relative_path": B_STATS_CONFIG_RELATIVE_PATH,
        "evidence_path": B_STATS_EVIDENCE_PATH,
        "canonical_toml": B_STATS_CANONICAL_TOML,
        "canonical_enabled": False,
        "bytes": size,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def write_disabled_bstats_config(server_dir: pathlib.Path) -> dict[str, Any]:
    """Atomically write and verify the disabled bStats config for one server."""

    target = _config_path(pathlib.Path(server_dir))
    temporary_path: pathlib.Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".config.toml.", dir=str(target.parent))
        temporary_path = pathlib.Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(B_STATS_CONFIG_BYTES)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        return inspect_bstats_config(target)
    except BStatsConfigError:
        raise
    except OSError as exc:
        raise BStatsConfigError(f"unable to atomically write bStats config: {target}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def copy_bstats_evidence(evidence_root: pathlib.Path, source_path: pathlib.Path) -> dict[str, Any]:
    """Atomically copy a verified disabled config into a case evidence root."""

    raw_root = pathlib.Path(evidence_root)
    if raw_root.is_symlink():
        raise BStatsConfigError(f"bStats evidence root is a symlink: {raw_root}")
    root = raw_root.resolve()
    if not root.is_dir():
        raise BStatsConfigError(f"bStats evidence root is unavailable: {root}")
    target = root / B_STATS_EVIDENCE_PATH
    if target.is_symlink():
        raise BStatsConfigError(f"bStats evidence file is a symlink: {target}")
    try:
        if target.resolve().relative_to(root) != pathlib.Path(B_STATS_EVIDENCE_PATH):
            raise BStatsConfigError(f"bStats evidence path escapes case directory: {target}")
    except (OSError, RuntimeError, ValueError) as exc:
        raise BStatsConfigError(f"bStats evidence path escapes case directory: {target}") from exc
    if target.exists() and not target.is_file():
        raise BStatsConfigError(f"bStats evidence path is not a regular file: {target}")
    source_evidence = inspect_bstats_config(pathlib.Path(source_path))
    try:
        raw = pathlib.Path(source_path).read_bytes()
    except OSError as exc:
        raise BStatsConfigError(f"unable to read bStats source config: {source_path}: {exc}") from exc
    temporary_path: pathlib.Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".bstats-config.", dir=str(root))
        temporary_path = pathlib.Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        copied = inspect_bstats_config(target)
    except BStatsConfigError:
        raise
    except OSError as exc:
        raise BStatsConfigError(f"unable to atomically copy bStats evidence: {target}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
    if copied != source_evidence:
        raise BStatsConfigError("copied bStats evidence does not match the source config")
    return copied
