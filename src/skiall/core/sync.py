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
