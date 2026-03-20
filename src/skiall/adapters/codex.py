"""Codex adapter — manages ~/.codex/ config directory and ~/AGENTS.md.

Syncs:
  - config.toml (partial: model, model_reasoning_effort, personality, features)
  - memories/ (full)
  - AGENTS.md (full, lives at ~/AGENTS.md)

Excludes auth, history, sessions, caches, logs, and other ephemeral/machine-specific data.
"""

from __future__ import annotations

import filecmp
import fnmatch
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

# Keys to preserve from config.toml's top-level scope.
_CONFIG_KEYS: list[str] = ["model", "model_reasoning_effort", "personality", "features"]

# Top-level sections to skip entirely when syncing config.toml.
_CONFIG_SKIP_SECTIONS: list[str] = ["projects"]

# Dot-path keys to skip (section.key notation).
_CONFIG_SKIP_DOTKEYS: list[str] = ["notice.model_migrations"]

# Patterns that must NEVER be synced (relative to ~/.codex/).
_EXCLUSIONS: list[str] = [
    "auth.json",
    "history.jsonl",
    "sessions",
    "sessions/*",
    "cache",
    "cache/*",
    "log",
    "log/*",
    "logs_*.sqlite",
    "state_*.sqlite",
    "models_cache.json",
    "shell_snapshots",
    "shell_snapshots/*",
    "tmp",
    "tmp/*",
    "version.json",
    ".personality_migration",
    "skills/.system",
    "skills/.system/*",
]


def _is_excluded(rel: str) -> bool:
    """Return True if *rel* (forward-slash separated) matches any exclusion pattern."""
    for pattern in _EXCLUSIONS:
        if fnmatch.fnmatch(rel, pattern):
            return True
        # Also match if any parent directory is excluded.
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            partial = "/".join(parts[:i])
            if fnmatch.fnmatch(partial, pattern):
                return True
    return False


# ---- Minimal TOML helpers (no third-party dependency) ----

def _parse_toml(text: str) -> dict[str, object]:
    """Parse a *subset* of TOML sufficient for Codex's config.toml.

    Supports: bare keys, string/int/bool/float values, [section] headers,
    array values on a single line, and inline tables (shallow).
    Does NOT handle multi-line arrays, multi-line strings, or nested inline
    tables — those aren't used in Codex's config.
    """
    result: dict[str, object] = {}
    current_section: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Section header
        if line.startswith("[") and not line.startswith("[["):
            section_name = line.strip("[]").strip()
            current_section = section_name
            result.setdefault(current_section, {})
            continue

        # Key = value
        if "=" in line:
            key, _, raw_val = line.partition("=")
            key = key.strip()
            raw_val = raw_val.strip()
            value = _parse_toml_value(raw_val)
            if current_section is not None:
                section_dict = result.setdefault(current_section, {})
                if isinstance(section_dict, dict):
                    section_dict[key] = value  # type: ignore[index]
            else:
                result[key] = value

    return result


def _parse_toml_value(raw: str) -> object:
    """Parse a single TOML value token."""
    # Strip inline comment (rough heuristic — doesn't handle # inside strings)
    if raw.startswith('"'):
        # Find closing quote
        end = raw.index('"', 1)
        return raw[1:end]
    if raw.startswith("'"):
        end = raw.index("'", 1)
        return raw[1:end]
    if raw.startswith("["):
        # Inline array
        inner = raw[1:raw.rindex("]")].strip()
        if not inner:
            return []
        return [_parse_toml_value(v.strip()) for v in _split_toml_array(inner)]
    if raw.startswith("{"):
        # Inline table
        inner = raw[1:raw.rindex("}")].strip()
        if not inner:
            return {}
        table: dict[str, object] = {}
        for pair in _split_toml_array(inner):
            k, _, v = pair.partition("=")
            table[k.strip()] = _parse_toml_value(v.strip())
        return table
    if raw == "true":
        return True
    if raw == "false":
        return False
    # Try int then float
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _split_toml_array(s: str) -> list[str]:
    """Split a comma-separated TOML array/inline-table body respecting nesting."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    in_string = False
    quote_char = ""
    for ch in s:
        if in_string:
            current.append(ch)
            if ch == quote_char:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote_char = ch
            current.append(ch)
            continue
        if ch in ("[", "{"):
            depth += 1
            current.append(ch)
        elif ch in ("]", "}"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _dump_toml(data: dict[str, object]) -> str:
    """Dump a dict back to TOML text (simple subset)."""
    lines: list[str] = []

    # Top-level bare keys first
    for key, value in data.items():
        if isinstance(value, dict):
            continue
        lines.append(f"{key} = {_toml_encode(value)}")

    # Then sections
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        lines.append("")
        lines.append(f"[{key}]")
        for sk, sv in value.items():
            lines.append(f"{sk} = {_toml_encode(sv)}")

    return "\n".join(lines) + "\n"


def _toml_encode(value: object) -> str:
    """Encode a single value as a TOML literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        inner = ", ".join(_toml_encode(v) for v in value)
        return f"[{inner}]"
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_toml_encode(v)}" for k, v in value.items())
        return f"{{{inner}}}"
    return str(value)


