"""SkiAll CLI — cross-device agent skill sync.

Commands:
    skiall setup [--remote URL]   Initialize sync repo from current machine
    skiall clone <repo-url>       Join an existing sync repo on a new machine
    skiall pull [--force]         Deploy repo state to local platforms
    skiall push [-m MESSAGE]      Collect local state and push to repo
    skiall status                 Show sync status across all platforms
    skiall diff                   Show detailed changes
    skiall info                   Show what's installed locally (no repo needed)
"""

from __future__ import annotations

from pathlib import Path

import click

from skiall.core.types import ChangeKind

DEFAULT_REPO_DIR = Path.home() / ".skiall"


def _make_engine(repo_dir: Path):
    """Create the engine with all known adapters."""
    from skiall.adapters.claude_code import ClaudeCodeAdapter
    from skiall.adapters.codex import CodexAdapter
    from skiall.adapters.shared import SharedAdapter
    from skiall.core.engine import Engine

    adapters = [
        SharedAdapter(repo_dir),
        ClaudeCodeAdapter(repo_dir),
        CodexAdapter(repo_dir),
    ]
    return Engine(repo_dir, adapters)


@click.group()
@click.option(
    "--repo-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_REPO_DIR,
    envvar="SKIALL_REPO",
    help="Path to the SkiAll sync repo.",
)
@click.pass_context
def cli(ctx: click.Context, repo_dir: Path) -> None:
    """SkiAll — sync your AI agent skills across devices."""
    ctx.ensure_object(dict)
    ctx.obj["repo_dir"] = repo_dir


@cli.command()
@click.option("--remote", default=None, help="Git remote URL for the sync repo.")
@click.pass_context
def setup(ctx: click.Context, remote: str | None) -> None:
    """Initialize a new SkiAll sync repo from your current machine."""
    repo_dir = ctx.obj["repo_dir"]
    engine = _make_engine(repo_dir)

    click.echo(f"Initializing SkiAll repo at {repo_dir}...")
    manifest = engine.setup(remote_url=remote)

    platforms = manifest.get("platforms", {})
    click.echo(f"\nDetected {len(platforms)} platform(s):")
    for name in platforms:
        click.echo(f"  - {name}")

    click.echo(f"\nManifest written to {repo_dir / 'skiall.yaml'}")
    click.echo("Run 'skiall push' to snapshot your current config.")


@cli.command("clone")
@click.argument("repo_url")
@click.pass_context
def clone_cmd(ctx: click.Context, repo_url: str) -> None:
    """Clone an existing SkiAll repo onto this machine."""
    repo_dir = ctx.obj["repo_dir"]
    engine = _make_engine(repo_dir)

    click.echo(f"Cloning {repo_url} to {repo_dir}...")
    manifest = engine.clone(repo_url)

    platforms = manifest.get("platforms", {})
    click.echo(f"\nDeployed {len(platforms)} platform(s):")
    for name in platforms:
        click.echo(f"  - {name}")
    click.echo("\nDone! Your agent environment is ready.")


@cli.command()
@click.option("--force", is_flag=True, help="Overwrite local changes without warning.")
@click.pass_context
def pull(ctx: click.Context, force: bool) -> None:
    """Pull latest config from repo and deploy to local platforms."""
    repo_dir = ctx.obj["repo_dir"]
    if not repo_dir.exists():
        click.echo("No SkiAll repo found. Run 'skiall setup' or 'skiall clone' first.", err=True)
        raise SystemExit(1)

    engine = _make_engine(repo_dir)
    reports = engine.pull(force=force)

    has_errors = False
    for report in reports:
        _print_report(report)
        if not report.success:
            has_errors = True

    if has_errors:
        raise SystemExit(1)


