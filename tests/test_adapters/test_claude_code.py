"""Tests for the Claude Code adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skiall.adapters.claude_code import ClaudeCodeAdapter
from skiall.core.types import SyncType


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to tmp_path so ~/.claude/ is isolated."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture()
def claude_dir(fake_home: Path) -> Path:
    """Create a minimal ~/.claude/ tree and return its path."""
    d = fake_home / ".claude"
    d.mkdir()
    return d


@pytest.fixture()
def repo_dir(tmp_path: Path) -> Path:
    """Return an empty repo directory."""
    d = tmp_path / "repo"
    d.mkdir()
    return d


@pytest.fixture()
def adapter(repo_dir: Path) -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter(repo_dir=repo_dir)


# ------------------------------------------------------------------ #
# T1: detect()
# ------------------------------------------------------------------ #


class TestDetect:
    def test_detect_true_when_dir_exists(
        self, adapter: ClaudeCodeAdapter, claude_dir: Path
    ) -> None:
        assert adapter.detect() is True

    def test_detect_false_when_dir_missing(
        self, adapter: ClaudeCodeAdapter, fake_home: Path
    ) -> None:
        # fake_home exists but ~/.claude/ does not.
        assert adapter.detect() is False


# ------------------------------------------------------------------ #
# T2: get_paths()
# ------------------------------------------------------------------ #


class TestGetPaths:
    def test_config_dir_points_to_dot_claude(
        self, adapter: ClaudeCodeAdapter, fake_home: Path
    ) -> None:
        paths = adapter.get_paths()
        assert paths.config_dir == fake_home / ".claude"


# ------------------------------------------------------------------ #
# T3: Collect / deploy round-trip for CLAUDE.md
# ------------------------------------------------------------------ #


class TestCollectDeployRoundTrip:
    def test_claude_md_round_trip(
        self, adapter: ClaudeCodeAdapter, claude_dir: Path, repo_dir: Path
    ) -> None:
        # Create a CLAUDE.md in the fake ~/.claude/.
        original = "# My Setup\nSome instructions.\n"
        (claude_dir / "CLAUDE.md").write_text(original, encoding="utf-8")

        # Collect: local → repo
        collect_report = adapter.collect()
        assert collect_report.success
        assert "CLAUDE.md" in collect_report.files_synced

        repo_copy = repo_dir / "claude-code" / "CLAUDE.md"
        assert repo_copy.exists()
        assert repo_copy.read_text(encoding="utf-8") == original

        # Mutate local file to prove deploy overwrites it.
        (claude_dir / "CLAUDE.md").write_text("replaced", encoding="utf-8")

        # Deploy: repo → local
        deploy_report = adapter.deploy()
        assert deploy_report.success
        assert "CLAUDE.md" in deploy_report.files_synced
        assert (claude_dir / "CLAUDE.md").read_text(encoding="utf-8") == original


# ------------------------------------------------------------------ #
# T4: Secret exclusion — .credentials.json
# ------------------------------------------------------------------ #


class TestExclusions:
    def test_credentials_never_collected(
        self, adapter: ClaudeCodeAdapter, claude_dir: Path, repo_dir: Path
    ) -> None:
        # Place a .credentials.json alongside CLAUDE.md.
        (claude_dir / "CLAUDE.md").write_text("ok", encoding="utf-8")
        (claude_dir / ".credentials.json").write_text(
            '{"secret": "top-secret"}', encoding="utf-8"
        )

        report = adapter.collect()
        assert report.success

        # .credentials.json must NOT appear in synced or repo tree.
        repo_sub = repo_dir / "claude-code"
        all_repo_files = [
            str(f.relative_to(repo_sub)) for f in repo_sub.rglob("*") if f.is_file()
        ]
        assert ".credentials.json" not in all_repo_files
        assert ".credentials.json" not in report.files_synced

    def test_exclusion_list_contains_known_paths(
        self, adapter: ClaudeCodeAdapter
    ) -> None:
        exclusions = adapter.get_exclusions()
        for expected in [
            ".credentials.json",
            "sessions",
            "cache",
            "telemetry",
            "plugins/cache",
            "plugins/data",
        ]:
            assert expected in exclusions


# ------------------------------------------------------------------ #
# T5: Partial sync — settings.json
# ------------------------------------------------------------------ #


class TestPartialSync:
    def test_only_declared_keys_collected(
        self, adapter: ClaudeCodeAdapter, claude_dir: Path, repo_dir: Path
    ) -> None:
        local_settings = {
            "enabledPlugins": ["superpowers"],
            "effortLevel": "high",
            "theme": "dark",
            "customUserKey": 42,
        }
        (claude_dir / "settings.json").write_text(
            json.dumps(local_settings), encoding="utf-8"
        )

        report = adapter.collect()
        assert report.success
        assert "settings.json" in report.files_synced

        repo_settings = json.loads(
            (repo_dir / "claude-code" / "settings.json").read_text(encoding="utf-8")
        )
        # Only the declared keys should be present.
        assert set(repo_settings.keys()) == {"enabledPlugins", "effortLevel"}
        assert repo_settings["enabledPlugins"] == ["superpowers"]
        assert repo_settings["effortLevel"] == "high"

    def test_deploy_merges_into_existing_settings(
        self, adapter: ClaudeCodeAdapter, claude_dir: Path, repo_dir: Path
    ) -> None:
        # Existing local settings with extra keys we do NOT want to lose.
        local_settings = {
            "enabledPlugins": ["old-plugin"],
            "effortLevel": "low",
            "theme": "dark",
        }
        (claude_dir / "settings.json").write_text(
            json.dumps(local_settings), encoding="utf-8"
        )

        # Repo has updated values for the synced keys only.
        repo_sub = repo_dir / "claude-code"
        repo_sub.mkdir(parents=True, exist_ok=True)
        repo_data = {"enabledPlugins": ["superpowers"], "effortLevel": "high"}
        (repo_sub / "settings.json").write_text(
            json.dumps(repo_data), encoding="utf-8"
        )

        report = adapter.deploy()
        assert report.success

        merged = json.loads(
            (claude_dir / "settings.json").read_text(encoding="utf-8")
        )
        # Declared keys are updated.
        assert merged["enabledPlugins"] == ["superpowers"]
        assert merged["effortLevel"] == "high"
        # Non-synced keys are preserved.
        assert merged["theme"] == "dark"


# ------------------------------------------------------------------ #
# T6: memory/ directory round-trip
# ------------------------------------------------------------------ #


class TestMemorySync:
    def test_memory_dir_round_trip(
        self, adapter: ClaudeCodeAdapter, claude_dir: Path, repo_dir: Path
    ) -> None:
        mem_dir = claude_dir / "memory"
        mem_dir.mkdir()
        (mem_dir / "note1.md").write_text("remember this", encoding="utf-8")
        (mem_dir / "note2.md").write_text("and this", encoding="utf-8")

        report = adapter.collect()
        assert report.success

        repo_mem = repo_dir / "claude-code" / "memory"
        assert repo_mem.is_dir()
        assert (repo_mem / "note1.md").read_text(encoding="utf-8") == "remember this"
        assert (repo_mem / "note2.md").read_text(encoding="utf-8") == "and this"


# ------------------------------------------------------------------ #
# T7: skills/ — only non-symlink dirs are collected
# ------------------------------------------------------------------ #


class TestSkillsSync:
    def test_direct_dirs_collected_symlinks_excluded(
        self, adapter: ClaudeCodeAdapter, claude_dir: Path, repo_dir: Path
    ) -> None:
        skills = claude_dir / "skills"
        skills.mkdir()

        # Direct directory — should be collected.
        real = skills / "read-arxiv-paper"
        real.mkdir()
        (real / "skill.md").write_text("paper reader", encoding="utf-8")

        # Symlink directory — should NOT be collected.
        target = claude_dir / "_targets" / "gstack-browse"
        target.mkdir(parents=True)
        (target / "skill.md").write_text("gstack", encoding="utf-8")
        (skills / "gstack-browse").symlink_to(target)

        report = adapter.collect()
        assert report.success

        repo_skills = repo_dir / "claude-code" / "skills"
        assert (repo_skills / "read-arxiv-paper" / "skill.md").exists()
        assert not (repo_skills / "gstack-browse").exists()

    def test_symlinks_reported_by_get_symlinks(
        self, adapter: ClaudeCodeAdapter, claude_dir: Path
    ) -> None:
        skills = claude_dir / "skills"
        skills.mkdir()

        target = claude_dir / "_targets" / "browse"
        target.mkdir(parents=True)
        (skills / "browse").symlink_to(target)

        symlinks = adapter.get_symlinks()
        assert len(symlinks) == 1
        assert symlinks[0].name == "skills/browse"


# ------------------------------------------------------------------ #
# T8: Diff detection
# ------------------------------------------------------------------ #


class TestDiff:
    def test_diff_detects_modified_file(
        self, adapter: ClaudeCodeAdapter, claude_dir: Path, repo_dir: Path
    ) -> None:
        (claude_dir / "CLAUDE.md").write_text("local version", encoding="utf-8")

        repo_sub = repo_dir / "claude-code"
        repo_sub.mkdir(parents=True, exist_ok=True)
        (repo_sub / "CLAUDE.md").write_text("repo version", encoding="utf-8")

        changes = adapter.diff()
        claude_changes = [c for c in changes if c.path == "CLAUDE.md"]
        assert len(claude_changes) == 1
        assert claude_changes[0].kind.value == "modified"

    def test_diff_detects_no_change(
        self, adapter: ClaudeCodeAdapter, claude_dir: Path, repo_dir: Path
    ) -> None:
        content = "same content"
        (claude_dir / "CLAUDE.md").write_text(content, encoding="utf-8")

        repo_sub = repo_dir / "claude-code"
        repo_sub.mkdir(parents=True, exist_ok=True)
        (repo_sub / "CLAUDE.md").write_text(content, encoding="utf-8")

        changes = adapter.diff()
        claude_changes = [c for c in changes if c.path == "CLAUDE.md"]
        assert len(claude_changes) == 0


# ------------------------------------------------------------------ #
# T9: Dependencies
# ------------------------------------------------------------------ #


class TestDependencies:
    def test_depends_on_shared(self, adapter: ClaudeCodeAdapter) -> None:
        assert adapter.get_dependencies() == ["shared"]


# ------------------------------------------------------------------ #
# T10: Sync rules metadata
# ------------------------------------------------------------------ #


class TestSyncRules:
    def test_settings_is_partial(
        self, adapter: ClaudeCodeAdapter, fake_home: Path
    ) -> None:
        rules = adapter.get_sync_rules()
        settings_rules = [r for r in rules if r.path == "settings.json"]
        assert len(settings_rules) == 1
        assert settings_rules[0].sync_type is SyncType.PARTIAL
        assert "enabledPlugins" in settings_rules[0].keys
        assert "effortLevel" in settings_rules[0].keys

    def test_claude_md_is_full(
        self, adapter: ClaudeCodeAdapter, fake_home: Path
    ) -> None:
        rules = adapter.get_sync_rules()
        md_rules = [r for r in rules if r.path == "CLAUDE.md"]
        assert len(md_rules) == 1
        assert md_rules[0].sync_type is SyncType.FULL
