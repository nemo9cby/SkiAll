"""Engine — the orchestrator that runs adapters in dependency order.

Pull flow:
  ┌──────────┐     ┌──────────────┐     ┌───────────┐
  │ git pull  │────►│ resolve order │────►│ for each  │
  │ (remote)  │     │ (DAG from    │     │ adapter:  │
  └──────────┘     │  deps)       │     │ diff →    │
                   └──────────────┘     │ deploy    │
                                        └─────┬─────┘
                                              │
                                        ┌─────▼─────┐
                                        │ symlinks  │
                                        │ bundles   │
                                        │ plugins   │
                                        └───────────┘

Push flow:
  ┌───────────┐     ┌──────────────┐     ┌──────────┐
  │ for each  │────►│ collect from │────►│ git add  │
  │ adapter   │     │ local dirs   │     │ commit   │
  └───────────┘     └──────────────┘     │ push     │
                                         └──────────┘
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from skiall.adapters.base import BaseAdapter
from skiall.core.sync import (
    SyncAction,
    ConflictChoice,
    build_file_inventory,
    build_skill_inventory,
    classify_items,
    merge_plugins,
    prompt_conflict,
    strip_install_paths,
)
from skiall.core.types import Change, ChangeKind, SyncReport, SyncRule, SyncType

_STRATEGY_MAP = {
    "skip": ConflictChoice.SKIP,
    "local": ConflictChoice.LOCAL,
    "remote": ConflictChoice.REMOTE,
}


def _resolve_conflict(
    name: str, item_type: str, strategy: str | None
) -> ConflictChoice:
    """Resolve a conflict either automatically or by prompting the user."""
    if strategy is not None:
        return _STRATEGY_MAP[strategy]
    return prompt_conflict(name, item_type)


def _sync_partial_rules(
    rules: list[SyncRule], config_dir: Path, repo_subdir: Path
) -> None:
    """Bidirectional partial merge for config files (settings.json, config.toml).

    For each rule: deploy repo keys into local, then collect local keys back to repo.
    Only touches the declared keys — all other keys in the local file are preserved.
    """
    for rule in rules:
        repo_path = repo_subdir / rule.path
        local_path = config_dir / rule.path

        if not repo_path.exists() and not local_path.exists():
            continue

        try:
            if repo_path.suffix == ".toml":
                _sync_partial_toml(repo_path, local_path, rule.keys)
            else:
                _sync_partial_json(repo_path, local_path, rule.keys)
        except Exception:
            # If partial merge fails, skip silently rather than crash the sync
            pass


def _sync_partial_json(repo_path: Path, local_path: Path, keys: list[str]) -> None:
    """Partial merge for JSON config files (e.g., settings.json)."""
    import json as _json
    repo_data = _json.loads(repo_path.read_text(encoding="utf-8")) if repo_path.exists() else {}
    local_data = _json.loads(local_path.read_text(encoding="utf-8")) if local_path.exists() else {}

    # Deploy: merge repo keys into local
    for key in keys:
        if key in repo_data:
            local_data[key] = repo_data[key]

    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(_json.dumps(local_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Collect: write only synced keys to repo
    collected = {k: local_data[k] for k in keys if k in local_data}
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo_path.write_text(_json.dumps(collected, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sync_partial_toml(repo_path: Path, local_path: Path, keys: list[str]) -> None:
    """Partial merge for TOML config files (e.g., config.toml).

    Uses the Codex adapter's custom TOML parser which handles Windows paths
    in section headers that stdlib tomllib rejects.
    """
    from skiall.adapters.codex import _parse_toml, _dump_toml, _filter_config

    repo_data = _parse_toml(repo_path.read_text(encoding="utf-8")) if repo_path.exists() else {}
    local_data = _parse_toml(local_path.read_text(encoding="utf-8")) if local_path.exists() else {}

    # Deploy: merge repo keys into local
    for key in keys:
        if key in repo_data:
            local_data[key] = repo_data[key]

    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(_dump_toml(local_data), encoding="utf-8")

    # Collect: write only synced keys to repo
    collected = _filter_config(local_data)
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo_path.write_text(_dump_toml(collected), encoding="utf-8")


def _create_sub_skill_symlinks(skills_dir: Path, report: SyncReport) -> None:
    """Create symlinks for nested sub-skills so Claude Code can discover them.

    Claude Code discovers skills at ~/.claude/skills/*/SKILL.md (one level deep).
    Skill bundles like gstack have sub-skills at gstack/browse/SKILL.md, etc.
    This creates symlinks: ~/.claude/skills/browse -> gstack/browse
    so each sub-skill is discoverable.

    On Windows, uses directory junctions (no admin required) as fallback
    when symlinks fail.
    """
    import os
    import platform

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir() or skill_dir.is_symlink():
            continue
        # Skip hidden dirs (e.g., .system)
        if skill_dir.name.startswith("."):
            continue
        # Check if this skill has sub-directories with SKILL.md
        for sub_dir in sorted(skill_dir.iterdir()):
            if not sub_dir.is_dir() or sub_dir.is_symlink():
                continue
            # Skip non-skill dirs (node_modules, bin, test, scripts, docs, etc.)
            sub_skill_md = sub_dir / "SKILL.md"
            if not sub_skill_md.is_file():
                continue

            link_path = skills_dir / sub_dir.name
            abs_target = str(sub_dir.resolve())
            rel_target = f"{skill_dir.name}/{sub_dir.name}"

            if link_path.exists() and not link_path.is_symlink():
                # Real directory exists with this name — don't overwrite
                continue

            try:
                if link_path.is_symlink() or _is_junction(link_path):
                    _remove_link(link_path)
                os.symlink(rel_target, str(link_path))
                report.files_synced.append(f"symlink: {sub_dir.name} -> {rel_target}")
            except OSError:
                if platform.system() == "Windows":
                    # Fall back to directory junction (no admin required)
                    try:
                        if link_path.exists():
                            _remove_link(link_path)
                        subprocess.run(
                            ["cmd.exe", "/c", "mklink", "/J", str(link_path), abs_target],
                            check=True, capture_output=True,
                        )
                        report.files_synced.append(f"junction: {sub_dir.name} -> {rel_target}")
                    except (OSError, subprocess.CalledProcessError):
                        report.warnings.append(
                            f"Could not link {sub_dir.name} -> {rel_target}"
                        )
                else:
                    report.warnings.append(
                        f"Could not create symlink {sub_dir.name} -> {rel_target}"
                    )


def _is_junction(path: Path) -> bool:
    """Check if a path is a Windows directory junction."""
    import platform
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT) != 0
    except (OSError, AttributeError):
        return False


def _remove_link(path: Path) -> None:
    """Remove a symlink or junction."""
    import platform
    if platform.system() == "Windows" and path.is_dir():
        # Junctions are directories on Windows — rmdir removes without deleting target
        path.rmdir()
    else:
        path.unlink()


class Engine:
    """Orchestrates adapter execution for pull/push operations."""

    def __init__(self, repo_dir: Path, adapters: list[BaseAdapter]) -> None:
        self.repo_dir = repo_dir
        self.adapters = {a.name: a for a in adapters}

    def resolve_order(self) -> list[BaseAdapter]:
        """Topological sort of adapters by dependencies.

        Returns adapters in execution order (dependencies first).
        Raises ValueError if circular dependencies detected.
        """
        visited: set[str] = set()
        order: list[str] = []
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError(f"Circular dependency detected involving '{name}'")
            visiting.add(name)
            adapter = self.adapters.get(name)
            if adapter:
                for dep in adapter.get_dependencies():
                    if dep in self.adapters:
                        visit(dep)
            visiting.discard(name)
            visited.add(name)
            order.append(name)

        for name in self.adapters:
            visit(name)

        return [self.adapters[n] for n in order if n in self.adapters]

    def pull(self, force: bool = False) -> list[SyncReport]:
        """Pull: deploy repo state to local platform directories.

        1. Run git pull on the repo
        2. Resolve adapter order
        3. For each adapter: check diff, deploy if safe
        """
        self._git_pull()
        ordered = self.resolve_order()
        reports = []
        for adapter in ordered:
            # Check if this adapter has content in the repo to deploy
            repo_subdir = self.repo_dir / adapter.name
            has_repo_content = repo_subdir.is_dir() and any(repo_subdir.iterdir())

            if not adapter.detect() and not has_repo_content:
                # Platform not installed AND nothing in repo — truly skip
                reports.append(
                    SyncReport(
                        adapter_name=adapter.name,
                        warnings=[f"Platform '{adapter.name}' not detected, skipping"],
                    )
                )
                continue

            # If platform not detected but repo has content, deploy anyway
            # (fresh machine bootstrap — deploy() will create the dirs)
            if adapter.detect():
                changes = adapter.diff()
                conflicts = [c for c in changes if c.kind == ChangeKind.MODIFIED]
            else:
                conflicts = []

            if conflicts and not force:
                report = SyncReport(
                    adapter_name=adapter.name,
                    errors=[
                        f"Local changes detected in {len(conflicts)} file(s). "
                        "Use --force to overwrite, or resolve manually:\n"
                        + "\n".join(f"  - {c.path}: {c.detail}" for c in conflicts)
                    ],
                )
                reports.append(report)
                continue

            report = adapter.deploy()
            reports.append(report)

        return reports

    def push(self, message: str = "skiall push") -> list[SyncReport]:
        """Push: collect local state into repo and commit/push.

        1. Resolve adapter order
        2. For each adapter: collect local files into repo
        3. Git add, commit, push
        """
        ordered = self.resolve_order()
        reports = []
        for adapter in ordered:
            if not adapter.detect():
                reports.append(
                    SyncReport(
                        adapter_name=adapter.name,
                        warnings=[f"Platform '{adapter.name}' not detected, skipping"],
                    )
                )
                continue
            report = adapter.collect()
            reports.append(report)

        # Abort git operations if any adapter reported errors
        has_errors = any(not r.success for r in reports)
        if has_errors:
            return reports

        self._git_commit_and_push(message)
        return reports

    def status(self) -> dict[str, list[Change]]:
        """Show sync status for all detected adapters."""
        result = {}
        for adapter in self.resolve_order():
            if adapter.detect():
                result[adapter.name] = adapter.diff()
            else:
                result[adapter.name] = [
                    Change(path="<platform>", kind=ChangeKind.DELETED, detail="not installed")
                ]
        return result

    def setup(self, remote_url: str | None = None) -> dict:
        """Initialize a new SkiAll repo from the current machine's state.

        Returns the generated manifest dict.
        """
        from skiall.core.manifest import generate_manifest_from_adapters, save_manifest
        from skiall.core.secrets import generate_gitignore, install_pre_commit_hook

        self.repo_dir.mkdir(parents=True, exist_ok=True)

        # Init git repo if not already
        git_dir = self.repo_dir / ".git"
        if not git_dir.exists():
            subprocess.run(["git", "init"], cwd=self.repo_dir, check=True, capture_output=True)

        # Generate .gitignore
        gitignore_path = self.repo_dir / ".gitignore"
        gitignore_path.write_text(generate_gitignore())

        # Install pre-commit hook
        install_pre_commit_hook(self.repo_dir)

        # Generate manifest from detected adapters
        all_adapters = list(self.adapters.values())
        manifest = generate_manifest_from_adapters(all_adapters)

        manifest_path = self.repo_dir / "skiall.yaml"
        save_manifest(manifest_path, manifest)

        # Create adapter directories
        for adapter in all_adapters:
            if adapter.detect():
                adapter_dir = self.repo_dir / adapter.name
                adapter_dir.mkdir(parents=True, exist_ok=True)

        # Set remote if provided
        if remote_url:
            subprocess.run(
                ["git", "remote", "add", "origin", remote_url],
                cwd=self.repo_dir,
                capture_output=True,
            )
            # Check if remote already has content (e.g. pushed from another machine)
            fetch_result = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
            )
            if fetch_result.returncode == 0:
                # Check if remote has any branches
                ls_result = subprocess.run(
                    ["git", "branch", "-r"],
                    cwd=self.repo_dir,
                    capture_output=True,
                    text=True,
                )
                remote_branches = ls_result.stdout.strip()
                if remote_branches:
                    # Remote has content — pull it in so we merge with existing config
                    # Find the default branch name
                    branch = remote_branches.split("/")[-1].split()[0]
                    # Set up tracking and merge
                    subprocess.run(
                        ["git", "pull", "origin", branch, "--allow-unrelated-histories"],
                        cwd=self.repo_dir,
                        capture_output=True,
                        text=True,
                    )

        return manifest

    def clone(self, remote_url: str) -> dict:
        """Clone an existing SkiAll repo and deploy to local.

        Returns the manifest dict from the cloned repo.
        """
        from skiall.core.manifest import load_manifest

        from skiall.core.secrets import install_pre_commit_hook

        subprocess.run(
            ["git", "clone", remote_url, str(self.repo_dir)],
            check=True,
            capture_output=True,
        )
        # Git doesn't clone hooks — reinstall the secret guard
        install_pre_commit_hook(self.repo_dir)

        manifest = load_manifest(self.repo_dir / "skiall.yaml")
        # Deploy all adapters (force=True since this is a fresh machine)
        self.pull(force=True)
        return manifest

    def sync(
        self,
        remote_url: str | None = None,
        message: str = "skiall sync",
        conflict_strategy: str | None = None,
    ) -> list[SyncReport]:
        """Full sync: pull remote, merge with local, push result.

        1. Ensure repo exists (clone or pull)
        2. For each adapter: classify items, resolve conflicts, merge
        3. Commit and push

        Args:
            conflict_strategy: "skip", "local", or "remote" to auto-resolve
                all conflicts without prompting. None = interactive prompt.
        """
        self._ensure_repo(remote_url)
        ordered = self.resolve_order()
        reports: list[SyncReport] = []

        for adapter in ordered:
            repo_subdir = self.repo_dir / adapter.name
            has_repo_content = repo_subdir.is_dir() and any(repo_subdir.iterdir()) if repo_subdir.is_dir() else False

            if not adapter.detect() and not has_repo_content:
                reports.append(SyncReport(
                    adapter_name=adapter.name,
                    warnings=[f"Platform '{adapter.name}' not detected and no repo content, skipping"],
                ))
                continue

            report = self._sync_adapter(adapter, conflict_strategy=conflict_strategy)
            reports.append(report)

        has_errors = any(not r.success for r in reports)
        if not has_errors:
            self._git_commit_and_push(message)

        return reports

    def _ensure_repo(self, remote_url: str | None = None) -> None:
        """Ensure the sync repo exists. Clone if needed, pull if exists."""
        git_dir = self.repo_dir / ".git"
        if git_dir.is_dir():
            self._git_pull()
        elif remote_url:
            subprocess.run(
                ["git", "clone", remote_url, str(self.repo_dir)],
                check=True,
                capture_output=True,
            )
            from skiall.core.secrets import install_pre_commit_hook
            install_pre_commit_hook(self.repo_dir)
        else:
            raise RuntimeError(
                "No SkiAll repo found. Provide a repo URL for first-time sync."
            )

    def _sync_adapter(
        self, adapter: BaseAdapter, conflict_strategy: str | None = None
    ) -> SyncReport:
        """Run the sync merge logic for a single adapter."""
        report = SyncReport(adapter_name=adapter.name)
        config_dir = adapter.get_paths().config_dir
        repo_subdir = self.repo_dir / adapter.name

        self._sync_skills(adapter, config_dir, repo_subdir, report, conflict_strategy)

        if adapter.name == "claude-code":
            self._sync_plugins(config_dir, repo_subdir, report)

        self._sync_files(adapter, config_dir, repo_subdir, report, conflict_strategy)

        return report

    def _sync_skills(
        self, adapter: BaseAdapter, config_dir: Path, repo_subdir: Path,
        report: SyncReport, conflict_strategy: str | None = None,
    ) -> None:
        """Sync skills directories with conflict resolution."""
        if adapter.name == "claude-code":
            local_skills = config_dir / "skills"
            repo_skills = repo_subdir / "skills"
        elif adapter.name == "shared":
            local_skills = config_dir
            repo_skills = repo_subdir
        elif adapter.name == "codex":
            local_skills = config_dir / "skills"
            repo_skills = repo_subdir / "skills"
            if not local_skills.is_dir() and not repo_skills.is_dir():
                return
        else:
            return

        local_inv = build_skill_inventory(local_skills)
        repo_inv = build_skill_inventory(repo_skills)

        if not local_inv and not repo_inv:
            return

        classified = classify_items(repo_inv, local_inv)

        for name, action in classified.items():
            local_path = local_skills / name
            repo_path = repo_skills / name

            if action == SyncAction.REMOTE_ONLY:
                local_skills.mkdir(parents=True, exist_ok=True)
                if repo_path.is_dir():
                    ignore = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".env", ".env.*")
                    shutil.copytree(repo_path, local_path, ignore=ignore)
                else:
                    shutil.copy2(repo_path, local_path)
                report.files_synced.append(f"skills/{name} (remote -> local)")

            elif action == SyncAction.LOCAL_ONLY:
                repo_skills.mkdir(parents=True, exist_ok=True)
                if local_path.is_dir():
                    ignore = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".env", ".env.*")
                    shutil.copytree(local_path, repo_path, ignore=ignore)
                else:
                    shutil.copy2(local_path, repo_path)
                report.files_synced.append(f"skills/{name} (local -> repo)")

            elif action == SyncAction.CONFLICT:
                choice = _resolve_conflict(name, "skill", conflict_strategy)
                if choice == ConflictChoice.LOCAL:
                    if repo_path.is_dir():
                        shutil.rmtree(repo_path)
                    if local_path.is_dir():
                        ignore = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".env", ".env.*")
                        shutil.copytree(local_path, repo_path, ignore=ignore)
                    else:
                        shutil.copy2(local_path, repo_path)
                    report.files_synced.append(f"skills/{name} (kept local)")
                elif choice == ConflictChoice.REMOTE:
                    if local_path.is_dir():
                        shutil.rmtree(local_path)
                    if repo_path.is_dir():
                        ignore = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".env", ".env.*")
                        shutil.copytree(repo_path, local_path, ignore=ignore)
                    else:
                        shutil.copy2(repo_path, local_path)
                    report.files_synced.append(f"skills/{name} (kept remote)")
                else:
                    report.files_skipped.append(f"skills/{name} (skipped)")

        # Create symlinks for nested sub-skills (e.g., gstack/browse -> browse)
        # so Claude Code can discover them at ~/.claude/skills/*/SKILL.md
        if local_skills.is_dir():
            _create_sub_skill_symlinks(local_skills, report)

    def _sync_plugins(
        self, config_dir: Path, repo_subdir: Path, report: SyncReport
    ) -> None:
        """Merge installed_plugins.json by unioning plugin names."""
        local_plugins_file = config_dir / "plugins" / "installed_plugins.json"
        repo_plugins_file = repo_subdir / "plugins" / "installed_plugins.json"

        local_data = None
        repo_data = None
        if local_plugins_file.is_file():
            local_data = json.loads(local_plugins_file.read_text(encoding="utf-8"))
        if repo_plugins_file.is_file():
            repo_data = json.loads(repo_plugins_file.read_text(encoding="utf-8"))

        if local_data is None and repo_data is None:
            return

        # Save original local installPaths before merge
        local_paths: dict[str, dict[str, str]] = {}  # name -> {scope -> path}
        if local_data:
            for name, entries in local_data.get("plugins", {}).items():
                for entry in entries:
                    if "installPath" in entry:
                        local_paths.setdefault(name, {})[entry.get("scope", "user")] = entry["installPath"]

        merged = merge_plugins(repo_data, local_data)

        # Local copy: restore this machine's installPaths (merged may have
        # remote paths or no paths at all since repo strips them)
        for name, entries in merged.get("plugins", {}).items():
            for entry in entries:
                scope = entry.get("scope", "user")
                local_path = local_paths.get(name, {}).get(scope)
                if local_path:
                    entry["installPath"] = local_path
                else:
                    entry.pop("installPath", None)  # Remove stale remote paths

        local_plugins_file.parent.mkdir(parents=True, exist_ok=True)
        local_plugins_file.write_text(
            json.dumps(merged, indent=2) + "\n", encoding="utf-8"
        )

        # Repo copy: strip installPath (machine-specific, not portable)
        repo_merged = strip_install_paths(merged)
        repo_plugins_file.parent.mkdir(parents=True, exist_ok=True)
        repo_plugins_file.write_text(
            json.dumps(repo_merged, indent=2) + "\n", encoding="utf-8"
        )
        report.files_synced.append("plugins/installed_plugins.json (merged)")

    def _sync_files(
        self, adapter: BaseAdapter, config_dir: Path, repo_subdir: Path,
        report: SyncReport, conflict_strategy: str | None = None,
    ) -> None:
        """Sync individual files (CLAUDE.md, memory/, AGENTS.md, etc.)."""
        file_paths: list[str] = []
        partial_rules: list[SyncRule] = []
        for rule in adapter.get_sync_rules():
            if rule.path.startswith("skills"):
                continue
            if rule.path.startswith("plugins"):
                continue
            if rule.path == ".":
                continue
            if rule.sync_type == SyncType.PARTIAL:
                partial_rules.append(rule)
                continue
            file_paths.append(rule.path)

        # Handle PARTIAL sync rules using adapter's existing merge logic
        for rule in partial_rules:
            repo_path = repo_subdir / rule.path
            local_path = config_dir / rule.path
            if repo_path.exists() and local_path.exists():
                # Both exist — deploy repo keys to local (merge), then collect local back to repo
                # This effectively does a bidirectional partial merge
                report.files_synced.append(f"{rule.path} (partial merge)")
            elif repo_path.exists():
                report.files_synced.append(f"{rule.path} (remote -> local, partial)")
            elif local_path.exists():
                report.files_synced.append(f"{rule.path} (local -> repo, partial)")
            # Let the adapter's normal collect/deploy handle the actual merge

        if not file_paths:
            if partial_rules and adapter.detect():
                _sync_partial_rules(partial_rules, config_dir, repo_subdir)
            return

        repo_inv = build_file_inventory(repo_subdir, file_paths)
        local_inv = build_file_inventory(config_dir, file_paths)

        classified = classify_items(repo_inv, local_inv)

        for rel_path, action in classified.items():
            repo_path = repo_subdir / rel_path
            local_path = config_dir / rel_path

            if action == SyncAction.REMOTE_ONLY:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(repo_path, local_path)
                report.files_synced.append(f"{rel_path} (remote -> local)")

            elif action == SyncAction.LOCAL_ONLY:
                repo_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_path, repo_path)
                report.files_synced.append(f"{rel_path} (local -> repo)")

            elif action == SyncAction.CONFLICT:
                item_type = "file"
                if "memory" in rel_path:
                    item_type = "memory"
                elif rel_path.endswith(".md"):
                    item_type = "config"

                choice = _resolve_conflict(rel_path, item_type, conflict_strategy)
                if choice == ConflictChoice.LOCAL:
                    shutil.copy2(local_path, repo_path)
                    report.files_synced.append(f"{rel_path} (kept local)")
                elif choice == ConflictChoice.REMOTE:
                    shutil.copy2(repo_path, local_path)
                    report.files_synced.append(f"{rel_path} (kept remote)")
                else:
                    report.files_skipped.append(f"{rel_path} (skipped)")

        # Handle partial rules with targeted merge (not full adapter deploy/collect
        # which would overwrite files like plugins that sync already handled)
        if partial_rules and adapter.detect():
            _sync_partial_rules(partial_rules, config_dir, repo_subdir)

    def _git_pull(self) -> None:
        """Run git pull on the repo directory."""
        result = subprocess.run(
            ["git", "pull"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "no tracking information" in stderr or "no remote" in stderr.lower():
                # Try pulling with explicit origin + branch detection
                branch_result = subprocess.run(
                    ["git", "branch", "-r"],
                    cwd=self.repo_dir,
                    capture_output=True,
                    text=True,
                )
                remote_branches = branch_result.stdout.strip()
                if remote_branches:
                    branch = remote_branches.split("/")[-1].split()[0]
                    subprocess.run(
                        ["git", "pull", "origin", branch],
                        cwd=self.repo_dir,
                        capture_output=True,
                    )
                return  # Local-only repo or handled above
            if stderr:
                raise RuntimeError(f"git pull failed: {stderr}")

    def _git_commit_and_push(self, message: str) -> None:
        """Stage all changes, commit, and push."""
        subprocess.run(
            ["git", "add", "-A"],
            cwd=self.repo_dir,
            check=True,
            capture_output=True,
        )
        # Check if there's anything to commit
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            return  # Nothing to commit

        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        if commit_result.returncode != 0:
            stderr = commit_result.stderr.strip()
            raise RuntimeError(f"git commit failed: {stderr}")

        # Push — use -u on first push to set upstream tracking
        # Try regular push first, fall back to -u if no upstream
        push_result = subprocess.run(
            ["git", "push"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        if push_result.returncode != 0:
            stderr = push_result.stderr.strip()
            # No upstream — try setting it with -u
            if "no upstream" in stderr.lower() or "has no upstream" in stderr.lower():
                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=self.repo_dir, capture_output=True, text=True,
                ).stdout.strip() or "main"
                push_result = subprocess.run(
                    ["git", "push", "-u", "origin", branch],
                    cwd=self.repo_dir, capture_output=True, text=True,
                )
                if push_result.returncode != 0:
                    raise RuntimeError(f"git push failed: {push_result.stderr.strip()}")
            elif "no configured push destination" not in stderr.lower():
                raise RuntimeError(f"git push failed: {stderr}")
