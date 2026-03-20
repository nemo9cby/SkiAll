"""Tests for the CLI commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from skiall.cli import cli


class TestCLI:
    def test_status_no_repo(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["--repo-dir", str(tmp_path / "nope"), "status"])
        assert result.exit_code != 0
        assert "No SkiAll repo found" in result.output

    def test_pull_no_repo(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["--repo-dir", str(tmp_path / "nope"), "pull"])
        assert result.exit_code != 0

    def test_push_no_repo(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["--repo-dir", str(tmp_path / "nope"), "push"])
        assert result.exit_code != 0

    def test_setup_creates_repo(self, tmp_path: Path):
        repo_dir = tmp_path / "test-repo"
        runner = CliRunner()
        result = runner.invoke(cli, ["--repo-dir", str(repo_dir), "setup"])
        assert result.exit_code == 0
        assert "Initializing" in result.output
        assert (repo_dir / "skiall.yaml").exists()
