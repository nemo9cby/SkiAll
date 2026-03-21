"""Core types used across SkiAll."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SyncType(Enum):
    """How a file should be synced."""

    FULL = "full"  # Copy entire file
    PARTIAL = "partial"  # Merge specific keys only


class ChangeKind(Enum):
    """Type of change detected between repo and local."""

    ADDED = "added"  # Exists in source but not target
    MODIFIED = "modified"  # Differs between source and target
    DELETED = "deleted"  # Exists in target but not source
    UNCHANGED = "unchanged"


@dataclass
class SyncRule:
    """Describes how to sync a single file or directory."""

    path: str  # Relative path within the platform's config dir
    sync_type: SyncType = SyncType.FULL
    keys: list[str] = field(default_factory=list)  # For PARTIAL sync: which keys to sync


@dataclass
class Symlink:
    """A symlink to create in the platform's config directory."""

    name: str  # Symlink name (relative to platform config dir)
    target: str  # Target path (relative to platform config dir or absolute)


@dataclass
class Bundle:
    """A git repo to clone/update as a skill bundle."""

    name: str
    repo: str  # Git URL or GitHub shorthand
    path: str  # Where to clone, relative to platform config dir
    ref: str = "main"  # Branch or tag


@dataclass
class Plugin:
    """A plugin to install via the platform's native mechanism."""

    name: str  # e.g. "superpowers@claude-plugins-official"


@dataclass
class PlatformPaths:
    """Where a platform stores its config on the current OS."""

    config_dir: Path  # e.g. ~/.claude/
    extra_dirs: dict[str, Path] = field(default_factory=dict)  # e.g. {"agents": ~/.agents/skills/}


@dataclass
class Change:
    """A single difference between repo and local state."""

    path: str
    kind: ChangeKind
    detail: str = ""  # Human-readable description


@dataclass
class SyncReport:
    """Result of a sync operation (pull or push)."""

    adapter_name: str
    files_synced: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


def normalized_bytes(path: Path) -> bytes:
    """Read file bytes with line endings normalized to LF.

    This ensures CRLF vs LF differences (common between Windows and
    Linux) are not reported as content changes during diff/sync.
    """
    data = path.read_bytes()
    return data.replace(b"\r\n", b"\n")
