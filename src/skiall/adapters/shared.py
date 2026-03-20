"""Shared adapter — manages ~/.agents/skills/ cross-platform skills directory.

This directory is used by both Claude Code and Codex via symlinks.
It syncs all subdirectories with no exclusions, merge rules, or plugins.
"""

from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

from skiall.adapters.base import BaseAdapter
from skiall.core.types import (
    Change,
    ChangeKind,
    PlatformPaths,
    SyncReport,
    SyncRule,
    SyncType,
)


class SharedAdapter(BaseAdapter):
    """Adapter for the shared skills directory (~/.agents/skills/)."""

    @property
    def name(self) -> str:
        return "shared"

    # --- Discovery ---

    def detect(self) -> bool:
        """Return True if ~/.agents/skills/ exists."""
        return self._skills_dir().is_dir()

    def get_paths(self) -> PlatformPaths:
        return PlatformPaths(config_dir=self._skills_dir())

    # --- Sync ---

    def get_sync_rules(self) -> list[SyncRule]:
        return [SyncRule(path=".", sync_type=SyncType.FULL)]

    def collect(self) -> SyncReport:
        """Copy all skill directories from ~/.agents/skills/ into repo."""
        report = SyncReport(adapter_name=self.name)
        skills_dir = self._skills_dir()
        target = self._repo_subdir()

        if not skills_dir.is_dir():
            report.errors.append(f"Skills directory not found: {skills_dir}")
            return report

        target.mkdir(parents=True, exist_ok=True)

        # Remove repo entries that no longer exist locally
        if target.is_dir():
            for existing in sorted(target.iterdir()):
                rel = existing.relative_to(target)
                local_path = skills_dir / rel
                if not local_path.exists():
                    if existing.is_dir():
                        shutil.rmtree(existing)
                    else:
                        existing.unlink()
                    report.files_synced.append(f"removed {rel}")

        # Copy everything from skills_dir into repo
        for entry in sorted(skills_dir.iterdir()):
            rel = entry.relative_to(skills_dir)
            dest = target / rel
            try:
                if entry.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(entry, dest)
                else:
                    shutil.copy2(entry, dest)
                report.files_synced.append(str(rel))
            except OSError as exc:
                report.errors.append(f"Failed to copy {rel}: {exc}")

        return report

    def deploy(self) -> SyncReport:
        """Copy from repo/shared/ to ~/.agents/skills/."""
        report = SyncReport(adapter_name=self.name)
        source = self._repo_subdir()
        skills_dir = self._skills_dir()

        if not source.is_dir():
            report.errors.append(f"Repo shared directory not found: {source}")
            return report

        skills_dir.mkdir(parents=True, exist_ok=True)

        # Remove local entries that no longer exist in repo
        for existing in sorted(skills_dir.iterdir()):
            rel = existing.relative_to(skills_dir)
            repo_path = source / rel
            if not repo_path.exists():
                if existing.is_dir():
                    shutil.rmtree(existing)
                else:
                    existing.unlink()
                report.files_synced.append(f"removed {rel}")

        # Copy everything from repo into skills_dir
        for entry in sorted(source.iterdir()):
            rel = entry.relative_to(source)
            dest = skills_dir / rel
            try:
                if entry.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(entry, dest)
                else:
                    shutil.copy2(entry, dest)
                report.files_synced.append(str(rel))
            except OSError as exc:
                report.errors.append(f"Failed to deploy {rel}: {exc}")

        return report

    def diff(self) -> list[Change]:
        """Compare ~/.agents/skills/ against repo/shared/."""
        changes: list[Change] = []
        skills_dir = self._skills_dir()
        repo = self._repo_subdir()

        if not skills_dir.is_dir() and not repo.is_dir():
            return changes

        local_entries = _list_relative(skills_dir) if skills_dir.is_dir() else set()
        repo_entries = _list_relative(repo) if repo.is_dir() else set()

        for rel in sorted(local_entries - repo_entries):
            changes.append(Change(path=str(rel), kind=ChangeKind.ADDED, detail="exists locally but not in repo"))

        for rel in sorted(repo_entries - local_entries):
            changes.append(Change(path=str(rel), kind=ChangeKind.DELETED, detail="exists in repo but not locally"))

        for rel in sorted(local_entries & repo_entries):
            local_file = skills_dir / rel
            repo_file = repo / rel
            if local_file.is_file() and repo_file.is_file():
                if not filecmp.cmp(local_file, repo_file, shallow=False):
                    changes.append(Change(path=str(rel), kind=ChangeKind.MODIFIED, detail="content differs"))

        return changes

    # --- Info ---

    def info(self) -> dict:
        """Return detailed inventory of the shared skills directory."""
        result = super().info()
        if not self.detect():
            return result

        skills_dir = self._skills_dir()
        skills = []
        for child in sorted(skills_dir.iterdir()):
            entry = {"name": child.name}
            if child.is_symlink():
                entry["type"] = "symlink"
                entry["target"] = str(child.resolve())
            elif child.is_dir():
                entry["type"] = "directory"
                entry["files"] = sum(1 for _ in child.rglob("*") if _.is_file())
            else:
                entry["type"] = "file"
            skills.append(entry)
        result["skills"] = skills
        return result

    # --- Internals ---

    def _skills_dir(self) -> Path:
        return Path.home() / ".agents" / "skills"

    def _repo_subdir(self) -> Path:
        return self.repo_dir / "shared"


def _list_relative(root: Path) -> set[Path]:
    """Recursively list all files under *root* as relative paths."""
    if not root.is_dir():
        return set()
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}
