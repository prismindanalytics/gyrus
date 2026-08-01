# Changelog

## 0.4.0 — 2026-08-01

**The dogfood release: chunked merges, honest telemetry, junk-slug quarantine, and doc surfaces that upgrade themselves.**

### Fixed
- **Merge death spiral**: merges now process pending thoughts oldest-first in batches (`merge.batch_size`, default 40; at most `merge.max_batches_per_page_per_run` per page per run) instead of stuffing an ever-growing backlog into one prompt that local models could never finish within the timeout. Consecutive failures halve the batch (floor 5), and a batch that still cannot merge is dead-lettered with a visible warning instead of wedging the queue forever.
- **Telemetry honesty**: `runs.jsonl` `pages_updated` lists only pages whose merge actually saved; new `merge_failed`, `backlog_remaining`, and `dead_lettered` fields. Local models estimate $0.00 instead of cloud rates.
- **Junk slugs**: the slugify preserves separators instead of deleting them (`calledthird/research/x` no longer crushes into a new identity), path-like names resolve by segment against existing pages first, junk names (`none`, UUIDs, prompt-fragment sentence slugs from Codex scratch dirs) are quarantined onto an `unsorted` page instead of minted, and the fuzzy matcher never attaches variants to a junk canonical.
- **Unreachable me.md/ideas.md**: they migrate eagerly to the KB root on startup instead of waiting for a successful merge, so every documented path works even when merges are failing.
- **Frozen doc blocks**: pre-marker legacy blocks in `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` upgrade in place to managed marker blocks (only when every line is provably installer-written); the installer and `gyrus update` both route through the same writer, so shipped guidance improvements finally reach existing installs. Codex's block gains the pointer to its full instructions file.
- **Status contract**: one shared normalizer maps the vocabulary models actually write (`Prototype`, `BACKLOG`, `Pre-launch`, …) onto `active/paused/dormant/killed/brainstorm/shipped`; new pages default to `active`; unknown pages with activity in the last 14 days surface as active; manual overrides win everywhere, including the interactive review.
- Poison-pill sessions dead-letter after 3 failed extraction attempts (surfaced in `gyrus doctor`), the timeout clamp ceiling rises to 1800s, `ingest.log` rotates at 5 MB, and the `\r` progress spinner stays off non-TTY output.

### Changed
- `gyrus merge` refuses junk targets, carries source pages' Key Decisions/Timeline bullets into the target, parks source pages as `.premerge.` snapshots instead of deleting them, rewrites `merged_into_page`, and proposes junk-identity consolidations in its suggestion flow.
- The merge prompt assigns each event to exactly one of Key Decisions or Timeline & History, and validation permits consolidation (cross-section exact duplicates collapse; same-dated close paraphrases are accepted) while still restoring genuinely dropped append-only lines.
- `latest-digest.md` is written on every ingest run (digest email remains opt-in), and the `/gyrus` command leads with `gyrus context`, discovers namespaced MCP tools instead of assuming flat names, and excludes snapshots and personal pages from export-all.

## 0.3.6

### Added
- Import Claude Code auto-memory (`~/.claude/projects/*/memory/*.md`) as a session source: distilled `user`/`feedback` facts feed `me.md` and `project` facts feed the matching page, re-read only when a fact file changes. Exclude with `"claude-memory"` in `excluded_tools`.

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
