"""Tests for the CodexAdapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from skiall.adapters.codex import CodexAdapter, _is_excluded, _parse_toml, _dump_toml, _filter_config
from skiall.core.types import ChangeKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up fake home with ~/.codex/ and a repo dir."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    codex_dir = fake_home / ".codex"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    return fake_home, codex_dir, repo_dir


SAMPLE_CONFIG = """\
model = "o3"
model_reasoning_effort = "high"

[personality]
style = "concise"
tone = "friendly"

[features]
sandbox = true

[projects]
"/home/user/project-a" = {model = "o4-mini"}
"/home/user/project-b" = {sandbox = false}

[notice]
model_migrations = "2025-01-01"
seen_welcome = true
"""


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------

class TestDetect:
    def test_detect_returns_false_when_missing(self, env):
        _home, _codex_dir, repo_dir = env
        adapter = CodexAdapter(repo_dir=repo_dir)
        assert adapter.detect() is False

    def test_detect_returns_true_when_present(self, env):
        _home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)
        adapter = CodexAdapter(repo_dir=repo_dir)
        assert adapter.detect() is True


# ---------------------------------------------------------------------------
# Partial sync for config.toml
# ---------------------------------------------------------------------------

class TestConfigPartialSync:
    def test_collect_filters_config_keys(self, env):
        _home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text(SAMPLE_CONFIG)

        adapter = CodexAdapter(repo_dir=repo_dir)
        report = adapter.collect()

        assert report.success
        assert "config.toml" in report.files_synced

        # Read the collected config
        collected = (repo_dir / "codex" / "config.toml").read_text()
        parsed = _parse_toml(collected)

        # Synced keys should be present
        assert parsed["model"] == "o3"
        assert parsed["model_reasoning_effort"] == "high"
        assert "personality" in parsed
        assert "features" in parsed

        # Excluded keys/sections should be absent
        assert "projects" not in parsed
        # notice section should exist but without model_migrations
        if "notice" in parsed:
            notice = parsed["notice"]
            assert isinstance(notice, dict)
            assert "model_migrations" not in notice

    def test_deploy_merges_config_preserving_local_sections(self, env):
        _home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)

        # Existing local config with projects section
        (codex_dir / "config.toml").write_text(SAMPLE_CONFIG)

        # Repo config with different model
        repo_codex = repo_dir / "codex"
        repo_codex.mkdir(parents=True)
        (repo_codex / "config.toml").write_text(
            'model = "o4"\nmodel_reasoning_effort = "medium"\n'
        )

        adapter = CodexAdapter(repo_dir=repo_dir)
        report = adapter.deploy()

        assert report.success
        assert "config.toml" in report.files_synced

        # Read the deployed config — should have merged values
        deployed = _parse_toml((codex_dir / "config.toml").read_text())
        assert deployed["model"] == "o4"
        assert deployed["model_reasoning_effort"] == "medium"

        # The [projects] section from the local config should be preserved
        assert "projects" in deployed

    def test_config_round_trip(self, env):
        _home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text(SAMPLE_CONFIG)

        adapter = CodexAdapter(repo_dir=repo_dir)
        adapter.collect()

        # Wipe config, deploy
        (codex_dir / "config.toml").unlink()
        adapter.deploy()

        deployed = _parse_toml((codex_dir / "config.toml").read_text())
        assert deployed["model"] == "o3"
        assert deployed["model_reasoning_effort"] == "high"
        # Projects should NOT come back (they were filtered on collect)
        assert "projects" not in deployed


# ---------------------------------------------------------------------------
# AGENTS.md sync
# ---------------------------------------------------------------------------

class TestAgentsMd:
    def test_collect_copies_agents_md(self, env):
        home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)
        agents = home / "AGENTS.md"
        agents.write_text("# My Agent Rules\nBe helpful.\n")

        adapter = CodexAdapter(repo_dir=repo_dir)
        report = adapter.collect()

        assert report.success
        assert "AGENTS.md" in report.files_synced
        assert (repo_dir / "codex" / "AGENTS.md").read_text() == "# My Agent Rules\nBe helpful.\n"

    def test_deploy_restores_agents_md(self, env):
        home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)

        repo_codex = repo_dir / "codex"
        repo_codex.mkdir(parents=True)
        (repo_codex / "AGENTS.md").write_text("# Deployed Rules\n")

        adapter = CodexAdapter(repo_dir=repo_dir)
        report = adapter.deploy()

        assert report.success
        assert "AGENTS.md" in report.files_synced
        assert (home / "AGENTS.md").read_text() == "# Deployed Rules\n"

    def test_agents_md_round_trip(self, env):
        home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)
        original = "# Round Trip Test\nLine 2\n"
        (home / "AGENTS.md").write_text(original)

        adapter = CodexAdapter(repo_dir=repo_dir)
        adapter.collect()

        # Remove local, deploy
        (home / "AGENTS.md").unlink()
        adapter.deploy()

        assert (home / "AGENTS.md").read_text() == original

    def test_collect_skips_missing_agents_md(self, env):
        _home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text('model = "o3"\n')

        adapter = CodexAdapter(repo_dir=repo_dir)
        report = adapter.collect()

        assert report.success
        assert any("AGENTS.md" in s for s in report.files_skipped)


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------

class TestExclusions:
    @pytest.mark.parametrize(
        "rel_path",
        [
            "auth.json",
            "history.jsonl",
            "sessions/abc123.json",
            "cache/models.bin",
            "log/2025-01-01.log",
            "logs_main.sqlite",
            "state_main.sqlite",
            "models_cache.json",
            "shell_snapshots/snap1.json",
            "tmp/scratch.txt",
            "version.json",
            ".personality_migration",
            "skills/.system/builtin.js",
        ],
    )
    def test_excluded_paths_are_detected(self, rel_path: str):
        assert _is_excluded(rel_path), f"{rel_path} should be excluded"

    @pytest.mark.parametrize(
        "rel_path",
        [
            "config.toml",
            "memories/project-a.md",
            "skills/my-skill/index.js",
        ],
    )
    def test_allowed_paths_are_not_excluded(self, rel_path: str):
        assert not _is_excluded(rel_path), f"{rel_path} should NOT be excluded"

    def test_collect_skips_excluded_files(self, env):
        _home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)

        # Create syncable and excluded files
        (codex_dir / "config.toml").write_text('model = "o3"\n')
        (codex_dir / "memories").mkdir()
        (codex_dir / "memories" / "project.md").write_text("memory")
        (codex_dir / "auth.json").write_text('{"token": "secret"}')
        (codex_dir / "history.jsonl").write_text('{"line": 1}')
        (codex_dir / "sessions").mkdir()
        (codex_dir / "sessions" / "s1.json").write_text("{}")
        (codex_dir / "version.json").write_text('{"v": 1}')

        adapter = CodexAdapter(repo_dir=repo_dir)
        report = adapter.collect()

        assert report.success
        # Excluded files should be in skipped list
        skipped_rels = report.files_skipped
        assert "auth.json" in skipped_rels
        assert "history.jsonl" in skipped_rels

        # Excluded files should NOT be in repo
        repo_codex = repo_dir / "codex"
        assert not (repo_codex / "auth.json").exists()
        assert not (repo_codex / "history.jsonl").exists()
        assert not (repo_codex / "sessions").exists()
        assert not (repo_codex / "version.json").exists()

        # Syncable files should be present
        assert (repo_codex / "config.toml").exists()
        assert (repo_codex / "memories" / "project.md").exists()


# ---------------------------------------------------------------------------
# Memories full sync
# ---------------------------------------------------------------------------

class TestMemories:
    def test_memories_round_trip(self, env):
        _home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)
        memories = codex_dir / "memories"
        memories.mkdir()
        (memories / "proj-a.md").write_text("context A")
        (memories / "proj-b.md").write_text("context B")

        adapter = CodexAdapter(repo_dir=repo_dir)
        adapter.collect()

        # Verify collected
        assert (repo_dir / "codex" / "memories" / "proj-a.md").read_text() == "context A"

        # Wipe and deploy
        import shutil
        shutil.rmtree(memories)

        adapter.deploy()

        assert (memories / "proj-a.md").read_text() == "context A"
        assert (memories / "proj-b.md").read_text() == "context B"


# ---------------------------------------------------------------------------
# diff()
# ---------------------------------------------------------------------------

class TestDiff:
    def test_diff_no_changes_after_collect(self, env):
        _home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text('model = "o3"\n')

        adapter = CodexAdapter(repo_dir=repo_dir)
        adapter.collect()

        changes = adapter.diff()
        assert changes == []

    def test_diff_detects_modified_config(self, env):
        _home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text('model = "o3"\n')

        adapter = CodexAdapter(repo_dir=repo_dir)
        adapter.collect()

        # Change model locally
        (codex_dir / "config.toml").write_text('model = "o4"\n')

        changes = adapter.diff()
        modified = [c for c in changes if c.kind == ChangeKind.MODIFIED]
        assert len(modified) == 1
        assert modified[0].path == "config.toml"

    def test_diff_ignores_excluded_files(self, env):
        _home, codex_dir, repo_dir = env
        codex_dir.mkdir(parents=True)

        adapter = CodexAdapter(repo_dir=repo_dir)
        adapter.collect()

        # Add an excluded file locally
        (codex_dir / "auth.json").write_text('{"secret": true}')

        changes = adapter.diff()
        # auth.json should NOT appear in diff
        paths = [c.path for c in changes]
        assert "auth.json" not in paths


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

class TestDependencies:
    def test_depends_on_shared(self, env):
        _home, _codex_dir, repo_dir = env
        adapter = CodexAdapter(repo_dir=repo_dir)
        assert adapter.get_dependencies() == ["shared"]


# ---------------------------------------------------------------------------
# TOML helpers
# ---------------------------------------------------------------------------

class TestTomlHelpers:
    def test_parse_and_dump_round_trip(self):
        toml_text = 'model = "gpt-4"\ncount = 42\nenabled = true\n'
        parsed = _parse_toml(toml_text)
        assert parsed["model"] == "gpt-4"
        assert parsed["count"] == 42
        assert parsed["enabled"] is True

        dumped = _dump_toml(parsed)
        reparsed = _parse_toml(dumped)
        assert reparsed == parsed

    def test_filter_config_keeps_correct_keys(self):
        parsed = _parse_toml(SAMPLE_CONFIG)
        filtered = _filter_config(parsed)

        assert "model" in filtered
        assert "model_reasoning_effort" in filtered
        assert "personality" in filtered
        assert "features" in filtered
        assert "projects" not in filtered
        if "notice" in filtered:
            assert "model_migrations" not in filtered["notice"]

    def test_parse_inline_array(self):
        toml_text = 'tags = ["a", "b", "c"]\n'
        parsed = _parse_toml(toml_text)
        assert parsed["tags"] == ["a", "b", "c"]

    def test_parse_inline_table(self):
        toml_text = 'opts = {key = "val", num = 5}\n'
        parsed = _parse_toml(toml_text)
        assert parsed["opts"] == {"key": "val", "num": 5}
