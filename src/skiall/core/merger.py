"""Key-level merge for partial file sync.

Handles reading config files (JSON, TOML, YAML), merging only specified keys
from a repo version into a local version, and detecting conflicts on synced keys.

Merge Logic Flow
================

    repo_file (source of truth for synced keys)
        |
        v
    +-------------------+
    | read_config(repo) |---> repo_data: dict
    +-------------------+
                              |
    local_file (machine-specific keys preserved)
        |                     |
        v                     v
    +--------------------+   +----------------------------+
    | read_config(local) |-->| for key in synced_keys:    |
    +--------------------+   |   local[key] = repo[key]   |
         |                   |   (deep merge for dicts)   |
         |                   +----------------------------+
         v                              |
    local_data: dict                    v
    (untouched keys                merged_data: dict
     remain as-is)                 (repo keys injected,
                                    local keys preserved)

    Conflict Detection (three-way when base is available):
    -------------------------------------------------------
    For each synced key:
      - Two-way:   repo[key] != local[key]  => conflict
      - Three-way: repo[key] != base[key] AND local[key] != base[key] => conflict
                   (both sides changed since last sync)
"""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Config file I/O
# ---------------------------------------------------------------------------

def read_config(path: Path) -> dict:
    """Read a config file (JSON, TOML, or YAML) and return as dict.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file extension is unsupported.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")

    if suffix == ".json":
        return json.loads(text)
    elif suffix == ".toml":
        return tomllib.loads(text)
    elif suffix in (".yaml", ".yml"):
        result = yaml.safe_load(text)
        # yaml.safe_load returns None for empty files
        return result if isinstance(result, dict) else {}
    else:
        raise ValueError(f"Unsupported config format: {suffix}")


def write_config(path: Path, data: dict) -> None:
    """Write dict back to config file, preserving format based on extension.

    Note: TOML writing uses a minimal serializer since tomli_w is not available.
    Complex TOML structures (deeply nested inline tables, multiline strings) may
    not round-trip perfectly.

    Raises:
        ValueError: If the file extension is unsupported.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    # Ensure parent directory exists
    path.parent.mkdir(parents=True, exist_ok=True)

    if suffix == ".json":
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    elif suffix == ".toml":
        path.write_text(_serialize_toml(data), encoding="utf-8")
    elif suffix in (".yaml", ".yml"):
        path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    else:
        raise ValueError(f"Unsupported config format: {suffix}")


# ---------------------------------------------------------------------------
# Key-level merge
# ---------------------------------------------------------------------------

def merge_partial(repo_file: Path, local_file: Path, keys: list[str]) -> dict:
    """Merge specified keys from repo_file into local_file.

    Returns the merged content as a dict. Does NOT write to disk (caller decides).

    Behavior:
      - If local_file doesn't exist, starts from an empty dict and injects the
        requested keys from the repo.
      - Keys use dot-notation for nesting (e.g. "editor.fontSize").
      - If a key exists in both repo and local and both values are dicts, they
        are deep-merged (repo wins on leaf conflicts).
      - If a key is in the keys list but missing from repo_file, local value is
        left untouched (no deletion).
    """
    repo_data = read_config(repo_file)

    try:
        local_data = read_config(local_file)
    except FileNotFoundError:
        local_data = {}

    merged = copy.deepcopy(local_data)

    for key in keys:
        repo_val = _get_nested(repo_data, key)
        if repo_val is _MISSING:
            # Key not in repo -- leave local as-is
            continue
        _set_nested(merged, key, copy.deepcopy(repo_val))

    return merged


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def detect_key_conflicts(
    repo_file: Path,
    local_file: Path,
    base_file: Path | None,
    keys: list[str],
) -> list[str]:
    """Check if any synced keys differ between repo and local.

    Returns list of conflicting key paths (dot-notation). Only checks the
    specified keys, ignoring machine-specific keys entirely.

    Two modes:
      - Two-way (base_file is None): any difference on a synced key is a conflict.
      - Three-way (base_file provided): a conflict exists only when BOTH repo and
        local have changed the key relative to the base (true merge conflict).
    """
    repo_data = _safe_read(repo_file)
    local_data = _safe_read(local_file)
    base_data = _safe_read(base_file) if base_file else None

    conflicts: list[str] = []

    for key in keys:
        repo_val = _get_nested(repo_data, key)
        local_val = _get_nested(local_data, key)

        if base_data is None:
            # Two-way: any difference is a conflict
            if not _values_equal(repo_val, local_val):
                conflicts.append(key)
        else:
            # Three-way: conflict only if both sides changed from base
            base_val = _get_nested(base_data, key)
            repo_changed = not _values_equal(repo_val, base_val)
            local_changed = not _values_equal(local_val, base_val)
            if repo_changed and local_changed:
                # Both diverged -- only a real conflict if they diverged differently
                if not _values_equal(repo_val, local_val):
                    conflicts.append(key)

    return conflicts


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _MissingSentinel:
    """Sentinel for missing keys (distinct from None which is a valid value)."""

    def __repr__(self) -> str:
        return "<MISSING>"