@cli.command()
@click.option("-m", "--message", default="skiall push", help="Commit message.")
@click.pass_context
def push(ctx: click.Context, message: str) -> None:
    """Collect local config and push to the sync repo."""
    repo_dir = ctx.obj["repo_dir"]
    if not repo_dir.exists():
        click.echo("No SkiAll repo found. Run 'skiall setup' first.", err=True)
        raise SystemExit(1)

    engine = _make_engine(repo_dir)
    try:
        reports = engine.push(message=message)
    except RuntimeError as e:
        click.echo(f"ERROR: {e}", err=True)
        raise SystemExit(1)

    for report in reports:
        _print_report(report)

    if any(not r.success for r in reports):
        raise SystemExit(1)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show sync status for all platforms."""
    repo_dir = ctx.obj["repo_dir"]
    if not repo_dir.exists():
        click.echo("No SkiAll repo found. Run 'skiall setup' or 'skiall clone' first.", err=True)
        raise SystemExit(1)

    engine = _make_engine(repo_dir)
    all_changes = engine.status()

    for adapter_name, changes in all_changes.items():
        click.echo(f"\n[{adapter_name}]")
        if not changes:
            click.echo("  In sync")
            continue
        for change in changes:
            icon = {
                ChangeKind.ADDED: "+",
                ChangeKind.MODIFIED: "~",
                ChangeKind.DELETED: "-",
                ChangeKind.UNCHANGED: " ",
            }.get(change.kind, "?")
            detail = f" ({change.detail})" if change.detail else ""
            click.echo(f"  {icon} {change.path}{detail}")


@cli.command()
@click.pass_context
def diff(ctx: click.Context) -> None:
    """Show detailed changes between repo and local state."""
    repo_dir = ctx.obj["repo_dir"]
    if not repo_dir.exists():
        click.echo("No SkiAll repo found.", err=True)
        raise SystemExit(1)

    engine = _make_engine(repo_dir)
    all_changes = engine.status()

    total_changes = 0
    for adapter_name, changes in all_changes.items():
        meaningful = [c for c in changes if c.kind != ChangeKind.UNCHANGED]
        if not meaningful:
            continue
        click.echo(f"\n=== {adapter_name} ===")
        for change in meaningful:
            click.echo(f"  [{change.kind.value}] {change.path}")
            if change.detail:
                click.echo(f"           {change.detail}")
            total_changes += 1

    if total_changes == 0:
        click.echo("Everything in sync.")


@cli.command()
@click.argument("repo_url", required=False, default=None)
@click.option("-m", "--message", default="skiall sync", help="Commit message.")
@click.option(
    "-s", "--skip-conflicts", is_flag=True,
    help="Skip all conflicts instead of prompting interactively.",
)
@click.option(
    "-l", "--keep-local", is_flag=True,
    help="Keep local version for all conflicts.",
)
@click.option(
    "-r", "--keep-remote", is_flag=True,
    help="Keep remote version for all conflicts.",
)
@click.pass_context
def sync(
    ctx: click.Context,
    repo_url: str | None,
    message: str,
    skip_conflicts: bool,
    keep_local: bool,
    keep_remote: bool,
) -> None:
    """Pull remote config, merge with local state, and push back.

    First time: skiall sync <repo-url>
    After that: skiall sync
    """
    conflict_strategy = None
    if skip_conflicts:
        conflict_strategy = "skip"
    elif keep_local:
        conflict_strategy = "local"
    elif keep_remote:
        conflict_strategy = "remote"

    repo_dir = ctx.obj["repo_dir"]
    engine = _make_engine(repo_dir)

    try:
        reports = engine.sync(
            remote_url=repo_url, message=message, conflict_strategy=conflict_strategy
        )
    except RuntimeError as e:
        click.echo(f"ERROR: {e}", err=True)
        raise SystemExit(1)

    has_errors = False
    for report in reports:
        _print_report(report)
        if not report.success:
            has_errors = True

    if has_errors:
        raise SystemExit(1)
    else:
        click.echo("\nSync complete!")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
@click.pass_context
def info(ctx: click.Context, as_json: bool) -> None:
    """Show what's installed locally across all agent platforms (no repo needed)."""
    import json as json_mod

    repo_dir = ctx.obj["repo_dir"]
    # info doesn't need a repo — use a dummy path if it doesn't exist
    engine = _make_engine(repo_dir if repo_dir.exists() else Path("/tmp/skiall-dummy"))

    all_info = []
    for adapter in engine.resolve_order():
        all_info.append(adapter.info())

    if as_json:
        click.echo(json_mod.dumps(all_info, indent=2))
        return

    for platform in all_info:
        name = platform["name"]
        detected = platform.get("detected", False)

        click.echo(f"\n{'=' * 60}")
        click.echo(f"  {name.upper()}")
        click.echo(f"{'=' * 60}")

        if not detected:
            click.echo("  Not installed")
            continue

        click.echo(f"  Config dir: {platform.get('config_dir', '?')}")

        # Claude Code specifics
        if name == "claude-code":
            _print_claude_info(platform)
        elif name == "codex":
            _print_codex_info(platform)
        elif name == "shared":
            _print_shared_info(platform)


