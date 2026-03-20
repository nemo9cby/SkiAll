"""Tests for skiall.core.merger -- partial key merge and conflict detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from skiall.core.merger import (
    detect_key_conflicts,
    merge_partial,
    read_config,
    write_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    """Return a clean temporary directory for each test."""
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_toml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


# ===========================================================================
# T4: Partial key merge for JSON
# ===========================================================================

class TestPartialMergeJSON:
    """Merge enabledPlugins into existing settings.json, verify other keys untouched."""

    def test_merge_single_key_preserves_others(self, tmp_dir: Path) -> None:
        """T4 core: merge enabledPlugins from repo into local settings.json."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {
            "enabledPlugins": ["plugin-a", "plugin-b"],
            "effortLevel": "high",
        })
        _write_json(local_file, {
            "enabledPlugins": ["plugin-old"],
            "theme": "dark",
            "fontSize": 14,
        })

        merged = merge_partial(repo_file, local_file, keys=["enabledPlugins"])

        # Synced key should come from repo
        assert merged["enabledPlugins"] == ["plugin-a", "plugin-b"]
        # Non-synced keys should remain from local
        assert merged["theme"] == "dark"
        assert merged["fontSize"] == 14
        # Key in repo but not in synced list should NOT leak into local
        assert "effortLevel" not in merged

    def test_merge_multiple_keys(self, tmp_dir: Path) -> None:
        """Merge multiple keys at once."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {
            "enabledPlugins": ["plugin-a"],
            "effortLevel": "high",
            "model": "opus",
        })
        _write_json(local_file, {
            "enabledPlugins": [],
            "effortLevel": "low",
            "localOnly": True,
        })

        merged = merge_partial(
            repo_file, local_file,
            keys=["enabledPlugins", "effortLevel"],
        )

        assert merged["enabledPlugins"] == ["plugin-a"]
        assert merged["effortLevel"] == "high"
        assert merged["localOnly"] is True
        # Non-synced repo key should not appear
        assert "model" not in merged


# ===========================================================================
# T5: Partial key merge for TOML
# ===========================================================================

class TestPartialMergeTOML:
    """Merge model key, verify [projects] section untouched."""

    def test_merge_toml_top_level_key(self, tmp_dir: Path) -> None:
        """T5 core: merge 'model' key from repo TOML, preserve [projects]."""
        repo_file = tmp_dir / "repo" / "config.toml"
        local_file = tmp_dir / "local" / "config.toml"

        _write_toml(repo_file, (
            'model = "opus"\n'
            'version = "2.0"\n'
            "\n"
            "[projects]\n"
            'default = "/repo/projects/default"\n'
        ))
        _write_toml(local_file, (
            'model = "sonnet"\n'
            "\n"
            "[projects]\n"
            'default = "/home/alice/projects"\n'
            'extra = "/home/alice/extra"\n'
        ))

        merged = merge_partial(repo_file, local_file, keys=["model"])

        assert merged["model"] == "opus"
        # [projects] should be entirely from local
        assert merged["projects"]["default"] == "/home/alice/projects"
        assert merged["projects"]["extra"] == "/home/alice/extra"
        # 'version' from repo should NOT leak
        assert "version" not in merged

    def test_merge_toml_section_key(self, tmp_dir: Path) -> None:
        """Merge a nested section key using dot-notation."""
        repo_file = tmp_dir / "repo" / "config.toml"
        local_file = tmp_dir / "local" / "config.toml"

        _write_toml(repo_file, (
            "[editor]\n"
            "theme = \"monokai\"\n"
            "font_size = 16\n"
            "\n"
            "[projects]\n"
            "default = \"/repo/path\"\n"
        ))
        _write_toml(local_file, (
            "[editor]\n"
            "theme = \"solarized\"\n"
            "tab_size = 4\n"
            "\n"
            "[projects]\n"
            "default = \"/local/path\"\n"
        ))

        merged = merge_partial(repo_file, local_file, keys=["editor.theme"])

        assert merged["editor"]["theme"] == "monokai"
        # Other editor keys from local should remain
        assert merged["editor"]["tab_size"] == 4
        # font_size from repo should NOT leak (not a synced key)
        assert "font_size" not in merged["editor"]
        # projects untouched
        assert merged["projects"]["default"] == "/local/path"


# ===========================================================================
# YAML merge tests
# ===========================================================================

class TestPartialMergeYAML:
    """Verify YAML support works end-to-end."""

    def test_merge_yaml_key(self, tmp_dir: Path) -> None:
        repo_file = tmp_dir / "repo" / "config.yaml"
        local_file = tmp_dir / "local" / "config.yaml"

        _write_yaml(repo_file, {
            "plugins": ["a", "b", "c"],
            "version": 3,
        })
        _write_yaml(local_file, {
            "plugins": ["old"],
            "local_setting": True,
        })

        merged = merge_partial(repo_file, local_file, keys=["plugins"])

        assert merged["plugins"] == ["a", "b", "c"]
        assert merged["local_setting"] is True
        assert "version" not in merged


# ===========================================================================
# Edge cases
# ===========================================================================

class TestEdgeCases:
    """Missing keys, file doesn't exist, nested keys, type mismatches."""

    def test_local_file_does_not_exist(self, tmp_dir: Path) -> None:
        """First-time setup: local file missing. Should return only synced keys from repo."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"  # does not exist

        _write_json(repo_file, {
            "enabledPlugins": ["plugin-a"],
            "effortLevel": "high",
            "model": "opus",
        })

        merged = merge_partial(repo_file, local_file, keys=["enabledPlugins", "effortLevel"])

        assert merged["enabledPlugins"] == ["plugin-a"]
        assert merged["effortLevel"] == "high"
        # Non-synced key from repo should NOT appear
        assert "model" not in merged

    def test_key_missing_from_repo(self, tmp_dir: Path) -> None:
        """Synced key missing in repo -> local value left intact."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"otherKey": 1})
        _write_json(local_file, {"enabledPlugins": ["local-plugin"], "fontSize": 12})

        merged = merge_partial(repo_file, local_file, keys=["enabledPlugins"])

        # Key stayed from local since repo doesn't have it
        assert merged["enabledPlugins"] == ["local-plugin"]
        assert merged["fontSize"] == 12

    def test_key_missing_from_both(self, tmp_dir: Path) -> None:
        """Synced key missing from both repo and local -> key simply absent."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"a": 1})
        _write_json(local_file, {"b": 2})

        merged = merge_partial(repo_file, local_file, keys=["nonexistent"])

        assert merged == {"b": 2}

    def test_nested_key_creation(self, tmp_dir: Path) -> None:
        """Dot-notation key that doesn't exist locally creates intermediate dicts."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"editor": {"fontSize": 16, "theme": "dark"}})
        _write_json(local_file, {"other": "value"})

        merged = merge_partial(repo_file, local_file, keys=["editor.fontSize"])

        assert merged["editor"]["fontSize"] == 16
        assert merged["other"] == "value"
        # theme from repo should NOT leak
        assert "theme" not in merged.get("editor", {})

    def test_nested_key_deep_merge(self, tmp_dir: Path) -> None:
        """When synced key is a dict in both, deep-merge (repo wins on leaves)."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {
            "editor": {
                "fontSize": 16,
                "lineNumbers": True,
                "minimap": {"enabled": True},
            },
        })
        _write_json(local_file, {
            "editor": {
                "fontSize": 12,
                "tabSize": 4,
                "minimap": {"enabled": False, "maxColumn": 80},
            },
        })

        merged = merge_partial(repo_file, local_file, keys=["editor"])

        # Repo values win on conflicts
        assert merged["editor"]["fontSize"] == 16
        assert merged["editor"]["lineNumbers"] is True
        assert merged["editor"]["minimap"]["enabled"] is True
        # Local-only leaves survive deep merge
        assert merged["editor"]["tabSize"] == 4
        assert merged["editor"]["minimap"]["maxColumn"] == 80

    def test_type_mismatch_repo_wins(self, tmp_dir: Path) -> None:
        """When local has a scalar but repo has a dict (or vice versa), repo wins."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"plugins": {"enabled": ["a", "b"]}})
        _write_json(local_file, {"plugins": "old-format-string", "other": 1})

        merged = merge_partial(repo_file, local_file, keys=["plugins"])

        # Repo wins completely -- type changed from str to dict
        assert merged["plugins"] == {"enabled": ["a", "b"]}
        assert merged["other"] == 1

    def test_type_mismatch_reverse(self, tmp_dir: Path) -> None:
        """Local has dict, repo has scalar -- repo wins."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"plugins": ["a", "b"]})
        _write_json(local_file, {"plugins": {"enabled": True}, "other": 1})

        merged = merge_partial(repo_file, local_file, keys=["plugins"])

        assert merged["plugins"] == ["a", "b"]
        assert merged["other"] == 1

    def test_empty_keys_list(self, tmp_dir: Path) -> None:
        """No keys to sync -> local is returned unchanged."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"a": 1, "b": 2})
        _write_json(local_file, {"c": 3, "d": 4})

        merged = merge_partial(repo_file, local_file, keys=[])

        assert merged == {"c": 3, "d": 4}

    def test_merge_does_not_mutate_inputs(self, tmp_dir: Path) -> None:
        """Ensure merge_partial returns a new dict without mutating originals on disk."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        local_data = {"plugins": ["old"], "keep": True}
        _write_json(repo_file, {"plugins": ["new"]})
        _write_json(local_file, local_data)

        merged = merge_partial(repo_file, local_file, keys=["plugins"])

        # Re-read local to confirm it was not modified on disk
        reread = read_config(local_file)
        assert reread["plugins"] == ["old"]
        assert merged["plugins"] == ["new"]


# ===========================================================================
# Conflict detection
# ===========================================================================

class TestConflictDetection:
    """detect_key_conflicts: synced key differs, non-synced key should NOT conflict."""

    def test_two_way_synced_key_differs(self, tmp_dir: Path) -> None:
        """Synced key differs between repo and local -> conflict reported."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"enabledPlugins": ["a", "b"], "theme": "dark"})
        _write_json(local_file, {"enabledPlugins": ["a", "c"], "theme": "light"})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=None,
            keys=["enabledPlugins"],
        )

        assert "enabledPlugins" in conflicts

    def test_two_way_non_synced_key_differs_no_conflict(self, tmp_dir: Path) -> None:
        """Non-synced key differs -> should NOT be reported as conflict."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"enabledPlugins": ["a"], "theme": "dark"})
        _write_json(local_file, {"enabledPlugins": ["a"], "theme": "light"})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=None,
            keys=["enabledPlugins"],
        )

        assert conflicts == []

    def test_two_way_synced_key_same_no_conflict(self, tmp_dir: Path) -> None:
        """Synced key is identical -> no conflict."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"plugins": ["a", "b"]})
        _write_json(local_file, {"plugins": ["a", "b"], "localOnly": True})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=None,
            keys=["plugins"],
        )

        assert conflicts == []

    def test_three_way_both_changed_differently(self, tmp_dir: Path) -> None:
        """Three-way: both repo and local changed from base -> conflict."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"
        base_file = tmp_dir / "base" / "settings.json"

        _write_json(base_file, {"model": "haiku"})
        _write_json(repo_file, {"model": "opus"})
        _write_json(local_file, {"model": "sonnet"})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=base_file,
            keys=["model"],
        )

        assert "model" in conflicts

    def test_three_way_only_repo_changed(self, tmp_dir: Path) -> None:
        """Three-way: only repo changed -> no conflict (safe to accept repo)."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"
        base_file = tmp_dir / "base" / "settings.json"

        _write_json(base_file, {"model": "haiku"})
        _write_json(repo_file, {"model": "opus"})
        _write_json(local_file, {"model": "haiku"})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=base_file,
            keys=["model"],
        )

        assert conflicts == []

    def test_three_way_only_local_changed(self, tmp_dir: Path) -> None:
        """Three-way: only local changed -> no conflict (safe to keep local)."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"
        base_file = tmp_dir / "base" / "settings.json"

        _write_json(base_file, {"model": "haiku"})
        _write_json(repo_file, {"model": "haiku"})
        _write_json(local_file, {"model": "sonnet"})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=base_file,
            keys=["model"],
        )

        assert conflicts == []

    def test_three_way_both_changed_same_way(self, tmp_dir: Path) -> None:
        """Three-way: both changed but to the same value -> no conflict."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"
        base_file = tmp_dir / "base" / "settings.json"

        _write_json(base_file, {"model": "haiku"})
        _write_json(repo_file, {"model": "opus"})
        _write_json(local_file, {"model": "opus"})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=base_file,
            keys=["model"],
        )

        assert conflicts == []

    def test_conflict_detection_nested_key(self, tmp_dir: Path) -> None:
        """Conflict detection works with dot-notation nested keys."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"editor": {"fontSize": 16}})
        _write_json(local_file, {"editor": {"fontSize": 12}})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=None,
            keys=["editor.fontSize"],
        )

        assert "editor.fontSize" in conflicts

    def test_conflict_detection_missing_key_one_side(self, tmp_dir: Path) -> None:
        """Key exists in repo but not local -> conflict (two-way)."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"plugins": ["a"]})
        _write_json(local_file, {"other": 1})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=None,
            keys=["plugins"],
        )

        assert "plugins" in conflicts

    def test_conflict_detection_missing_both_sides(self, tmp_dir: Path) -> None:
        """Key missing from both -> no conflict (both agree on absence)."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"

        _write_json(repo_file, {"a": 1})
        _write_json(local_file, {"b": 2})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=None,
            keys=["nonexistent"],
        )

        assert conflicts == []

    def test_conflict_detection_local_missing_file(self, tmp_dir: Path) -> None:
        """Local file doesn't exist -> all synced keys that exist in repo are conflicts."""
        repo_file = tmp_dir / "repo" / "settings.json"
        local_file = tmp_dir / "local" / "settings.json"  # does not exist

        _write_json(repo_file, {"plugins": ["a"], "model": "opus"})

        conflicts = detect_key_conflicts(
            repo_file, local_file, base_file=None,
            keys=["plugins", "model"],
        )

        assert "plugins" in conflicts
        assert "model" in conflicts


