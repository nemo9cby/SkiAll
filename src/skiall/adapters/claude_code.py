"""Claude Code adapter — syncs ~/.claude/ config to the SkiAll repo.

Managed paths:
  - CLAUDE.md                        (full sync)
  - settings.json                    (partial sync: enabledPlugins, effortLevel)
  - memory/                          (full sync, all files)
  - skills/  (direct dirs only)      (full sync — symlinks excluded)
  - plugins/installed_plugins.json   (full sync — manifest only)

Exclusions (hardcoded, NEVER synced):
  .credentials.json, history.jsonl, sessions/, cache/, debug/,
  paste-cache/, file-history/, shell-snapshots/, session-env/,
  stats-cache.json, statsig/, telemetry/, backups/, downloads/,
  usage-data/, plugins/cache/, plugins/data/, plugins/blocklist.json,
  plugins/install-counts-cache.json, plugins/known_marketplaces.json,
  plugins/marketplaces/
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from skiall.adapters.base import BaseAdapter
from skiall.core.types import (
    Change,
    ChangeKind,
    PlatformPaths,
    Symlink,
    SyncReport,
    SyncRule,
    SyncType,
    normalized_bytes,
)

# Keys we extract from settings.json during partial sync.
_SETTINGS_KEYS: list[str] = ["enabledPlugins", "effortLevel"]

# Paths (relative to ~/.claude/) that are NEVER synced.
_EXCLUDED_PATHS: list[str] = [
    ".credentials.json",
    "history.jsonl",
    "sessions",
    "cache",
    "debug",
    "paste-cache",
    "file-history",
    "shell-snapshots",
    "session-env",
    "stats-cache.json",
    "statsig",
    "telemetry",
    "backups",
    "downloads",
    "usage-data",
    "plugins/cache",
    "plugins/data",
    "plugins/blocklist.json",
    "plugins/install-counts-cache.json",
    "plugins/known_marketplaces.json",
    "plugins/marketplaces",
]


class ClaudeCodeAdapter(BaseAdapter):
    """Adapter for the Claude Code CLI (~/.claude/)."""

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #

    @property
    def name(self) -> str:
        return "claude-code"

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    def detect(self) -> bool:
        """Return True if ~/.claude/ exists."""
        return self.get_paths().config_dir.is_dir()

    def get_paths(self) -> PlatformPaths:
        return PlatformPaths(config_dir=Path.home() / ".claude")

    # ------------------------------------------------------------------ #
    # Sync metadata
    # ------------------------------------------------------------------ #

    def get_sync_rules(self) -> list[SyncRule]:
        rules: list[SyncRule] = [
            SyncRule(path="CLAUDE.md", sync_type=SyncType.FULL),
            SyncRule(
                path="settings.json",
                sync_type=SyncType.PARTIAL,
                keys=list(_SETTINGS_KEYS),
            ),
            SyncRule(path="memory", sync_type=SyncType.FULL),
            SyncRule(path="plugins/installed_plugins.json", sync_type=SyncType.FULL),
        ]
        # skills/ — union of local direct dirs + repo skill dirs (for fresh machines)
        skill_names: set[str] = set()
        skills_dir = self.get_paths().config_dir / "skills"
        if skills_dir.is_dir():
            for child in sorted(skills_dir.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    skill_names.add(child.name)
        # Also include skills that exist in the repo but not locally yet
        repo_skills_dir = self._repo_subdir / "skills"
        if repo_skills_dir.is_dir():
            for child in sorted(repo_skills_dir.iterdir()):
                if child.is_dir():
                    skill_names.add(child.name)
        for name in sorted(skill_names):
            rules.append(SyncRule(path=f"skills/{name}", sync_type=SyncType.FULL))
        return rules

    def get_merge_rules(self) -> dict[str, list[str]]:
        return {"settings.json": list(_SETTINGS_KEYS)}

    def get_exclusions(self) -> list[str]:
        return list(_EXCLUDED_PATHS)

    def get_symlinks(self) -> list[Symlink]:
        """Detect existing symlinks in skills/ and report them."""
        symlinks: list[Symlink] = []
        skills_dir = self.get_paths().config_dir / "skills"
        if skills_dir.is_dir():
            for child in sorted(skills_dir.iterdir()):
                if child.is_symlink():
                    target = str(child.resolve())
                    symlinks.append(
                        Symlink(name=f"skills/{child.name}", target=target)
                    )
        return symlinks

    def get_dependencies(self) -> list[str]:
        return ["shared"]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @property
    def _repo_subdir(self) -> Path:
        """Path inside the repo where Claude Code files are stored."""
        return self.repo_dir / "claude-code"

    def _is_excluded(self, rel: str) -> bool:
        """Return True if *rel* (relative to config_dir) is excluded."""
        for excl in _EXCLUDED_PATHS:
            if rel == excl or rel.startswith(excl + "/"):
                return True
        # Skip .git dirs and node_modules inside skill directories
        parts = rel.split("/")
        if ".git" in parts or "node_modules" in parts or "__pycache__" in parts:
            return True
        return False

    # ------------------------------------------------------------------ #
    # Collect (local → repo)
    # ------------------------------------------------------------------ #

    def collect(self) -> SyncReport:
        """Copy syncable files from ~/.claude/ into the repo."""
        report = SyncReport(adapter_name=self.name)
        config_dir = self.get_paths().config_dir
        if not config_dir.is_dir():
            report.errors.append(f"Config directory not found: {config_dir}")
            return report

        for rule in self.get_sync_rules():
            src = config_dir / rule.path
            dst = self._repo_subdir / rule.path

            if not src.exists():
                report.files_skipped.append(rule.path)
                continue

            if self._is_excluded(rule.path):
                report.files_skipped.append(rule.path)
                continue

            try:
                if rule.sync_type is SyncType.PARTIAL:
                    self._collect_partial(src, dst, rule.keys, report)
                elif src.is_dir():
                    self._collect_dir(src, dst, config_dir, report)
                else:
                    self._collect_file(src, dst, rule.path, report)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{rule.path}: {exc}")

        return report

    def _collect_file(
        self, src: Path, dst: Path, rel: str, report: SyncReport
    ) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        report.files_synced.append(rel)

    def _collect_dir(
        self, src: Path, dst: Path, config_dir: Path, report: SyncReport
    ) -> None:
        for child in src.rglob("*"):
            if child.is_symlink():
                continue
            rel = str(child.relative_to(config_dir))
            if self._is_excluded(rel):
                report.files_skipped.append(rel)
                continue
            if child.is_file():
                target = self._repo_subdir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)
                report.files_synced.append(rel)

    def _collect_partial(
        self, src: Path, dst: Path, keys: list[str], report: SyncReport
    ) -> None:
        """Extract only the declared keys from a JSON file."""
        full = json.loads(src.read_text(encoding="utf-8"))
        extracted = {k: full[k] for k in keys if k in full}
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            json.dumps(extracted, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rel = str(src.name)
        # Use the rule path, not just the filename — keeps it consistent.
        for rule in self.get_sync_rules():
            if rule.path.endswith(src.name):
                rel = rule.path
                break
        report.files_synced.append(rel)

    # ------------------------------------------------------------------ #
    # Deploy (repo → local)
    # ------------------------------------------------------------------ #

    def deploy(self) -> SyncReport:
        """Copy files from the repo into ~/.claude/."""
        report = SyncReport(adapter_name=self.name)
        config_dir = self.get_paths().config_dir
        repo_sub = self._repo_subdir

        if not repo_sub.is_dir():
            report.errors.append(f"Repo subdirectory not found: {repo_sub}")
            return report

        config_dir.mkdir(parents=True, exist_ok=True)

        for rule in self.get_sync_rules():
            src = repo_sub / rule.path
            dst = config_dir / rule.path

            if not src.exists():
                report.files_skipped.append(rule.path)
                continue

            try:
                if rule.sync_type is SyncType.PARTIAL:
                    self._deploy_partial(src, dst, rule.keys, report)
                elif src.is_dir():
                    self._deploy_dir(src, dst, repo_sub, config_dir, report)
                else:
                    self._deploy_file(src, dst, rule.path, report)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{rule.path}: {exc}")

        return report

    def _deploy_file(
        self, src: Path, dst: Path, rel: str, report: SyncReport
    ) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        report.files_synced.append(rel)

    def _deploy_dir(
        self,
        src: Path,
        dst: Path,
        repo_sub: Path,
        config_dir: Path,
        report: SyncReport,
    ) -> None:
        for child in src.rglob("*"):
            if child.is_file():
                rel = str(child.relative_to(repo_sub))
                target = config_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)
                report.files_synced.append(rel)

    def _deploy_partial(
        self, src: Path, dst: Path, keys: list[str], report: SyncReport
    ) -> None:
        """Merge declared keys into the existing local JSON file."""
        incoming = json.loads(src.read_text(encoding="utf-8"))
        if dst.exists():
            existing = json.loads(dst.read_text(encoding="utf-8"))
        else:
            existing = {}
        for key in keys:
            if key in incoming:
                existing[key] = incoming[key]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            json.dumps(existing, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rel = str(src.relative_to(self._repo_subdir))
        report.files_synced.append(rel)

    # ------------------------------------------------------------------ #
    # Diff (repo ↔ local)
    # ------------------------------------------------------------------ #

    def diff(self) -> list[Change]:
        """Compare repo state against local ~/.claude/ for syncable files."""
        changes: list[Change] = []
        config_dir = self.get_paths().config_dir
        repo_sub = self._repo_subdir

        for rule in self.get_sync_rules():
            # plugins/installed_plugins.json — compare by plugin names only,
            # not byte-for-byte (installPath, gitCommitSha, timestamps are
            # machine-specific and always differ across platforms)
            if rule.path.startswith("plugins"):
                changes.extend(self._diff_plugins(repo_sub, config_dir))
                continue

            repo_path = repo_sub / rule.path
            local_path = config_dir / rule.path

            repo_exists = repo_path.exists()
            local_exists = local_path.exists()

            if not repo_exists and not local_exists:
                continue

            if repo_exists and not local_exists:
                changes.append(
                    Change(
                        path=rule.path,
                        kind=ChangeKind.ADDED,
                        detail="Exists in repo but not locally",
                    )
                )
                continue

            if not repo_exists and local_exists:
                changes.append(
                    Change(
                        path=rule.path,
                        kind=ChangeKind.DELETED,
                        detail="Exists locally but not in repo",
                    )
                )
                continue

            # Both exist — compare contents.
            if rule.sync_type is SyncType.PARTIAL:
                change = self._diff_partial(repo_path, local_path, rule)
            elif repo_path.is_dir():
                change = self._diff_dir(repo_path, local_path, repo_sub, config_dir, rule)
            else:
                change = self._diff_file(repo_path, local_path, rule)

            if change is not None:
                changes.append(change)

        return changes

    def _diff_file(
        self, repo_path: Path, local_path: Path, rule: SyncRule
    ) -> Change | None:
        repo_bytes = normalized_bytes(repo_path)
        local_bytes = normalized_bytes(local_path)
        if repo_bytes != local_bytes:
            return Change(
                path=rule.path,
                kind=ChangeKind.MODIFIED,
                detail="File contents differ",
            )
        return None

    def _diff_dir(
        self,
        repo_path: Path,
        local_path: Path,
        repo_sub: Path,
        config_dir: Path,
        rule: SyncRule,
    ) -> Change | None:
        repo_files = {
            str(f.relative_to(repo_path)): normalized_bytes(f)
            for f in repo_path.rglob("*")
            if f.is_file()
        }
        local_files = {
            str(f.relative_to(local_path)): normalized_bytes(f)
            for f in local_path.rglob("*")
            if f.is_file()
            and not f.is_symlink()
            and not self._is_excluded(str(f.relative_to(config_dir)))
        }
        if repo_files != local_files:
            return Change(
                path=rule.path,
                kind=ChangeKind.MODIFIED,
                detail="Directory contents differ",
            )
        return None

    def _diff_partial(
        self, repo_path: Path, local_path: Path, rule: SyncRule
    ) -> Change | None:
        repo_data = json.loads(repo_path.read_text(encoding="utf-8"))
        local_data = json.loads(local_path.read_text(encoding="utf-8"))
        for key in rule.keys:
            if repo_data.get(key) != local_data.get(key):
                return Change(
                    path=rule.path,
                    kind=ChangeKind.MODIFIED,
                    detail=f"Key '{key}' differs",
                )
        return None

    def _diff_plugins(self, repo_sub: Path, config_dir: Path) -> list[Change]:
        """Compare plugins by name — ignore installPath, timestamps, etc."""
        repo_file = repo_sub / "plugins" / "installed_plugins.json"
        local_file = config_dir / "plugins" / "installed_plugins.json"

        repo_names: set[str] = set()
        local_names: set[str] = set()

        if repo_file.is_file():
            data = json.loads(repo_file.read_text(encoding="utf-8"))
            repo_names = set(data.get("plugins", {}).keys())
        if local_file.is_file():
            data = json.loads(local_file.read_text(encoding="utf-8"))
            local_names = set(data.get("plugins", {}).keys())

        changes: list[Change] = []
        for name in sorted(repo_names - local_names):
            changes.append(Change(
                path=f"plugins/{name}",
                kind=ChangeKind.ADDED,
                detail="Plugin in repo but not installed locally",
            ))
        for name in sorted(local_names - repo_names):
            changes.append(Change(
                path=f"plugins/{name}",
                kind=ChangeKind.DELETED,
                detail="Plugin installed locally but not in repo",
            ))
        return changes

    # ------------------------------------------------------------------ #
    # Info (local inventory, no repo needed)
    # ------------------------------------------------------------------ #

    def info(self) -> dict:
        """Return detailed inventory of the local Claude Code installation."""
        result = super().info()
        if not self.detect():
            return result

        config_dir = self.get_paths().config_dir

        # CLAUDE.md
        claude_md = config_dir / "CLAUDE.md"
        result["claude_md"] = str(claude_md) if claude_md.exists() else None

        # Settings
        settings_path = config_dir / "settings.json"
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                result["settings"] = {
                    "defaultMode": settings.get("permissions", {}).get("defaultMode"),
                    "effortLevel": settings.get("effortLevel"),
                }
            except (json.JSONDecodeError, OSError):
                result["settings"] = {"error": "could not parse"}
        else:
            result["settings"] = None

        # Skills — classify as bundles, standalone, or symlinked
        skills_dir = config_dir / "skills"
        bundles = []
        standalone_skills = []
        symlinked_skills = []
        bundle_names: set[str] = set()
        if skills_dir.is_dir():
            # First pass: identify bundles (git repos containing sub-skills)
            for child in sorted(skills_dir.iterdir()):
                if child.is_symlink() or not child.is_dir():
                    continue
                git_dir = child / ".git"
                # A bundle is a directory that contains its own .git AND has
                # subdirectories that other symlinks point to
                if git_dir.exists():
                    sub_skills = sorted(
                        d.name for d in child.iterdir()
                        if d.is_dir() and not d.name.startswith(".")
                        and d.name not in ("node_modules", "__pycache__", "bin", "scripts", "test", "docs")
                    )
                    bundles.append({
                        "name": child.name,
                        "skills": sub_skills,
                        "skill_count": len(sub_skills),
                    })
                    bundle_names.add(child.name)
                else:
                    standalone_skills.append({"name": child.name})

            # Second pass: symlinks (skip those pointing into a known bundle)
            for child in sorted(skills_dir.iterdir()):
                if child.is_symlink():
                    target = str(child.resolve())
                    # Classify: points into a bundle, or external?
                    points_to_bundle = any(
                        f"/skills/{bn}/" in target for bn in bundle_names
                    )
                    if not points_to_bundle:
                        symlinked_skills.append({"name": child.name, "target": target})

        result["bundles"] = bundles
        result["skills_standalone"] = standalone_skills
        result["skills_symlinked"] = symlinked_skills

        # Plugins
        plugins_json = config_dir / "plugins" / "installed_plugins.json"
        if plugins_json.exists():
            try:
                data = json.loads(plugins_json.read_text(encoding="utf-8"))
                plugins = []
                for name, entries in data.get("plugins", {}).items():
                    entry = entries[0] if entries else {}
                    plugins.append({
                        "name": name,
                        "version": entry.get("version", "?"),
                        "scope": entry.get("scope", "?"),
                    })
                result["plugins"] = plugins
            except (json.JSONDecodeError, OSError):
                result["plugins"] = []
        else:
            result["plugins"] = []

        # Memory files
        memory_dir = config_dir / "memory"
        if memory_dir.is_dir():
            result["memory_files"] = sorted(f.name for f in memory_dir.iterdir() if f.is_file())
        else:
            result["memory_files"] = []

        return result
