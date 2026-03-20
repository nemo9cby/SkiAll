"""Tests for the SharedAdapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from skiall.adapters.shared import SharedAdapter
from skiall.core.types import ChangeKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up fake home with ~/.agents/skills/ and a repo dir."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # pathlib.Path.home() caches; override at class level
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    skills_dir = fake_home / ".agents" / "skills"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    return skills_dir, repo_dir


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------

class TestDetect:
    def test_detect_returns_false_when_missing(self, env):
        skills_dir, repo_dir = env
        adapter = SharedAdapter(repo_dir=repo_dir)
        assert adapter.detect() is False

    def test_detect_returns_true_when_present(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)
        adapter = SharedAdapter(repo_dir=repo_dir)
        assert adapter.detect() is True


# ---------------------------------------------------------------------------
# collect() / deploy() round-trip
# ---------------------------------------------------------------------------

class TestCollectDeploy:
    def test_collect_copies_skills_to_repo(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)

        # Create a skill with nested files
        skill = skills_dir / "my-skill"
        skill.mkdir()
        (skill / "index.js").write_text("console.log('hello');")
        (skill / "README.md").write_text("# My Skill")

        adapter = SharedAdapter(repo_dir=repo_dir)
        report = adapter.collect()

        assert report.success
        assert "my-skill" in report.files_synced

        repo_skill = repo_dir / "shared" / "my-skill"
        assert repo_skill.is_dir()
        assert (repo_skill / "index.js").read_text() == "console.log('hello');"
        assert (repo_skill / "README.md").read_text() == "# My Skill"

    def test_deploy_copies_skills_from_repo(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)

        # Populate repo
        repo_shared = repo_dir / "shared" / "another-skill"
        repo_shared.mkdir(parents=True)
        (repo_shared / "main.py").write_text("print('hi')")

        adapter = SharedAdapter(repo_dir=repo_dir)
        report = adapter.deploy()

        assert report.success
        deployed = skills_dir / "another-skill" / "main.py"
        assert deployed.is_file()
        assert deployed.read_text() == "print('hi')"

    def test_round_trip_preserves_content(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)

        # Create skills
        (skills_dir / "alpha").mkdir()
        (skills_dir / "alpha" / "a.txt").write_text("aaa")
        (skills_dir / "beta").mkdir()
        (skills_dir / "beta" / "b.txt").write_text("bbb")

        adapter = SharedAdapter(repo_dir=repo_dir)

        # Collect
        collect_report = adapter.collect()
        assert collect_report.success

        # Wipe local and deploy
        import shutil
        shutil.rmtree(skills_dir)
        skills_dir.mkdir(parents=True)

        deploy_report = adapter.deploy()
        assert deploy_report.success

        assert (skills_dir / "alpha" / "a.txt").read_text() == "aaa"
        assert (skills_dir / "beta" / "b.txt").read_text() == "bbb"

    def test_collect_removes_stale_repo_entries(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)

        # First collect with two skills
        (skills_dir / "keep").mkdir()
        (skills_dir / "keep" / "k.txt").write_text("keep")
        (skills_dir / "remove").mkdir()
        (skills_dir / "remove" / "r.txt").write_text("remove")

        adapter = SharedAdapter(repo_dir=repo_dir)
        adapter.collect()

        # Remove one locally, collect again
        import shutil
        shutil.rmtree(skills_dir / "remove")

        report = adapter.collect()
        assert report.success
        assert not (repo_dir / "shared" / "remove").exists()

    def test_deploy_removes_stale_local_entries(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)

        # Local has a stale skill
        (skills_dir / "stale").mkdir()
        (skills_dir / "stale" / "x.txt").write_text("old")

        # Repo has a different skill
        repo_shared = repo_dir / "shared" / "fresh"
        repo_shared.mkdir(parents=True)
        (repo_shared / "y.txt").write_text("new")

        adapter = SharedAdapter(repo_dir=repo_dir)
        report = adapter.deploy()

        assert report.success
        assert not (skills_dir / "stale").exists()
        assert (skills_dir / "fresh" / "y.txt").read_text() == "new"

    def test_collect_error_when_dir_missing(self, env):
        _skills_dir, repo_dir = env
        adapter = SharedAdapter(repo_dir=repo_dir)
        report = adapter.collect()
        assert not report.success
        assert any("not found" in e for e in report.errors)

    def test_deploy_error_when_repo_dir_missing(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)
        adapter = SharedAdapter(repo_dir=repo_dir)
        report = adapter.deploy()
        assert not report.success
        assert any("not found" in e for e in report.errors)


# ---------------------------------------------------------------------------
# diff()
# ---------------------------------------------------------------------------

class TestDiff:
    def test_diff_no_changes(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)
        (skills_dir / "s1").mkdir()
        (skills_dir / "s1" / "f.txt").write_text("same")

        adapter = SharedAdapter(repo_dir=repo_dir)
        adapter.collect()

        changes = adapter.diff()
        assert changes == []

    def test_diff_detects_added_local_file(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)

        adapter = SharedAdapter(repo_dir=repo_dir)

        # Collect empty, then add a local file
        adapter.collect()
        (skills_dir / "new-skill").mkdir()
        (skills_dir / "new-skill" / "n.txt").write_text("new")

        changes = adapter.diff()
        added = [c for c in changes if c.kind == ChangeKind.ADDED]
        assert len(added) == 1
        assert "new-skill" in added[0].path

    def test_diff_detects_deleted_local_file(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)
        (skills_dir / "gone").mkdir()
        (skills_dir / "gone" / "g.txt").write_text("bye")

        adapter = SharedAdapter(repo_dir=repo_dir)
        adapter.collect()

        # Remove locally
        import shutil
        shutil.rmtree(skills_dir / "gone")

        changes = adapter.diff()
        deleted = [c for c in changes if c.kind == ChangeKind.DELETED]
        assert len(deleted) == 1

    def test_diff_detects_modified_file(self, env):
        skills_dir, repo_dir = env
        skills_dir.mkdir(parents=True)
        (skills_dir / "mod").mkdir()
        (skills_dir / "mod" / "m.txt").write_text("v1")

        adapter = SharedAdapter(repo_dir=repo_dir)
        adapter.collect()

        (skills_dir / "mod" / "m.txt").write_text("v2")

        changes = adapter.diff()
        modified = [c for c in changes if c.kind == ChangeKind.MODIFIED]
        assert len(modified) == 1

    def test_diff_empty_when_both_missing(self, env):
        _skills_dir, repo_dir = env
        adapter = SharedAdapter(repo_dir=repo_dir)
        changes = adapter.diff()
        assert changes == []
