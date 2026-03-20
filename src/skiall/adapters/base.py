"""Base adapter interface for platform adapters.

Each adapter knows how to detect, collect, and deploy configs for one platform.
Adapters provide defaults; the manifest can override symlinks and sync rules.

Execution order:
  1. Engine calls get_dependencies() to build a DAG
  2. Adapters run in dependency order (e.g. shared before claude-code)
  3. For each adapter: detect → diff → deploy (pull) or collect (push)
  4. After all adapters: engine creates symlinks, installs bundles/plugins
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from skiall.core.types import (
    Bundle,
    Change,
    PlatformPaths,
    Plugin,
    Symlink,
    SyncReport,
    SyncRule,
)


class BaseAdapter(ABC):
    """Abstract base class for platform adapters."""

    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique adapter name (e.g. 'claude-code', 'codex', 'shared')."""

    # --- Discovery ---

    @abstractmethod
    def detect(self) -> bool:
        """Return True if this platform is installed on the current machine."""

    @abstractmethod
    def get_paths(self) -> PlatformPaths:
        """Return where this platform stores its config on the current OS."""

    # --- Sync ---

    @abstractmethod
    def get_sync_rules(self) -> list[SyncRule]:
        """Return the list of files/dirs this adapter syncs."""

    @abstractmethod
    def collect(self) -> SyncReport:
        """Gather files from local platform into the repo directory."""

    @abstractmethod
    def deploy(self) -> SyncReport:
        """Deploy files from the repo directory to local platform locations."""

    @abstractmethod
    def diff(self) -> list[Change]:
        """Return differences between repo state and local platform state."""

    # --- Advanced (optional, override if needed) ---

    def get_merge_rules(self) -> dict[str, list[str]]:
        """Return per-file key lists for partial sync.

        Returns: {"settings.json": ["enabledPlugins", "effortLevel"], ...}
        """
        return {}

    def get_exclusions(self) -> list[str]:
        """Return hardcoded patterns that must NEVER be synced (secrets)."""
        return []

    def get_symlinks(self) -> list[Symlink]:
        """Return default symlinks this adapter needs."""
        return []

    def get_bundles(self) -> list[Bundle]:
        """Return git repos to clone/update for this platform."""
        return []

    def get_plugins(self) -> list[Plugin]:
        """Return plugins to install via native mechanism."""
        return []

    def get_dependencies(self) -> list[str]:
        """Return names of adapters that must run before this one."""
        return []

    def info(self) -> dict:
        """Return a detailed inventory of what's installed locally.

        No repo needed — this inspects the live file system.
        Returns a dict with platform-specific details.
        """
        return {
            "name": self.name,
            "detected": self.detect(),
            "config_dir": str(self.get_paths().config_dir) if self.detect() else None,
        }