def _print_claude_info(p: dict) -> None:
    """Print Claude Code info section."""
    if p.get("claude_md"):
        click.echo(f"  CLAUDE.md: {p['claude_md']}")

    settings = p.get("settings")
    if settings and not settings.get("error"):
        click.echo(f"  Settings: mode={settings.get('defaultMode', '?')}, effort={settings.get('effortLevel', '?')}")

    # Plugins
    plugins = p.get("plugins", [])
    if plugins:
        click.echo(f"\n  Plugins ({len(plugins)}):")
        for pl in plugins:
            click.echo(f"    {pl['name']}  (v{pl['version']}, {pl['scope']})")

    # Bundles (git repos containing multiple skills)
    bundles = p.get("bundles", [])
    if bundles:
        click.echo(f"\n  Skill Bundles ({len(bundles)}):")
        for b in bundles:
            click.echo(f"    {b['name']}/  ({b['skill_count']} skills)")
            for skill in b["skills"]:
                click.echo(f"      - {skill}")

    # Standalone skills (not part of a bundle)
    standalone = p.get("skills_standalone", [])
    if standalone:
        click.echo(f"\n  Standalone Skills ({len(standalone)}):")
        for s in standalone:
            click.echo(f"    {s['name']}/")

    # Symlinked skills from external sources (not from bundles)
    symlinked = p.get("skills_symlinked", [])
    if symlinked:
        click.echo(f"\n  External Skill Links ({len(symlinked)}):")
        for s in symlinked:
            click.echo(f"    {s['name']} -> {s['target']}")

    # Memory
    memory = p.get("memory_files", [])
    if memory:
        click.echo(f"\n  Memory ({len(memory)}):")
        for m in memory:
            click.echo(f"    {m}")


def _print_codex_info(p: dict) -> None:
    """Print Codex info section."""
    config = p.get("config")
    if config and not config.get("error"):
        click.echo(f"  Model: {config.get('model', '?')}")
        click.echo(f"  Reasoning effort: {config.get('model_reasoning_effort', '?')}")
        click.echo(f"  Personality: {config.get('personality', '?')}")
        features = config.get("features")
        if features:
            enabled = [k for k, v in features.items() if v]
            if enabled:
                click.echo(f"  Features: {', '.join(enabled)}")

    if p.get("agents_md"):
        click.echo(f"  AGENTS.md: {p['agents_md']}")

    skills = p.get("skills", [])
    if skills:
        click.echo(f"\n  Skills ({len(skills)}):")
        for s in skills:
            if s["type"] == "symlink":
                click.echo(f"    {s['name']} -> {s['target']}")
            else:
                click.echo(f"    {s['name']}/")

    memory = p.get("memory_files", [])
    if memory:
        click.echo(f"\n  Memories ({len(memory)}):")
        for m in memory:
            click.echo(f"    {m}")


def _print_shared_info(p: dict) -> None:
    """Print Shared skills info section."""
    skills = p.get("skills", [])
    if skills:
        click.echo(f"\n  Skills ({len(skills)}):")
        for s in skills:
            extra = ""
            if s["type"] == "symlink":
                extra = f" -> {s['target']}"
            elif "files" in s:
                extra = f"  ({s['files']} files)"
            click.echo(f"    {s['name']}/{extra}")


def _print_report(report: SyncReport) -> None:
    """Pretty-print a sync report."""
    click.echo(f"\n[{report.adapter_name}]")
    if report.files_synced:
        click.echo(f"  Synced: {len(report.files_synced)} file(s)")
        for f in report.files_synced:
            click.echo(f"    {f}")
    if report.files_skipped:
        click.echo(f"  Skipped: {len(report.files_skipped)} file(s)")
    for w in report.warnings:
        click.echo(f"  WARNING: {w}")
    for e in report.errors:
        click.echo(f"  ERROR: {e}")


if __name__ == "__main__":
    cli()
