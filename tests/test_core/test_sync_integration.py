"""Integration tests for the full sync flow."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skiall.core.engine import Engine
from skiall.core.sync import ConflictChoice


class TestSyncSkills:
    def test_remote_only_skill_deployed(self, tmp_path: Path):
        """A skill in repo but not local gets deployed to local."""
        repo_dir = tmp_path / "repo"
        skill_dir = repo_dir / "claude-code" / "skills" / "remote-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("remote skill content")

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            home = tmp_path / "home"
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True)

            from skiall.adapters.claude_code import ClaudeCodeAdapter

            adapter = ClaudeCodeAdapter(repo_dir)
            engine = Engine(repo_dir, [adapter])
            engine._ensure_repo = MagicMock()
            engine._git_commit_and_push = MagicMock()

            reports = engine.sync()
            deployed = claude_dir / "skills" / "remote-skill" / "SKILL.md"
            assert deployed.exists()
            assert deployed.read_text() == "remote skill content"

    def test_local_only_skill_collected(self, tmp_path: Path):
        """A skill only on local gets collected into repo."""
        repo_dir = tmp_path / "repo"
        (repo_dir / "claude-code" / "skills").mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            home = tmp_path / "home"
            local_skill = home / ".claude" / "skills" / "local-skill"
            local_skill.mkdir(parents=True)
            (local_skill / "SKILL.md").write_text("local only")

            from skiall.adapters.claude_code import ClaudeCodeAdapter

            adapter = ClaudeCodeAdapter(repo_dir)
            engine = Engine(repo_dir, [adapter])
            engine._ensure_repo = MagicMock()
            engine._git_commit_and_push = MagicMock()

            reports = engine.sync()
            collected = repo_dir / "claude-code" / "skills" / "local-skill" / "SKILL.md"
            assert collected.exists()
            assert collected.read_text() == "local only"

    def test_conflict_user_chooses_local(self, tmp_path: Path):
        """When same skill differs, user choosing 'local' overwrites repo."""
        repo_dir = tmp_path / "repo"
        repo_skill = repo_dir / "claude-code" / "skills" / "shared-skill"
        repo_skill.mkdir(parents=True)
        (repo_skill / "SKILL.md").write_text("repo version")

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            home = tmp_path / "home"
            local_skill = home / ".claude" / "skills" / "shared-skill"
            local_skill.mkdir(parents=True)
            (local_skill / "SKILL.md").write_text("local version")

            from skiall.adapters.claude_code import ClaudeCodeAdapter

            adapter = ClaudeCodeAdapter(repo_dir)
            engine = Engine(repo_dir, [adapter])
            engine._ensure_repo = MagicMock()
            engine._git_commit_and_push = MagicMock()

            with patch("skiall.core.engine.prompt_conflict", return_value=ConflictChoice.LOCAL):
                reports = engine.sync()

            assert (repo_skill / "SKILL.md").read_text() == "local version"
            assert (local_skill / "SKILL.md").read_text() == "local version"

    def test_conflict_user_chooses_remote(self, tmp_path: Path):
        """When same skill differs, user choosing 'remote' overwrites local."""
        repo_dir = tmp_path / "repo"
        repo_skill = repo_dir / "claude-code" / "skills" / "shared-skill"
        repo_skill.mkdir(parents=True)
        (repo_skill / "SKILL.md").write_text("repo version")

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            home = tmp_path / "home"
            local_skill = home / ".claude" / "skills" / "shared-skill"
            local_skill.mkdir(parents=True)
            (local_skill / "SKILL.md").write_text("local version")

            from skiall.adapters.claude_code import ClaudeCodeAdapter

            adapter = ClaudeCodeAdapter(repo_dir)
            engine = Engine(repo_dir, [adapter])
            engine._ensure_repo = MagicMock()
            engine._git_commit_and_push = MagicMock()

            with patch("skiall.core.engine.prompt_conflict", return_value=ConflictChoice.REMOTE):
                reports = engine.sync()

            assert (local_skill / "SKILL.md").read_text() == "repo version"


class TestSyncPlugins:
    def test_plugins_merged(self, tmp_path: Path):
        """Plugins from both sides are unioned."""
        repo_dir = tmp_path / "repo"
        repo_plugins = repo_dir / "claude-code" / "plugins"
        repo_plugins.mkdir(parents=True)
        (repo_plugins / "installed_plugins.json").write_text(json.dumps({
            "version": 2,
            "plugins": {
                "remote-plugin@registry": [{"scope": "user", "installPath": "/old", "version": "1.0", "installedAt": "2026-01-01T00:00:00Z", "lastUpdated": "2026-01-01T00:00:00Z"}],
            },
        }))
        (repo_dir / "claude-code" / "skills").mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            home = tmp_path / "home"
            local_plugins = home / ".claude" / "plugins"
            local_plugins.mkdir(parents=True)
            (local_plugins / "installed_plugins.json").write_text(json.dumps({
                "version": 2,
                "plugins": {
                    "local-plugin@registry": [{"scope": "user", "installPath": "C:\\old", "version": "2.0", "installedAt": "2026-02-01T00:00:00Z", "lastUpdated": "2026-02-01T00:00:00Z"}],
                },
            }))

            from skiall.adapters.claude_code import ClaudeCodeAdapter

            adapter = ClaudeCodeAdapter(repo_dir)
            engine = Engine(repo_dir, [adapter])
            engine._ensure_repo = MagicMock()
            engine._git_commit_and_push = MagicMock()

            reports = engine.sync()

            merged = json.loads((local_plugins / "installed_plugins.json").read_text())
            assert "remote-plugin@registry" in merged["plugins"]
            assert "local-plugin@registry" in merged["plugins"]


class TestSyncFiles:
    def test_remote_only_file_deployed(self, tmp_path: Path):
        """A file in repo but not local gets deployed."""
        repo_dir = tmp_path / "repo"
        (repo_dir / "claude-code").mkdir(parents=True)
        (repo_dir / "claude-code" / "CLAUDE.md").write_text("remote claude md")
        (repo_dir / "claude-code" / "skills").mkdir()

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            home = tmp_path / "home"
            (home / ".claude").mkdir(parents=True)

            from skiall.adapters.claude_code import ClaudeCodeAdapter

            adapter = ClaudeCodeAdapter(repo_dir)
            engine = Engine(repo_dir, [adapter])
            engine._ensure_repo = MagicMock()
            engine._git_commit_and_push = MagicMock()

            reports = engine.sync()
            assert (home / ".claude" / "CLAUDE.md").read_text() == "remote claude md"

    def test_local_only_file_collected(self, tmp_path: Path):
        """A file only on local gets collected into repo."""
        repo_dir = tmp_path / "repo"
        (repo_dir / "claude-code" / "skills").mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            home = tmp_path / "home"
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True)
            (claude_dir / "CLAUDE.md").write_text("local claude md")

            from skiall.adapters.claude_code import ClaudeCodeAdapter

            adapter = ClaudeCodeAdapter(repo_dir)
            engine = Engine(repo_dir, [adapter])
            engine._ensure_repo = MagicMock()
            engine._git_commit_and_push = MagicMock()

            reports = engine.sync()
            assert (repo_dir / "claude-code" / "CLAUDE.md").read_text() == "local claude md"
