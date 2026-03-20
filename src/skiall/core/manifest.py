"""Manifest reader/writer for skiall.yaml.

The manifest declares which platforms to sync, what files to include,
and any overrides for symlinks, bundles, or plugins.

Example skiall.yaml:
    platforms:
      claude-code:
        sync:
          - CLAUDE.md
          - settings.json:
              keys: [enabledPlugins, effortLevel]
          - memory/*
          - skills/*
        plugins:
          - superpowers@claude-plugins-official
        bundles:
          - name: gstack
            repo: github.com/garryslist/gstack
            path: skills/gstack
        symlinks:
          - name: browse
            target: gstack/browse
      shared:
        sync:
          - "*"
      codex:
        sync:
          - config.toml:
              keys: [model, model_reasoning_effort, personality, features]
          - AGENTS.md
          - memories/*
"""

from __future__ import annotations

from pathlib import Path

import yaml

from skiall.core.types import Bundle, Plugin, Symlink, SyncRule, SyncType


def load_manifest(path: Path) -> dict:
    """Load and parse skiall.yaml, returning the raw dict."""
    if not path.exists():
        return {"platforms": {}}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data


def save_manifest(path: Path, data: dict) -> None:
    """Write manifest dict back to skiall.yaml."""
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def parse_sync_rules(raw_list: list) -> list[SyncRule]:
    """Parse the 'sync' list from a platform section into SyncRules.

    Handles both simple strings ("CLAUDE.md") and dicts with keys:
        {"settings.json": {"keys": ["enabledPlugins"]}}
    """
    rules = []
    for item in raw_list:
        if isinstance(item, str):
            rules.append(SyncRule(path=item, sync_type=SyncType.FULL))
        elif isinstance(item, dict):
            for file_path, opts in item.items():
                if isinstance(opts, dict) and "keys" in opts:
                    rules.append(
                        SyncRule(
                            path=file_path,
                            sync_type=SyncType.PARTIAL,
                            keys=opts["keys"],
                        )
                    )
                else:
                    rules.append(SyncRule(path=file_path, sync_type=SyncType.FULL))
    return rules


def parse_symlinks(raw_list: list) -> list[Symlink]:
    """Parse the 'symlinks' list from a platform section."""
    symlinks = []
    for item in raw_list:
        if isinstance(item, dict):
            name = item.get("name", "")
            target = item.get("target", "")
            if name and target:
                symlinks.append(Symlink(name=name, target=target))
    return symlinks


def parse_bundles(raw_list: list) -> list[Bundle]:
    """Parse the 'bundles' list from a platform section."""
    bundles = []
    for item in raw_list:
        if isinstance(item, dict):
            bundles.append(
                Bundle(
                    name=item.get("name", ""),
                    repo=item.get("repo", ""),
                    path=item.get("path", ""),
                    ref=item.get("ref", "main"),
                )
            )
    return bundles


def parse_plugins(raw_list: list) -> list[Plugin]:
    """Parse the 'plugins' list from a platform section."""
    return [Plugin(name=item) for item in raw_list if isinstance(item, str)]


def generate_manifest_from_adapters(adapters: list) -> dict:
    """Auto-generate a manifest by scanning detected adapters.

    Used by `skiall setup` to create the initial skiall.yaml.
    """
    platforms = {}
    for adapter in adapters:
        if not adapter.detect():
            continue
        section: dict = {"sync": []}
        for rule in adapter.get_sync_rules():
            if rule.sync_type == SyncType.FULL:
                section["sync"].append(rule.path)
            else:
                section["sync"].append({rule.path: {"keys": rule.keys}})
        symlinks = adapter.get_symlinks()
        if symlinks:
            section["symlinks"] = [
                {"name": s.name, "target": s.target} for s in symlinks
            ]
        bundles = adapter.get_bundles()
        if bundles:
            section["bundles"] = [
                {"name": b.name, "repo": b.repo, "path": b.path, "ref": b.ref}
                for b in bundles
            ]
        plugins = adapter.get_plugins()
        if plugins:
            section["plugins"] = [p.name for p in plugins]
        platforms[adapter.name] = section
    return {"platforms": platforms}
