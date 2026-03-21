"""Tests for the sync engine."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skiall.adapters.base import BaseAdapter
from skiall.core.engine import Engine
from skiall.core.types import Change, ChangeKind, PlatformPaths, SyncReport, SyncRule


class FakeAdapter(BaseAdapter):
    """A minimal adapter for testing."""

    def __init__(self, repo_dir: Path, adapter_name: str = "fake", deps: list[str] | None = None):
        super().__init__(repo_dir)
        self._name = adapter_name
        self._deps = deps or []
        self._detected = True
        self._changes: list[Change] = []
        self.deploy_called = False
        self.collect_called = False

    @property
    def name(self) -> str:
        return self._name

    def detect(self) -> bool:
        return self._detected

    def get_paths(self) -> PlatformPaths:
        return PlatformPaths(config_dir=Path("/tmp/fake"))

    def get_sync_rules(self) -> list[SyncRule]:
        return []

    def collect(self) -> SyncReport:
        self.collect_called = True
        return SyncReport(adapter_name=self.name, files_synced=["test.txt"])

    def deploy(self) -> SyncReport:
        self.deploy_called = True
        return SyncReport(adapter_name=self.name, files_synced=["test.txt"])

    def diff(self) -> list[Change]:
        return self._changes

    def get_dependencies(self) -> list[str]:
        return self._deps


class TestResolveOrder:
    def test_no_dependencies(self, tmp_path: Path):
        a = FakeAdapter(tmp_path, "a")
        b = FakeAdapter(tmp_path, "b")
        engine = Engine(tmp_path, [a, b])
        order = engine.resolve_order()
        assert len(order) == 2

    def test_dependency_ordering(self, tmp_path: Path):
        """T15: Shared adapter runs before Claude Code adapter."""
        shared = FakeAdapter(tmp_path, "shared")
        claude = FakeAdapter(tmp_path, "claude-code", deps=["shared"])
        codex = FakeAdapter(tmp_path, "codex", deps=["shared"])
        engine = Engine(tmp_path, [claude, codex, shared])
        order = engine.resolve_order()
        names = [a.name for a in order]
        assert names.index("shared") < names.index("claude-code")
        assert names.index("shared") < names.index("codex")

    def test_circular_dependency_raises(self, tmp_path: Path):
        a = FakeAdapter(tmp_path, "a", deps=["b"])
        b = FakeAdapter(tmp_path, "b", deps=["a"])
        engine = Engine(tmp_path, [a, b])
        with pytest.raises(ValueError, match="Circular dependency"):
            engine.resolve_order()


class TestPull:
    def test_pull_deploys_detected_adapters(self, tmp_path: Path):
        a = FakeAdapter(tmp_path, "a")
        engine = Engine(tmp_path, [a])
        # Mock _git_pull to avoid actual git operations
        engine._git_pull = MagicMock()
        reports = engine.pull()
        assert a.deploy_called
        assert len(reports) == 1
        assert reports[0].success

    def test_pull_skips_undetected_platforms(self, tmp_path: Path):
        """T20: Platform not installed → graceful skip."""
        a = FakeAdapter(tmp_path, "a")
        a._detected = False
        engine = Engine(tmp_path, [a])
        engine._git_pull = MagicMock()
        reports = engine.pull()
        assert not a.deploy_called
        assert len(reports[0].warnings) > 0

    def test_pull_blocks_on_conflicts(self, tmp_path: Path):
        """T8: Conflict detection → warn and stop."""
        a = FakeAdapter(tmp_path, "a")
        a._changes = [Change(path="CLAUDE.md", kind=ChangeKind.MODIFIED, detail="local changes")]
        engine = Engine(tmp_path, [a])
        engine._git_pull = MagicMock()
        reports = engine.pull(force=False)
        assert not a.deploy_called
        assert not reports[0].success

    def test_pull_force_overrides_conflicts(self, tmp_path: Path):
        """T9: --force overrides local changes."""
        a = FakeAdapter(tmp_path, "a")
        a._changes = [Change(path="CLAUDE.md", kind=ChangeKind.MODIFIED, detail="local changes")]
        engine = Engine(tmp_path, [a])
        engine._git_pull = MagicMock()
        reports = engine.pull(force=True)
        assert a.deploy_called
        assert reports[0].success


class TestPush:
    def test_push_collects_from_detected_adapters(self, tmp_path: Path):
        a = FakeAdapter(tmp_path, "a")
        engine = Engine(tmp_path, [a])
        engine._git_commit_and_push = MagicMock()
        reports = engine.push()
        assert a.collect_called
        assert len(reports) == 1

    def test_push_skips_undetected_platforms(self, tmp_path: Path):
        a = FakeAdapter(tmp_path, "a")
        a._detected = False
        engine = Engine(tmp_path, [a])
        engine._git_commit_and_push = MagicMock()
        reports = engine.push()
        assert not a.collect_called


class TestStatus:
    def test_status_shows_changes(self, tmp_path: Path):
        a = FakeAdapter(tmp_path, "a")
        a._changes = [Change(path="test.txt", kind=ChangeKind.ADDED)]
        engine = Engine(tmp_path, [a])
        result = engine.status()
        assert "a" in result
        assert len(result["a"]) == 1
        assert result["a"][0].kind == ChangeKind.ADDED

    def test_status_shows_not_installed(self, tmp_path: Path):
        a = FakeAdapter(tmp_path, "a")
        a._detected = False
        engine = Engine(tmp_path, [a])
        result = engine.status()
        assert result["a"][0].kind == ChangeKind.DELETED


class TestSetup:
    def test_setup_creates_repo_structure(self, tmp_path: Path):
        repo_dir = tmp_path / "skiall-repo"
        a = FakeAdapter(repo_dir, "a")
        engine = Engine(repo_dir, [a])
        manifest = engine.setup()
        assert (repo_dir / ".git").exists()
        assert (repo_dir / "skiall.yaml").exists()
        assert (repo_dir / ".gitignore").exists()
        assert "platforms" in manifest


class TestSync:
    def test_sync_clones_when_no_repo(self, tmp_path: Path):
        """sync() with a URL and no existing repo should call _ensure_repo."""
        repo_dir = tmp_path / "skiall-repo"
        a = FakeAdapter(repo_dir, "a")
        engine = Engine(repo_dir, [a])
        engine._ensure_repo = MagicMock()
        engine._git_commit_and_push = MagicMock()
        engine.sync(remote_url="https://example.com/repo.git")
        engine._ensure_repo.assert_called_once_with("https://example.com/repo.git")

    def test_sync_pulls_when_repo_exists(self, tmp_path: Path):
        """sync() with existing repo should call _ensure_repo with None."""
        repo_dir = tmp_path / "skiall-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        a = FakeAdapter(repo_dir, "a")
        engine = Engine(repo_dir, [a])
        engine._ensure_repo = MagicMock()
        engine._git_commit_and_push = MagicMock()
        engine.sync()
        engine._ensure_repo.assert_called_once_with(None)
