"""Tests for the sync module."""

from __future__ import annotations

import pytest

from skiall.core.sync import SyncAction, classify_items, merge_plugins
from pathlib import Path
from skiall.core.sync import build_skill_inventory, build_file_inventory


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


class TestBuildSkillInventory:
    def test_dirs_detected(self, tmp_path: Path):
        (tmp_path / "skill-a").mkdir()
        (tmp_path / "skill-a" / "SKILL.md").write_text("hello")
        (tmp_path / "skill-b").mkdir()
        (tmp_path / "skill-b" / "SKILL.md").write_text("world")
        inv = build_skill_inventory(tmp_path)
        assert "skill-a" in inv
        assert "skill-b" in inv

    def test_symlinks_skipped(self, tmp_path: Path):
        real = tmp_path / "real-skill"
        real.mkdir()
        (real / "SKILL.md").write_text("content")
        link = tmp_path / "linked-skill"
        try:
            link.symlink_to(real)
        except OSError:
            pytest.skip("symlinks not supported on this platform")
        inv = build_skill_inventory(tmp_path)
        assert "real-skill" in inv
        assert "linked-skill" not in inv

    def test_files_included(self, tmp_path: Path):
        """Standalone .skill files should appear in inventory."""
        (tmp_path / "my.skill").write_bytes(b"data")
        inv = build_skill_inventory(tmp_path)
        assert "my.skill" in inv

    def test_nonexistent_dir(self, tmp_path: Path):
        inv = build_skill_inventory(tmp_path / "nope")
        assert inv == {}


class TestBuildFileInventory:
    def test_single_file(self, tmp_path: Path):
        (tmp_path / "README.md").write_bytes(b"hello")
        inv = build_file_inventory(tmp_path, ["README.md"])
        assert "README.md" in inv
        assert inv["README.md"] == b"hello"

    def test_directory(self, tmp_path: Path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "a.md").write_bytes(b"aaa")
        inv = build_file_inventory(tmp_path, ["memory"])
        assert "memory/a.md" in inv

    def test_missing_path_skipped(self, tmp_path: Path):
        inv = build_file_inventory(tmp_path, ["nonexistent.txt"])
        assert inv == {}


from unittest.mock import patch
from skiall.core.sync import prompt_conflict, ConflictChoice


class TestPromptConflict:
    @patch("click.prompt", return_value="l")
    def test_choose_local(self, mock_prompt):
        choice = prompt_conflict("skill-creator", "skill")
        assert choice == ConflictChoice.LOCAL

    @patch("click.prompt", return_value="r")
    def test_choose_remote(self, mock_prompt):
        choice = prompt_conflict("skill-creator", "skill")
        assert choice == ConflictChoice.REMOTE

    @patch("click.prompt", return_value="s")
    def test_choose_skip(self, mock_prompt):
        choice = prompt_conflict("skill-creator", "skill")
        assert choice == ConflictChoice.SKIP
