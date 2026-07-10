# Changelog

## 0.3.0 — 2026-07-09

**Public-readiness, safer ingestion, and a shared Claude/Codex handoff command.**

### Added
- `gyrus context --cwd <repo>` emits the same bounded, freshness-aware project context for Claude and Codex.
- Context output includes clearly labeled pending extracted evidence when a merge is still in progress.
- Modern Claude Code/Codex transcript parsing, duplicate-turn suppression, and head/tail context bounding.
- Validated merge output with append-only history preservation and retryable pending thoughts.
- Secret redaction, scoped opt-in memory-file context, local-profile opt-in, atomic private storage, and sync allowlisting.
- Packaged wheel/sdist metadata and a contributor guide.

### Fixed
- Failed model calls no longer advance session checkpoints.
- `me.md` and `ideas.md` are stored at the documented knowledge-base root, with legacy reads supported.
- Fully local extraction/merge configurations now pass startup validation.
- Installed CLI supports documented subcommands (`gyrus doctor`, `gyrus context`, etc.).
- Codex global instructions target `$CODEX_HOME/AGENTS.md` on all platforms.
- Notion special pages and thought deduplication now match the local adapter's behavior.

## 0.2.0 — 2026-04-19

**GitHub-first cross-machine sync + hardening against silent failures.**

Breaking-ish: iCloud / Dropbox / Google Drive / OneDrive are no longer offered as
storage options in the installer. The failure mode they produce (dataless-file
hangs that kill cron runs silently) is the inverse of what people sign up for.

### Added
- `gyrus init` — first-time setup wizard (storage, API key, GitHub, cron)
- `gyrus init --clone <url>` — second-machine bootstrap from an existing repo
- `gyrus doctor` — one-command health check covering storage location, dataless
  files, freshness, schedule, git sync, API keys, session sources, backlog, lockfile
- `gyrus sync` — manual pull + push of the GitHub remote
- Auto-sync on every run: `git pull --rebase --autostash` before work,
  `git commit && git push` after. Non-fatal: never blocks local ingest.
- Heartbeat line on every invocation so ingest staleness is never silent
- Cloud-sync path detection (iCloud / Dropbox / Google Drive / OneDrive / Box /
  Sync.com / pCloud / Proton Drive) with loud warnings during install and via `doctor`

### Changed
- Storage default is now `~/gyrus-local` with `~/.gyrus` as a symlink
- `_get_project_recency` streams thoughts files with per-file timeout +
  dataless-skip so `gyrus status` cannot hang on a stuck iCloud file
- Cron command simplified (no more `/tmp/gyrus_run` copy-before-run hack)
- Version bumped to 2026.04.19.0

### Removed
- Cloud-sync retry helpers (`_ensure_downloaded`, retry-on-EDEADLK) — replaced by
  plain file I/O once we stopped recommending cloud-sync folders as storage

## 0.1.0 — 2026-04-04

Initial release.

- Extract insights from AI coding tools: Claude Code, Claude Cowork, Codex, Antigravity, Cursor (more on request)
- Build iterative wiki-style knowledge pages per project (markdown)
- Multi-provider LLM support: Anthropic (Haiku/Sonnet/Opus), OpenAI (GPT 5.4 series), Google (Gemini 3.1 series)
- Configurable sync frequency during install (30 min / hourly / 4h / 12h / daily)
- Cross-machine sync via iCloud, Dropbox, Git, or Obsidian
- Optional Notion storage adapter
- Lockfile for cloud drive conflict prevention
- Personal memory page (`me.md`) for non-project knowledge
- Tool skill installation (`/gyrus` for Claude Code, instructions for Codex)
- Self-update via `--update` flag
- Interactive installer for macOS/Linux and Windows
- Cost tracking per ingestion run
- 37 unit tests
