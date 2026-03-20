"""Tests for skiall.core.secrets -- triple-layer secret exclusion."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from skiall.core.secrets import (
    EXCLUDED_FILENAMES,
    EXCLUDED_PATTERNS,
    SENSITIVE_KEY_PATTERNS,
    generate_gitignore,
    generate_pre_commit_hook,
    install_pre_commit_hook,
    is_excluded,
    scan_for_secrets,
)


# =========================================================================
# T6: is_excluded -- hardcoded exclusion checks
# =========================================================================


class TestIsExcluded:
    """T6: Attempt to check .credentials.json -> is_excluded returns True."""

    def test_credentials_json_excluded(self) -> None:
        assert is_excluded(".credentials.json") is True

    def test_credentials_json_nested_path(self) -> None:
        assert is_excluded("~/.claude/.credentials.json") is True

    def test_auth_json_excluded(self) -> None:
        assert is_excluded("auth.json") is True

    def test_auth_toml_excluded(self) -> None:
        assert is_excluded("auth.toml") is True

    def test_auth_json_nested(self) -> None:
        assert is_excluded("/home/user/.codex/auth.json") is True

    # -- Pattern-based exclusions --

    def test_key_file_excluded(self) -> None:
        assert is_excluded("server.key") is True

    def test_pem_file_excluded(self) -> None:
        assert is_excluded("cert.pem") is True

    def test_credentials_in_name(self) -> None:
        assert is_excluded("my_credentials_backup.yaml") is True

    def test_secret_in_name(self) -> None:
        assert is_excluded("app_secret.env") is True

    def test_token_in_name(self) -> None:
        assert is_excluded("github_token.txt") is True

    # -- Case insensitivity of patterns --

    def test_case_insensitive_key(self) -> None:
        assert is_excluded("Server.KEY") is True

    def test_case_insensitive_pem(self) -> None:
        assert is_excluded("CERT.PEM") is True

    def test_case_insensitive_secret(self) -> None:
        assert is_excluded("MySecret.yml") is True

    # -- Non-excluded files --

    def test_normal_file_not_excluded(self) -> None:
        assert is_excluded("settings.json") is False

    def test_readme_not_excluded(self) -> None:
        assert is_excluded("README.md") is False

    def test_python_file_not_excluded(self) -> None:
        assert is_excluded("main.py") is False

    def test_partial_match_not_false_positive(self) -> None:
        """'keynote.txt' should not match '*.key'."""
        assert is_excluded("keynote.txt") is False

    def test_path_object_accepted(self) -> None:
        assert is_excluded(Path("/some/dir/.credentials.json")) is True

    def test_empty_string(self) -> None:
        assert is_excluded("") is False


# =========================================================================
# T7: scan_for_secrets -- content scanning
# =========================================================================


class TestScanForSecrets:
    """T7: scan_for_secrets catches content with API keys."""

    def test_json_api_key(self) -> None:
        content = '{"api_key": "sk-abc123xyz456"}'
        warnings = scan_for_secrets(content)
        assert len(warnings) >= 1
        assert "api_key" in warnings[0].lower() or "secret" in warnings[0].lower()

    def test_toml_password(self) -> None:
        content = 'database_password = "hunter2"'
        warnings = scan_for_secrets(content)
        assert len(warnings) >= 1

    def test_yaml_token(self) -> None:
        content = "auth_token: ghp_abcdef1234567890"
        warnings = scan_for_secrets(content)
        assert len(warnings) >= 1

    def test_env_secret(self) -> None:
        content = 'APP_SECRET="very-long-secret-value"'
        warnings = scan_for_secrets(content)
        assert len(warnings) >= 1

    def test_credential_key(self) -> None:
        content = '"credential": "some-credential-value"'
        warnings = scan_for_secrets(content)
        assert len(warnings) >= 1

    def test_clean_content_no_warnings(self) -> None:
        content = textwrap.dedent("""\
            name = "skiall"
            version = "0.1.0"
            description = "A tool for syncing configs"
        """)
        warnings = scan_for_secrets(content)
        assert warnings == []

    def test_placeholder_values_ignored(self) -> None:
        content = '"api_key": "changeme"'
        warnings = scan_for_secrets(content)
        assert warnings == []

    def test_short_values_ignored(self) -> None:
        """Values shorter than 4 characters are treated as non-secrets."""
        content = '"api_key": "no"'
        warnings = scan_for_secrets(content)
        assert warnings == []

    def test_multiline_content(self) -> None:
        content = textwrap.dedent("""\
            [database]
            host = "localhost"
            port = 5432
            password = "super-secret-pass"
            name = "mydb"
        """)
        warnings = scan_for_secrets(content)
        assert len(warnings) == 1
        assert "Line 4" in warnings[0]

    def test_case_insensitive_key_detection(self) -> None:
        content = '"API_KEY": "sk-real-key-value-here"'
        warnings = scan_for_secrets(content)
        assert len(warnings) >= 1

    def test_python_dict_style(self) -> None:
        content = "secret_key = 'django-insecure-abcdef123'"
        warnings = scan_for_secrets(content)
        assert len(warnings) >= 1

    def test_empty_content(self) -> None:
        assert scan_for_secrets("") == []

    def test_comment_lines_still_scanned(self) -> None:
        """Even commented-out secrets should be flagged."""
        content = '# api_key = "leaked-key-in-comment"'
        warnings = scan_for_secrets(content)
        assert len(warnings) >= 1


# =========================================================================
# gitignore generation
# =========================================================================


class TestGenerateGitignore:
    def test_contains_all_excluded_filenames(self) -> None:
        gitignore = generate_gitignore()
        for name in EXCLUDED_FILENAMES:
            assert name in gitignore, f"Missing filename {name!r}"

    def test_contains_all_excluded_patterns(self) -> None:
        gitignore = generate_gitignore()
        for pattern in EXCLUDED_PATTERNS:
            assert pattern in gitignore, f"Missing pattern {pattern!r}"

    def test_contains_env_exclusion(self) -> None:
        gitignore = generate_gitignore()
        assert ".env" in gitignore

    def test_ends_with_newline(self) -> None:
        gitignore = generate_gitignore()
        assert gitignore.endswith("\n")

    def test_contains_header_comment(self) -> None:
        gitignore = generate_gitignore()
        assert "SkiAll" in gitignore


# =========================================================================
# Pre-commit hook
# =========================================================================


class TestGeneratePreCommitHook:
    def test_starts_with_shebang(self) -> None:
        hook = generate_pre_commit_hook()
        assert hook.startswith("#!/usr/bin/env bash")

    def test_contains_set_flags(self) -> None:
        hook = generate_pre_commit_hook()
        assert "set -euo pipefail" in hook

    def test_does_not_scan_content(self) -> None:
        """Hook should NOT grep file content — too many false positives."""
        hook = generate_pre_commit_hook()
        assert "scan_content" not in hook
        assert "grep -iEq" not in hook

    def test_contains_excluded_filenames(self) -> None:
        hook = generate_pre_commit_hook()
        for name in EXCLUDED_FILENAMES:
            assert name in hook, f"Missing filename {name!r}"

    def test_exits_nonzero_on_block(self) -> None:
        hook = generate_pre_commit_hook()
        assert "exit 1" in hook

    def test_exits_zero_on_success(self) -> None:
        hook = generate_pre_commit_hook()
        assert "exit 0" in hook

    def test_is_valid_bash_syntax(self, tmp_path: Path) -> None:
        """Verify the generated hook passes bash -n syntax check."""
        hook_file = tmp_path / "pre-commit"
        hook_file.write_text(generate_pre_commit_hook())
        # Quote the path to handle spaces in Windows usernames
        result = os.popen(f'bash -n "{hook_file}" 2>&1').read()
        assert result == "", f"Bash syntax errors:\n{result}"


# =========================================================================
# install_pre_commit_hook
# =========================================================================


class TestInstallPreCommitHook:
    def test_install_creates_hook(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)

        install_pre_commit_hook(tmp_path)

        hook_path = hooks_dir / "pre-commit"
        assert hook_path.exists()
        content = hook_path.read_text()
        assert "SkiAll pre-commit hook" in content

    def test_hook_is_executable(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)

        install_pre_commit_hook(tmp_path)

        hook_path = hooks_dir / "pre-commit"
        if sys.platform == "win32":
            # Windows doesn't use Unix file permissions — just check it exists
            assert hook_path.exists()
        else:
            mode = hook_path.stat().st_mode
            assert mode & stat.S_IXUSR, "Hook should be executable by owner"

    def test_overwrites_skiall_hook(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook_path = hooks_dir / "pre-commit"
        hook_path.write_text("#!/bin/bash\n# SkiAll pre-commit hook\necho old\n")

        install_pre_commit_hook(tmp_path)

        content = hook_path.read_text()
        assert "echo old" not in content
        assert "SkiAll pre-commit hook" in content

    def test_refuses_to_overwrite_foreign_hook(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook_path = hooks_dir / "pre-commit"
        hook_path.write_text("#!/bin/bash\necho 'my custom hook'\n")

        with pytest.raises(FileExistsError, match="not generated by SkiAll"):
            install_pre_commit_hook(tmp_path)

    def test_raises_on_missing_git_dir(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="No .git/hooks"):
            install_pre_commit_hook(tmp_path)
