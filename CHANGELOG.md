# Changelog

## 0.5.0 — 2026-09-28

**Handoff cards: bounded project summaries, freshness in-band, and Claude's native memory bridged to other tools.**

Seven weeks after 0.4.0 the live pipeline was failing again: the four busiest project pages had grown to the 16K-token output limit (about 64KB), so every whole-page merge came back without its last section and was rejected (3,913 times), and since 2026-09-19 every merge 404'd because the configured merge model had been removed from Ollama — reported as "is a local LLM server running?". Nothing surfaced either failure to a person.

### Changed
- **Project pages are now handoff cards.** Each run rebuilds a project's card from the previous card plus its new notes, topped up with recent context, in bounded model calls (`role="card"`, 4K output tokens). Sections: Status, Overview, Current Focus, Recent Decisions, Open Questions & Blockers, Next Steps, Durable Context, plus a verbatim `## Manual Notes`. Output is clipped to fixed per-section limits, so card size no longer grows with history. A model response missing a section is repaired from the previous card instead of rejected.
- **Backlogs catch up chronologically:** pending notes are summarized in arrival order, one prompt-sized pass at a time, each pass building on the card the previous one wrote, so the final card reflects the newest notes. A note is marked processed only by the pass that showed it to the model. Per run: at most `cards.max_per_run` projects (default 12, busiest first) and `cards.max_calls_per_run` model calls (default 60); the rest stays pending.
- A project's long-form page is condensed into its first successful card and archived to `projects.archive/` (synced, invisible to page discovery). `gyrus --backfill` rebuilds every card now.
- **Model-free fallback:** when the summary call fails for a project with new notes, its card is rewritten from the previous card's durable sections plus the newest raw notes, marked as unsummarized; the notes stay pending until a model succeeds. A long-form page is never replaced this way, and a card with no new notes is left untouched.
- `gyrus merge` into a card parks the source pages' history in a `Carried From Merged Pages` section and schedules the card for rebuild on the next run.
- Managed `CLAUDE.md`/`AGENTS.md`/`GEMINI.md` blocks are tool-specific. Claude Code's block treats its own auto-memory as the primary record and Gyrus as the cross-tool supplement (no read-first mandate). Codex and Antigravity are told to run `gyrus context --cwd "$PWD" --tool <tool>` before project work. All blocks point at the freshness line and drop the stale `me.md`/digest pointers.
- `status.md` ranks active projects by activity: this week / this month / quiet 30+ days, with 7-day and 30-day note counts. Junk-looking slugs and projects with ≤3 notes that stopped a month ago move to a **Needs sorting** bucket.

### Added
- `gyrus context` opens with a **freshness line**: when the card was built and which notes it covers, or a warning when it is a legacy page, was written without a model, has unsummarized notes, or summaries are failing.
- `gyrus context --tool codex` (any tool other than `claude-code`) appends **Claude Code's native memory** for the directory or its nearest parent — deterministic, no model, newest first, bounded. Works even when no Gyrus card matches.
- Every `gyrus context` call is logged to `context-log.jsonl` (local, never synced); `gyrus doctor` reports calls per tool over 7 days and the share that served a stale card.
- `gyrus doctor` checks that configured local models are installed (`models`) and whether recent runs saved any summary (`summaries`).
- Desktop notification (macOS) after 3 consecutive runs in which every summary failed, at most once a day. Disable with `"notifications": false`.
- A 404 from a local server now names the missing model and lists the installed ones.

### Models
- **Current model generation.** `sonnet` is now Claude Sonnet 5 (`claude-sonnet-5`, $2/$10 per MTok) and `opus` Claude Opus 5 (`claude-opus-5`); new `fable` (Claude Fable 5.1). OpenAI gains `gpt-6-astra`, `gpt-6-sol` and `gpt-6-luna`; `gpt-5.4-pro` is removed (Responses-only), and `gpt-4.1-nano`, `o4-mini` (retiring 2026-10-23) and `o3` (2026-12-11) are flagged. `gemini-flash` is now Gemini 3.8 Flash and `gemini-lite` Gemini 3.5 Flash-Lite; the old `gemini-3.1-flash-lite-preview` target was shut down on 2026-05-25. Prices updated from the providers' pricing pages; local catalog gains `qwen3.8-27b`.
- **Defaults:** extraction defaults to `gpt-6-luna` (was `gpt-4.1-mini`); the card model default stays `sonnet`, now Sonnet 5. The installer's OpenAI-only default for cards is `gpt-6-sol`.
- **Request shapes for reasoning models.** Sonnet 5, Opus 4.7+/5.x, Fable, GPT-6 and Gemini 3 reject or misbehave with `temperature=0` and think by default. Gyrus now omits sampling parameters for them, sets effort instead (`low` for extraction, `medium` for cards: `output_config.effort`, `reasoning_effort`, `thinkingConfig.thinkingLevel`), leaves room for reasoning inside the output cap, reads the text block rather than `content[0]` (a thinking block on these models), and raises on a refusal or an empty answer instead of parsing nothing. Opus 5 and Fable 5.1 requests opt into server-side refusal fallbacks (`fallbacks: "default"`). Older models keep `temperature=0`.

### Fixed
- **Malformed extraction JSON from local models:** gemma4:26b intermittently emitted a stray token before a key (`    / "tags": [`), `//` comments, trailing commas, or a cut-off array, and each such session was retried three times and dead-lettered (74 on the dogfood machine). After a strict parse fails, the parser repairs those defects (plus a key whose opening quote was replaced by junk, `    _tags": [`, junk-only lines and doubled commas), and as a last resort keeps every object that parses on its own, so one corrupted note costs that note rather than the session. Output with no recoverable object still fails.
- `gyrus doctor --fix` re-queues dead-lettered sessions. The previous advice (delete `dead_letter_sessions`) never retried anything, because dead-lettered sessions are also checkpointed as processed.
- **Re-extraction churn:** a session already extracted once is not re-extracted while it is still being written (`session_settle_minutes`, default 45), for at most `session_max_defer_hours` (default 6). One live session had yielded 562 thoughts from hourly re-extraction.
- Notes whose model-reported `occurred_at` is later than the session date use the session date (live data had notes dated days in the future).
- **Hour-long runs:** every run re-deduplicated the whole pending backlog against itself (quadratic `SequenceMatcher`), so with ~5,000 pending notes each hourly run spent ~55 minutes there before doing anything else. Notes recovered from a prior run were already de-duplicated when first saved and are no longer re-checked.
- Deduplication no longer marks BOTH halves of a near-duplicate pair that arrive in the same batch (each used to find the other among the just-saved thoughts).
- Marking notes processed and persisting alias/dedup metadata are batched (`update_thoughts`): each daily thoughts file is rewritten once per distinct update instead of once per thought.

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
- `gyrus merge` refuses junk targets, carries source pages' Key Decisions/Timeline bullets into the target (replacing an empty-section placeholder rather than stacking under it), parks source pages as `.premerge.<stamp>.bak.md` snapshots instead of deleting them, rewrites `merged_into_page`, and proposes junk-identity consolidations in its suggestion flow.
- `cross-cutting.md` is deduplicated at render time — it is regenerated wholesale each run, so near-duplicate cross-reference insights could not be fixed by editing the file.
- Snapshot artifacts (`*.bak.md`, `*.premerge.*`, `*.failed-merge.*`, managed-block backups) are gitignored: the synced knowledge base had accumulated 83 of them.
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
