"""Sync module — inventory comparison, conflict classification, and merge logic.

Used by the `skiall sync` command to merge remote and local state.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from pathlib import Path

import click


class SyncAction(Enum):
    """Classification of an item during sync."""
    REMOTE_ONLY = "remote_only"
    LOCAL_ONLY = "local_only"
    IDENTICAL = "identical"
    CONFLICT = "conflict"


class ConflictChoice(Enum):
    """User's choice when a conflict is detected."""
    LOCAL = "local"
    REMOTE = "remote"
    SKIP = "skip"


def prompt_conflict(name: str, item_type: str) -> ConflictChoice:
    """Prompt the user to resolve a conflict interactively.

    Args:
        name: name of the conflicting item (e.g., "skill-creator")
        item_type: type label for display (e.g., "skill", "file", "memory")

    Returns:
        user's choice
    """
    choice = click.prompt(
        f'  "{name}" ({item_type}) differs. Keep (l)ocal, (r)emote, or (s)kip?',
        type=click.Choice(["l", "r", "s"], case_sensitive=False),
        default="s",
    )
    return {
        "l": ConflictChoice.LOCAL,
        "r": ConflictChoice.REMOTE,
        "s": ConflictChoice.SKIP,
    }[choice.lower()]


def classify_items(
    repo_items: dict[str, bytes],
    local_items: dict[str, bytes],
) -> dict[str, SyncAction]:
    """Compare repo vs local inventories and classify each item.

    Args:
        repo_items: mapping of relative path -> content bytes from repo
        local_items: mapping of relative path -> content bytes from local

    Returns:
        mapping of relative path -> SyncAction classification
    """
    result: dict[str, SyncAction] = {}
    all_keys = sorted(set(repo_items) | set(local_items))
    for key in all_keys:
        in_repo = key in repo_items
        in_local = key in local_items
        if in_repo and not in_local:
            result[key] = SyncAction.REMOTE_ONLY
        elif in_local and not in_repo:
            result[key] = SyncAction.LOCAL_ONLY
        elif repo_items[key] == local_items[key]:
            result[key] = SyncAction.IDENTICAL
        else:
            result[key] = SyncAction.CONFLICT
    return result


def merge_plugins(
    remote_data: dict | None,
    local_data: dict | None,
) -> dict:
    """Merge two installed_plugins.json structures by unioning plugin names.

    For same (plugin_name, scope) pairs, keeps the entry with newer lastUpdated.
    installPath is left as-is — each machine's Claude Code manages its own paths.

    Args:
        remote_data: parsed installed_plugins.json from repo (or None)
        local_data: parsed installed_plugins.json from local (or None)

    Returns:
        merged installed_plugins.json dict
    """
    remote_plugins = (remote_data or {}).get("plugins", {})
    local_plugins = (local_data or {}).get("plugins", {})
    all_names = sorted(set(remote_plugins) | set(local_plugins))

    merged: dict[str, list[dict]] = {}
    for name in all_names:
        remote_entries = remote_plugins.get(name, [])
        local_entries = local_plugins.get(name, [])

        # Index by scope for dedup
        by_scope: dict[str, dict] = {}
        for entry in remote_entries:
            scope = entry.get("scope", "user")
            by_scope[scope] = dict(entry)
        for entry in local_entries:
            scope = entry.get("scope", "user")
            existing = by_scope.get(scope)
            if existing is None:
                by_scope[scope] = dict(entry)
            else:
                # Keep newer by lastUpdated
                if entry.get("lastUpdated", "") > existing.get("lastUpdated", ""):
                    by_scope[scope] = dict(entry)

        merged[name] = list(by_scope.values())

    return {"version": 2, "plugins": merged}


def strip_install_paths(data: dict) -> dict:
    """Return a copy of installed_plugins.json with installPath removed.

    Used for the repo copy — installPath is machine-specific and should
    not be stored in the portable sync repo.
    """
    stripped: dict[str, list[dict]] = {}
    for name, entries in data.get("plugins", {}).items():
        stripped[name] = []
        for entry in entries:
            clean = {k: v for k, v in entry.items() if k != "installPath"}
            stripped[name].append(clean)
    return {"version": 2, "plugins": stripped}


def build_skill_inventory(skills_dir: Path) -> dict[str, bytes]:
    """Build an inventory of skills as name -> content hash.

    Directories are hashed by concatenating all file contents.
    Symlinks are skipped. Standalone files (like .skill) are included.

    Args:
        skills_dir: path to the skills directory

    Returns:
        mapping of skill name -> content digest bytes
    """
    if not skills_dir.is_dir():
        return {}

    inventory: dict[str, bytes] = {}
    for entry in sorted(skills_dir.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            hasher = hashlib.sha256()
            for f in sorted(entry.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    rel = f.relative_to(entry).as_posix()
                    parts = rel.split("/")
                    if any(p in (".git", "node_modules", "__pycache__") for p in parts):
                        continue
                    hasher.update(rel.encode())
                    hasher.update(f.read_bytes().replace(b"\r\n", b"\n"))
            inventory[entry.name] = hasher.digest()
        elif entry.is_file():
            inventory[entry.name] = entry.read_bytes().replace(b"\r\n", b"\n")

    return inventory


def build_file_inventory(
    base_dir: Path, paths: list[str]
) -> dict[str, bytes]:
    """Build an inventory of individual files/directories.

    Args:
        base_dir: root directory (e.g., ~/.claude/ or repo/claude-code/)
        paths: list of relative paths to inventory

    Returns:
        mapping of relative path -> content bytes
    """
    inventory: dict[str, bytes] = {}
    for rel_path in paths:
        full = base_dir / rel_path
        if not full.exists():
            continue
        if full.is_file():
            inventory[rel_path] = full.read_bytes().replace(b"\r\n", b"\n")
        elif full.is_dir():
            for f in sorted(full.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    rel = f.relative_to(base_dir).as_posix()
                    inventory[rel] = f.read_bytes().replace(b"\r\n", b"\n")
    return inventory
