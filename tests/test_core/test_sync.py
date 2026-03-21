"""Tests for the sync module."""

from __future__ import annotations

import pytest

from skiall.core.sync import SyncAction, classify_items, merge_plugins


class TestClassifyItems:
    def test_remote_only(self):
        repo = {"a.txt": b"hello"}
        local = {}
        result = classify_items(repo, local)
        assert result == {"a.txt": SyncAction.REMOTE_ONLY}

    def test_local_only(self):
        repo = {}
        local = {"b.txt": b"world"}
        result = classify_items(repo, local)
        assert result == {"b.txt": SyncAction.LOCAL_ONLY}

    def test_identical(self):
        repo = {"c.txt": b"same"}
        local = {"c.txt": b"same"}
        result = classify_items(repo, local)
        assert result == {"c.txt": SyncAction.IDENTICAL}

    def test_conflict(self):
        repo = {"d.txt": b"repo version"}
        local = {"d.txt": b"local version"}
        result = classify_items(repo, local)
        assert result == {"d.txt": SyncAction.CONFLICT}

    def test_mixed(self):
        repo = {"a": b"1", "c": b"same", "d": b"repo"}
        local = {"b": b"2", "c": b"same", "d": b"local"}
        result = classify_items(repo, local)
        assert result == {
            "a": SyncAction.REMOTE_ONLY,
            "b": SyncAction.LOCAL_ONLY,
            "c": SyncAction.IDENTICAL,
            "d": SyncAction.CONFLICT,
        }


class TestMergePlugins:
    def test_union_disjoint(self):
        remote = {
            "version": 2,
            "plugins": {
                "plugin-a@registry": [{"scope": "user", "installPath": "/home/u/.claude/plugins/cache/registry/plugin-a/1.0", "version": "1.0", "installedAt": "2026-01-01T00:00:00Z", "lastUpdated": "2026-01-01T00:00:00Z"}],
            },
        }
        local = {
            "version": 2,
            "plugins": {
                "plugin-b@registry": [{"scope": "user", "installPath": "C:\\Users\\u\\.claude\\plugins\\cache\\registry\\plugin-b\\2.0", "version": "2.0", "installedAt": "2026-02-01T00:00:00Z", "lastUpdated": "2026-02-01T00:00:00Z"}],
            },
        }
        merged = merge_plugins(remote, local, local_cache_dir="/home/me/.claude/plugins/cache")
        assert "plugin-a@registry" in merged["plugins"]
        assert "plugin-b@registry" in merged["plugins"]

    def test_duplicate_keeps_newer(self):
        old_entry = {"scope": "user", "installPath": "/old/path", "version": "1.0", "installedAt": "2026-01-01T00:00:00Z", "lastUpdated": "2026-01-01T00:00:00Z"}
        new_entry = {"scope": "user", "installPath": "/new/path", "version": "2.0", "installedAt": "2026-01-01T00:00:00Z", "lastUpdated": "2026-03-01T00:00:00Z"}
        remote = {"version": 2, "plugins": {"p@r": [old_entry]}}
        local = {"version": 2, "plugins": {"p@r": [new_entry]}}
        merged = merge_plugins(remote, local, local_cache_dir="/home/me/.claude/plugins/cache")
        assert len(merged["plugins"]["p@r"]) == 1
        assert merged["plugins"]["p@r"][0]["version"] == "2.0"

    def test_multi_scope_preserved(self):
        user_entry = {"scope": "user", "installPath": "/p", "version": "1.0", "installedAt": "2026-01-01T00:00:00Z", "lastUpdated": "2026-01-01T00:00:00Z"}
        local_entry = {"scope": "local", "projectPath": "/proj", "installPath": "/p", "version": "1.0", "installedAt": "2026-02-01T00:00:00Z", "lastUpdated": "2026-02-01T00:00:00Z"}
        remote = {"version": 2, "plugins": {"p@r": [user_entry]}}
        local = {"version": 2, "plugins": {"p@r": [local_entry]}}
        merged = merge_plugins(remote, local, local_cache_dir="/home/me/.claude/plugins/cache")
        assert len(merged["plugins"]["p@r"]) == 2

    def test_install_path_rewritten(self):
        entry = {"scope": "user", "installPath": "/home/other/.claude/plugins/cache/registry/name/1.0", "version": "1.0", "installedAt": "2026-01-01T00:00:00Z", "lastUpdated": "2026-01-01T00:00:00Z"}
        remote = {"version": 2, "plugins": {"name@registry": [entry]}}
        local = {"version": 2, "plugins": {}}
        merged = merge_plugins(remote, local, local_cache_dir="/home/me/.claude/plugins/cache")
        path = merged["plugins"]["name@registry"][0]["installPath"]
        assert path.startswith("/home/me/.claude/plugins/cache/")

    def test_empty_inputs(self):
        merged = merge_plugins(None, None, local_cache_dir="/x")
        assert merged == {"version": 2, "plugins": {}}