_MISSING = _MissingSentinel()


def _safe_read(path: Path | None) -> dict:
    """Read config, returning empty dict if file is missing or path is None."""
    if path is None:
        return {}
    try:
        return read_config(path)
    except FileNotFoundError:
        return {}


def _get_nested(data: dict, dotted_key: str) -> Any:
    """Retrieve a value from a nested dict using dot-notation.

    Returns _MISSING if any segment of the path is absent.
    """
    segments = dotted_key.split(".")
    current: Any = data
    for seg in segments:
        if not isinstance(current, dict) or seg not in current:
            return _MISSING
        current = current[seg]
    return current


def _set_nested(data: dict, dotted_key: str, value: Any) -> None:
    """Set a value in a nested dict using dot-notation.

    Creates intermediate dicts as needed. If a repo value is a dict and the
    local value is also a dict, deep-merges (repo wins on leaf conflicts).
    """
    segments = dotted_key.split(".")
    current = data
    for seg in segments[:-1]:
        if seg not in current or not isinstance(current[seg], dict):
            current[seg] = {}
        current = current[seg]

    final_key = segments[-1]
    if (
        isinstance(value, dict)
        and final_key in current
        and isinstance(current[final_key], dict)
    ):
        current[final_key] = _deep_merge(current[final_key], value)
    else:
        current[final_key] = value


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*. Override wins on leaf conflicts."""
    merged = copy.deepcopy(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = copy.deepcopy(v)
    return merged


def _values_equal(a: Any, b: Any) -> bool:
    """Compare two values, treating _MISSING == _MISSING as True."""
    if isinstance(a, _MissingSentinel) and isinstance(b, _MissingSentinel):
        return True
    if isinstance(a, _MissingSentinel) or isinstance(b, _MissingSentinel):
        return False
    return a == b


# ---------------------------------------------------------------------------
# Minimal TOML serializer (no tomli_w dependency)
# ---------------------------------------------------------------------------

def _serialize_toml(data: dict, _prefix: str = "") -> str:
    """Serialize a dict to TOML format.

    Handles:
      - Strings, ints, floats, bools, lists of scalars
      - Nested tables (sections)
      - Arrays of tables

    Limitations vs. full TOML writers:
      - No multiline strings or inline tables
      - No datetime objects
      - Lists of mixed types or nested lists may not serialize correctly
    """
    lines: list[str] = []
    tables: list[tuple[str, str, dict]] = []  # (full_key, header, sub_dict)

    for key, value in data.items():
        full_key = f"{_prefix}.{key}" if _prefix else key
        if isinstance(value, dict):
            tables.append((full_key, full_key, value))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            # Array of tables
            for item in value:
                lines.append(f"\n[[{full_key}]]")
                lines.append(_serialize_toml_inline(item))
        else:
            lines.append(f"{key} = {_toml_value(value)}")

    for full_key, header, sub_dict in tables:
        lines.append(f"\n[{header}]")
        lines.append(_serialize_toml(sub_dict, _prefix=full_key).strip())

    return "\n".join(lines) + "\n"


def _serialize_toml_inline(data: dict) -> str:
    """Serialize dict values as flat key = value lines (no section headers)."""
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            # Flatten nested dicts -- not perfect but functional
            for sk, sv in value.items():
                lines.append(f"{key}.{sk} = {_toml_value(sv)}")
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines)


def _toml_value(value: Any) -> str:
    """Convert a Python value to its TOML representation."""
    if isinstance(value, bool):
        return "true" if value else "false"
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, float):
        return str(value)
    elif isinstance(value, str):
        # Escape backslashes and double quotes for TOML basic strings
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    elif isinstance(value, list):
        items = ", ".join(_toml_value(v) for v in value)
        return f"[{items}]"
    else:
        # Fallback -- quote as string
        return f'"{value}"'
