"""Tests for manifest parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from skiall.core.manifest import (
    generate_manifest_from_adapters,
    load_manifest,
    parse_bundles,
    parse_plugins,
    parse_symlinks,
    parse_sync_rules,
    save_manifest,
)
from skiall.core.types import SyncType


class TestLoadSave:
    def test_load_nonexistent_returns_empty(self, tmp_path: Path):
        result = load_manifest(tmp_path / "nope.yaml")
        assert result == {"platforms": {}}

    def test_round_trip(self, tmp_path: Path):
        path = tmp_path / "skiall.yaml"
        data = {"platforms": {"claude-code": {"sync": ["CLAUDE.md"]}}}
        save_manifest(path, data)
        loaded = load_manifest(path)
        assert loaded == data


class TestParseSyncRules:
    def test_simple_string(self):
        rules = parse_sync_rules(["CLAUDE.md", "memory/*"])
        assert len(rules) == 2
        assert rules[0].path == "CLAUDE.md"
        assert rules[0].sync_type == SyncType.FULL

    def test_partial_sync_with_keys(self):
        rules = parse_sync_rules([{"settings.json": {"keys": ["enabledPlugins", "effortLevel"]}}])
        assert len(rules) == 1
        assert rules[0].sync_type == SyncType.PARTIAL
        assert rules[0].keys == ["enabledPlugins", "effortLevel"]

    def test_mixed_rules(self):
        raw = [
            "CLAUDE.md",
            {"settings.json": {"keys": ["enabledPlugins"]}},
            "memory/*",
        ]
        rules = parse_sync_rules(raw)
        assert len(rules) == 3
        assert rules[0].sync_type == SyncType.FULL
        assert rules[1].sync_type == SyncType.PARTIAL
        assert rules[2].sync_type == SyncType.FULL


class TestParseSymlinks:
    def test_parse_symlinks(self):
        raw = [{"name": "browse", "target": "gstack/browse"}]
        result = parse_symlinks(raw)
        assert len(result) == 1
        assert result[0].name == "browse"
        assert result[0].target == "gstack/browse"

    def test_empty_list(self):
        assert parse_symlinks([]) == []


class TestParseBundles:
    def test_parse_bundle(self):
        raw = [{"name": "gstack", "repo": "github.com/garryslist/gstack", "path": "skills/gstack"}]
        result = parse_bundles(raw)
        assert len(result) == 1
        assert result[0].name == "gstack"
        assert result[0].ref == "main"

    def test_custom_ref(self):
        raw = [{"name": "x", "repo": "url", "path": "p", "ref": "v2"}]
        result = parse_bundles(raw)
        assert result[0].ref == "v2"


class TestParsePlugins:
    def test_parse_plugins(self):
        raw = ["superpowers@official", "playwright@official"]
        result = parse_plugins(raw)
        assert len(result) == 2
        assert result[0].name == "superpowers@official"
