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

import subprocess
from pathlib import Path

from skiall.adapters.base import BaseAdapter
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
