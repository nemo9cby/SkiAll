# SkiAll TODOs

## Tracked Items

### `skiall init` — Auto-generate manifest from current machine state
**Status:** Not started
**Priority:** High (v1 feature)
**What:** Add a `skiall init` (or fold into `skiall setup`) command that scans `~/.claude/`, `~/.codex/`, and `~/.agents/` to auto-generate `skiall.yaml` by detecting installed platforms, skills, plugins, bundles, and symlinks.
**Why:** Writing the manifest by hand is tedious and error-prone. Auto-detection transforms onboarding from "figure out the YAML format" to "run one command, review, done." This is the "whoa" moment.
**Depends on:** All three adapters' `detect()` and `get_paths()` methods must be implemented first.
**Context:** The design doc calls this `skiall setup`. The key insight is that the adapter's `detect()` + `get_paths()` + inspection of the actual file system gives us enough info to generate the manifest automatically. Should present the generated YAML to the user for review before committing.