# ===========================================================================
# read_config / write_config round-trips
# ===========================================================================

class TestConfigIO:
    """Verify read_config and write_config work for all formats."""

    def test_json_roundtrip(self, tmp_dir: Path) -> None:
        path = tmp_dir / "test.json"
        data = {"key": "value", "nested": {"a": 1}}
        write_config(path, data)
        assert read_config(path) == data

    def test_yaml_roundtrip(self, tmp_dir: Path) -> None:
        path = tmp_dir / "test.yaml"
        data = {"key": "value", "nested": {"a": 1}}
        write_config(path, data)
        assert read_config(path) == data

    def test_yml_extension(self, tmp_dir: Path) -> None:
        path = tmp_dir / "test.yml"
        data = {"items": [1, 2, 3]}
        write_config(path, data)
        assert read_config(path) == data

    def test_toml_read(self, tmp_dir: Path) -> None:
        """TOML read via tomllib."""
        path = tmp_dir / "test.toml"
        _write_toml(path, 'name = "test"\nversion = 1\n\n[section]\nkey = "val"\n')
        data = read_config(path)
        assert data["name"] == "test"
        assert data["version"] == 1
        assert data["section"]["key"] == "val"

    def test_toml_write_roundtrip(self, tmp_dir: Path) -> None:
        """TOML write via custom serializer, then read back."""
        path = tmp_dir / "test.toml"
        data = {"name": "test", "version": 1, "section": {"key": "val"}}
        write_config(path, data)
        result = read_config(path)
        assert result["name"] == "test"
        assert result["version"] == 1
        assert result["section"]["key"] == "val"

    def test_unsupported_format_read(self, tmp_dir: Path) -> None:
        path = tmp_dir / "test.ini"
        path.write_text("[section]\nkey=val\n")
        with pytest.raises(ValueError, match="Unsupported"):
            read_config(path)

    def test_unsupported_format_write(self, tmp_dir: Path) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            write_config(tmp_dir / "test.ini", {"a": 1})

    def test_file_not_found(self, tmp_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_config(tmp_dir / "does_not_exist.json")

    def test_empty_yaml_returns_empty_dict(self, tmp_dir: Path) -> None:
        path = tmp_dir / "empty.yaml"
        path.write_text("", encoding="utf-8")
        assert read_config(path) == {}
