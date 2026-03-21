"""Sync module — inventory comparison, conflict classification, and merge logic.

Used by the `skiall sync` command to merge remote and local state.
"""

from __future__ import annotations

from enum import Enum


class SyncAction(Enum):
    """Classification of an item during sync."""
    REMOTE_ONLY = "remote_only"
    LOCAL_ONLY = "local_only"
    IDENTICAL = "identical"
    CONFLICT = "conflict"


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
    local_cache_dir: str,
) -> dict:
    """Merge two installed_plugins.json structures by unioning plugin names.

    For same (plugin_name, scope) pairs, keeps the entry with newer lastUpdated.
    Rewrites installPath to use local_cache_dir.

    Args:
        remote_data: parsed installed_plugins.json from repo (or None)
        local_data: parsed installed_plugins.json from local (or None)
        local_cache_dir: absolute path to local plugins cache dir

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

        # Rewrite installPath for all entries
        entries = list(by_scope.values())
        for entry in entries:
            entry["installPath"] = _rewrite_install_path(
                entry.get("installPath", ""), name, entry.get("version", ""), local_cache_dir
            )
        merged[name] = entries

    return {"version": 2, "plugins": merged}


def _rewrite_install_path(
    original: str, plugin_name: str, version: str, local_cache_dir: str
) -> str:
    """Rebuild installPath using local cache dir.

    Plugin name format: "name@registry" -> cache path: <cache_dir>/<registry>/<name>/<version>
    """
    if "@" in plugin_name:
        name_part, registry = plugin_name.rsplit("@", 1)
    else:
        name_part = plugin_name
        registry = "unknown"

    cache = local_cache_dir.rstrip("/").rstrip("\\")
    return f"{cache}/{registry}/{name_part}/{version}"