def _filter_config(parsed: dict[str, object]) -> dict[str, object]:
    """Return only the syncable subset of a parsed config.toml."""
    filtered: dict[str, object] = {}

    # Top-level keys
    for key in _CONFIG_KEYS:
        if key in parsed:
            filtered[key] = parsed[key]

    # Sections (skip excluded ones, then filter dot-keys within remaining sections)
    for section_name, section_data in parsed.items():
        if not isinstance(section_data, dict):
            continue
        if section_name in _CONFIG_SKIP_SECTIONS:
            continue
        filtered_section: dict[str, object] = {}
        for sk, sv in section_data.items():
            dotkey = f"{section_name}.{sk}"
            if dotkey in _CONFIG_SKIP_DOTKEYS:
                continue
            filtered_section[sk] = sv
        if filtered_section:
            filtered[section_name] = filtered_section

    return filtered


class CodexAdapter(BaseAdapter):
    """Adapter for OpenAI Codex CLI (~/.codex/ and ~/AGENTS.md)."""

    @property
    def name(self) -> str:
        return "codex"

    # --- Discovery ---

    def detect(self) -> bool:
        """Return True if ~/.codex/ exists."""
        return self._codex_dir().is_dir()

    def get_paths(self) -> PlatformPaths:
        return PlatformPaths(
            config_dir=self._codex_dir(),
            extra_dirs={"agents_md": Path.home() / "AGENTS.md"},
        )

    # --- Sync ---

    def get_sync_rules(self) -> list[SyncRule]:
        return [
            SyncRule(path="config.toml", sync_type=SyncType.PARTIAL, keys=list(_CONFIG_KEYS)),
            SyncRule(path="memories", sync_type=SyncType.FULL),
            SyncRule(path="AGENTS.md", sync_type=SyncType.FULL),
        ]

    def get_exclusions(self) -> list[str]:
        return list(_EXCLUSIONS)

    def get_dependencies(self) -> list[str]:
        return ["shared"]

    def get_merge_rules(self) -> dict[str, list[str]]:
        return {"config.toml": list(_CONFIG_KEYS)}

    def collect(self) -> SyncReport:
        """Gather syncable files from ~/.codex/ and ~/AGENTS.md into the repo."""
        report = SyncReport(adapter_name=self.name)
        codex_dir = self._codex_dir()
        target = self._repo_subdir()

        if not codex_dir.is_dir():
            report.errors.append(f"Codex directory not found: {codex_dir}")
            return report

        target.mkdir(parents=True, exist_ok=True)

        # config.toml — partial sync
        config_src = codex_dir / "config.toml"
        config_dest = target / "config.toml"
        if config_src.is_file():
            try:
                parsed = _parse_toml(config_src.read_text(encoding="utf-8"))
                filtered = _filter_config(parsed)
                config_dest.write_text(_dump_toml(filtered), encoding="utf-8")
                report.files_synced.append("config.toml")
            except Exception as exc:
                report.errors.append(f"Failed to sync config.toml: {exc}")
        else:
            report.files_skipped.append("config.toml (not found)")

        # Walk ~/.codex/ for remaining syncable files
        if codex_dir.is_dir():
            for path in sorted(codex_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(codex_dir))
                if rel == "config.toml":
                    continue  # Already handled above
                if _is_excluded(rel):
                    report.files_skipped.append(rel)
                    continue
                dest = target / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
                    report.files_synced.append(rel)
                except OSError as exc:
                    report.errors.append(f"Failed to copy {rel}: {exc}")

        # ~/AGENTS.md
        agents_md = Path.home() / "AGENTS.md"
        if agents_md.is_file():
            try:
                dest = target / "AGENTS.md"
                shutil.copy2(agents_md, dest)
                report.files_synced.append("AGENTS.md")
            except OSError as exc:
                report.errors.append(f"Failed to copy AGENTS.md: {exc}")
        else:
            report.files_skipped.append("AGENTS.md (not found)")

        return report

    def deploy(self) -> SyncReport:
        """Deploy files from repo/codex/ to ~/.codex/ and ~/AGENTS.md."""
        report = SyncReport(adapter_name=self.name)
        source = self._repo_subdir()
        codex_dir = self._codex_dir()

        if not source.is_dir():
            report.errors.append(f"Repo codex directory not found: {source}")
            return report

        codex_dir.mkdir(parents=True, exist_ok=True)

        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(source))

            # AGENTS.md goes to ~/AGENTS.md
            if rel == "AGENTS.md":
                dest = Path.home() / "AGENTS.md"
            elif rel == "config.toml":
                # Partial merge: read existing config, overlay synced keys
                dest = codex_dir / "config.toml"
                try:
                    repo_parsed = _parse_toml(path.read_text(encoding="utf-8"))
                    if dest.is_file():
                        local_parsed = _parse_toml(dest.read_text(encoding="utf-8"))
                    else:
                        local_parsed = {}
                    # Overlay synced top-level keys
                    for key in _CONFIG_KEYS:
                        if key in repo_parsed:
                            local_parsed[key] = repo_parsed[key]
                    # Overlay synced sections (excluding skipped ones)
                    for section_name, section_data in repo_parsed.items():
                        if not isinstance(section_data, dict):
                            continue
                        if section_name in _CONFIG_SKIP_SECTIONS:
                            continue
                        local_section = local_parsed.setdefault(section_name, {})
                        if isinstance(local_section, dict):
                            for sk, sv in section_data.items():
                                dotkey = f"{section_name}.{sk}"
                                if dotkey not in _CONFIG_SKIP_DOTKEYS:
                                    local_section[sk] = sv
                    dest.write_text(_dump_toml(local_parsed), encoding="utf-8")
                    report.files_synced.append("config.toml")
                except Exception as exc:
                    report.errors.append(f"Failed to deploy config.toml: {exc}")
                continue
            else:
                dest = codex_dir / rel

            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                report.files_synced.append(rel)
            except OSError as exc:
                report.errors.append(f"Failed to deploy {rel}: {exc}")

        return report

    def diff(self) -> list[Change]:
        """Compare repo/codex/ state against local ~/.codex/ and ~/AGENTS.md."""
        changes: list[Change] = []
        codex_dir = self._codex_dir()
        repo = self._repo_subdir()

        # Collect local syncable files
        local_files: dict[str, Path] = {}
        if codex_dir.is_dir():
            for path in codex_dir.rglob("*"):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(codex_dir))
                if _is_excluded(rel):
                    continue
                local_files[rel] = path

        # ~/AGENTS.md
        agents_md = Path.home() / "AGENTS.md"
        if agents_md.is_file():
            local_files["AGENTS.md"] = agents_md

        # Collect repo files
        repo_files: dict[str, Path] = {}
        if repo.is_dir():
            for path in repo.rglob("*"):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(repo))
                repo_files[rel] = path

        all_keys = sorted(set(local_files) | set(repo_files))
        for rel in all_keys:
            in_local = rel in local_files
            in_repo = rel in repo_files

            if in_local and not in_repo:
                changes.append(Change(path=rel, kind=ChangeKind.ADDED, detail="exists locally but not in repo"))
            elif in_repo and not in_local:
                changes.append(Change(path=rel, kind=ChangeKind.DELETED, detail="exists in repo but not locally"))
            else:
                local_path = local_files[rel]
                repo_path = repo_files[rel]
                if rel == "config.toml":
                    # Compare filtered versions only
                    try:
                        local_filtered = _filter_config(
                            _parse_toml(local_path.read_text(encoding="utf-8"))
                        )
                        repo_filtered = _filter_config(
                            _parse_toml(repo_path.read_text(encoding="utf-8"))
                        )
                        if local_filtered != repo_filtered:
                            changes.append(
                                Change(path=rel, kind=ChangeKind.MODIFIED, detail="synced config keys differ")
                            )
                    except Exception:
                        changes.append(
                            Change(path=rel, kind=ChangeKind.MODIFIED, detail="unable to compare config.toml")
                        )
                else:
                    if not filecmp.cmp(local_path, repo_path, shallow=False):
                        changes.append(Change(path=rel, kind=ChangeKind.MODIFIED, detail="content differs"))

        return changes

    # --- Info ---

    def info(self) -> dict:
        """Return detailed inventory of the local Codex installation."""
        result = super().info()
        if not self.detect():
            return result

        codex_dir = self._codex_dir()

        # Config
        config_path = codex_dir / "config.toml"
        if config_path.exists():
            try:
                config = _parse_toml(config_path.read_text(encoding="utf-8"))
                result["config"] = {
                    "model": config.get("model"),
                    "model_reasoning_effort": config.get("model_reasoning_effort"),
                    "personality": config.get("personality"),
                }
                features = config.get("features")
                if isinstance(features, dict):
                    result["config"]["features"] = features
            except Exception:
                result["config"] = {"error": "could not parse"}
        else:
            result["config"] = None

        # AGENTS.md
        agents_md = Path.home() / "AGENTS.md"
        result["agents_md"] = str(agents_md) if agents_md.exists() else None

        # Skills (non-system)
        skills_dir = codex_dir / "skills"
        skills = []
        if skills_dir.is_dir():
            for child in sorted(skills_dir.iterdir()):
                if child.name == ".system":
                    continue
                if child.is_symlink():
                    skills.append({"name": child.name, "type": "symlink", "target": str(child.resolve())})
                elif child.is_dir():
                    skills.append({"name": child.name, "type": "directory"})
        result["skills"] = skills

        # Memories
        mem_dir = codex_dir / "memories"
        if mem_dir.is_dir():
            result["memory_files"] = sorted(f.name for f in mem_dir.iterdir() if f.is_file())
        else:
            result["memory_files"] = []

        return result

    # --- Internals ---

    def _codex_dir(self) -> Path:
        return Path.home() / ".codex"

    def _repo_subdir(self) -> Path:
        return self.repo_dir / "codex"
