# SkiAll

Cross-device agent skill sync with platform adapters. Dotfiles for the AI coding agent era.

## What it does

Syncs your Claude Code skills, Codex config, and shared agent plugins across all your machines with one command. SkiAll knows where each platform stores its config and handles the last-mile plumbing that makes a git repo insufficient on its own.

## Quick start

```bash
# Install
pip install -e .  # or: uv pip install -e .

# First machine: snapshot your current setup
skiall setup --remote git@github.com:you/my-agent-config.git
skiall push -m "Initial snapshot"

# Other machines: clone and deploy
skiall clone git@github.com:you/my-agent-config.git
```

## Commands

| Command | Description |
|---------|-------------|
| `skiall setup [--remote URL]` | Initialize sync repo from current machine |
| `skiall clone <repo-url>` | Join an existing sync repo on a new machine |
| `skiall pull [--force]` | Deploy repo state to local platforms |
| `skiall push [-m MESSAGE]` | Collect local state and push to repo |
| `skiall status` | Show sync status across all platforms |
| `skiall diff` | Show detailed changes |

## Supported platforms

| Platform | Config dir | What syncs |
|----------|-----------|------------|
| Claude Code | `~/.claude/` | CLAUDE.md, settings (partial), skills, memory, plugin manifest |
| Codex | `~/.codex/` | config.toml (partial), memories, AGENTS.md |
| Shared | `~/.agents/skills/` | Cross-platform skills (agent-browser, docx, etc.) |

## How it works

```
Your Machine A                 Git Repo                 Your Machine B
~/.claude/skills/ ──push──►  skiall-repo/  ──pull──►  ~/.claude/skills/
~/.codex/config   ──push──►  claude-code/  ──pull──►  ~/.codex/config
~/.agents/skills/ ──push──►  shared/       ──pull──►  ~/.agents/skills/
                              codex/
```

Each platform has an **adapter** that knows:
- Where configs live on each OS
- Which files to sync (and which to exclude)
- How to do partial key-level merges (e.g. sync `enabledPlugins` from settings.json without touching `permissions`)

## Safety

- **Secrets never enter the repo**: triple-layer exclusion (hardcoded adapter rules + .gitignore + pre-commit hook)
- **Conflict detection**: warns before overwriting local changes (use `--force` to override)
- **Partial sync**: machine-specific config sections (like Codex `[projects]` paths) are never touched
