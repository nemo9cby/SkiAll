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
)
from skiall.core.types import Change, ChangeKind, SyncReport


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
    ) -> list[SyncReport]:
        """Full sync: pull remote, merge with local, push result.

        1. Ensure repo exists (clone or pull)
        2. For each adapter: classify items, resolve conflicts, merge
        3. Commit and push
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

            report = self._sync_adapter(adapter)
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

    def _sync_adapter(self, adapter: BaseAdapter) -> SyncReport:
        """Run the sync merge logic for a single adapter."""
        report = SyncReport(adapter_name=adapter.name)
        config_dir = adapter.get_paths().config_dir
        repo_subdir = self.repo_dir / adapter.name

        self._sync_skills(adapter, config_dir, repo_subdir, report)

        if adapter.name == "claude-code":
            self._sync_plugins(config_dir, repo_subdir, report)

        self._sync_files(adapter, config_dir, repo_subdir, report)

        return report

    def _sync_skills(
        self, adapter: BaseAdapter, config_dir: Path, repo_subdir: Path, report: SyncReport
    ) -> None:
        """Sync skills directories with interactive conflict resolution."""
        if adapter.name == "claude-code":
            local_skills = config_dir / "skills"
            repo_skills = repo_subdir / "skills"
        elif adapter.name == "shared":
            local_skills = config_dir
            repo_skills = repo_subdir
        elif adapter.name == "codex":
            local_skills = config_dir / "skills"
            repo_skills = repo_subdir / "skills"
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
                    shutil.copytree(repo_path, local_path)
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
                choice = prompt_conflict(name, "skill")
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
                        shutil.copytree(repo_path, local_path)
                    else:
                        shutil.copy2(repo_path, local_path)
                    report.files_synced.append(f"skills/{name} (kept remote)")
                else:
                    report.files_skipped.append(f"skills/{name} (skipped)")

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

        cache_dir = str(config_dir / "plugins" / "cache")
        merged = merge_plugins(repo_data, local_data, local_cache_dir=cache_dir)

        local_plugins_file.parent.mkdir(parents=True, exist_ok=True)
        local_plugins_file.write_text(
            json.dumps(merged, indent=2) + "\n", encoding="utf-8"
        )
        repo_plugins_file.parent.mkdir(parents=True, exist_ok=True)
        repo_plugins_file.write_text(
            json.dumps(merged, indent=2) + "\n", encoding="utf-8"
        )
        report.files_synced.append("plugins/installed_plugins.json (merged)")

    def _sync_files(
        self, adapter: BaseAdapter, config_dir: Path, repo_subdir: Path, report: SyncReport
    ) -> None:
        """Sync individual files (CLAUDE.md, memory/, settings.json, etc.)."""
        file_paths: list[str] = []
        for rule in adapter.get_sync_rules():
            if rule.path.startswith("skills"):
                continue
            if rule.path.startswith("plugins"):
                continue
            if rule.path == ".":
                continue
            file_paths.append(rule.path)

        if not file_paths:
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

                choice = prompt_conflict(rel_path, item_type)
                if choice == ConflictChoice.LOCAL:
                    shutil.copy2(local_path, repo_path)
                    report.files_synced.append(f"{rel_path} (kept local)")
                elif choice == ConflictChoice.REMOTE:
                    shutil.copy2(repo_path, local_path)
                    report.files_synced.append(f"{rel_path} (kept remote)")
                else:
                    report.files_skipped.append(f"{rel_path} (skipped)")

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
