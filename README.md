# Gyrus

> *A **gyrus** is a ridge on the cerebral cortex — the folded surface that gives the brain its computational density. Each fold packs more thinking into less space. That's what this tool does for your AI workflows.*

**Your AI tools don't talk to each other. Gyrus fixes that.**

You use Claude Code, Cowork, Codex, Antigravity — maybe all in the same day. Each one starts from zero. None of them know what you decided in the others. Your strategic thinking is scattered across dozens of sessions on multiple machines.

Gyrus reads all your AI tool sessions, extracts the important stuff, and builds a knowledge base that gets smarter every hour. Every tool, every machine, same brain.

```bash
curl -fsSL https://gyrus.sh/install | bash
```

One command. One API key. That's it.

---

## See It Work

After install, Gyrus immediately scans your existing sessions and shows you what it found:

```
Building your knowledge base...

  [1/83] cowork | 2025-03-20 | local_session_a3f...
    Extracted 7 thoughts
  [2/83] claude-code | 2025-03-20 | 8f2d1a...
    Extracted 3 thoughts

  ── Week 1: 2025-03-17 ──
    Merging 12 thoughts into 'beacon'...
      ✓ Updated 'beacon' v1
    Merging 8 thoughts into 'atlas'...
      ✓ Updated 'atlas' v1

  ── Week 2: 2025-03-24 ──
    Merging 6 thoughts into 'beacon'...
      ✓ Updated 'beacon' v2 (deeper, refined)
    ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Gyrus found and organized 26 projects!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Open any project page and see structured knowledge — not a chat log dump, but a real wiki:

```markdown
# Pulse

## Status
active | post-MVP | Priority: P1 | Division: Analytics
Last activity: 2026-04-03 | Machine: studio.local

## Overview
A real-time analytics dashboard for early-stage startups. Sub-second
query performance on a columnar database. Positions as a growth
intelligence platform with embeddable widgets and churn prediction.

## Architecture & Technical Stack
React frontend + columnar DB backend. Streaming ingestion for
real-time event processing. Custom materialized views for sub-second
aggregations...

## Key Decisions
- [2026-03-20] Killed two side projects to focus on Pulse (source: cowork)
- [2026-03-24] Churn predictor confirmed as feature, not standalone product (source: claude-code)
- [2026-03-28] Switched to edge deployment for lower latency (source: antigravity)
- [2026-04-01] Dynamic sitemap for SEO pages (source: codex)

## Timeline & History
- [2026-03-20] Initial concept from Cowork brainstorm session
- [2026-03-24] Architecture decision: columnar DB with streaming ingestion
- [2026-03-28] UI polish pass, mobile responsiveness
- [2026-04-01] Website redesigned, public launch prep
```

This is what Gyrus builds automatically from your scattered AI sessions. Pages are LLM-maintained drafts — review and edit them like any other doc.

---

## How It Works

```
Your AI tools                          Your knowledge base
┌─────────────────┐                    ┌─────────────────────┐
│ Claude Code      │──┐                │ ~/.gyrus/projects/  │
│ Claude Cowork    │──┤  Gyrus         │   beacon.md         │
│ OpenAI Codex     │──┤────────────▶   │   atlas.md     │
│ Google Antigrav. │──┘  Extract       │   wanderly.md      │
│ (any machine)    │     → Merge       │ ~/.gyrus/me.md      │
│                  │                   │ ~/.gyrus/status.md  │
└─────────────────┘                    └─────────────────────┘
```

1. **Scans** sessions from 4 AI coding tools: Claude Code, Claude Cowork, OpenAI Codex, and Google Antigravity — with more added on request
2. **Extracts** strategic decisions, insights, status changes (GPT-4.1 Mini by default — run `gyrus compare` to benchmark on your data)
3. **Resolves** project names ("Pulse App" = "pulse" = "Pulse") via fuzzy matching
4. **Deduplicates** across sessions and machines
5. **Merges** new knowledge into existing wiki pages (stronger model — Sonnet by default)
6. **Refines** — each merge pass makes pages deeper, not just longer (knowledge compounds)

No database. No cloud account. No signup. Just markdown files on your machine.

---

## Directory Structure

After install, Gyrus creates:

```
~/.gyrus/
  config.json          # Your settings (API keys, models, sync frequency)
  .env                 # API keys (auto-loaded by ingest.py)
  ingest.py            # The ingestion script (runs via cron)
  storage.py           # Storage adapter
  thoughts/            # Raw extracted thoughts (JSONL, one file per day)
    2025-04-01.jsonl
    2025-04-02.jsonl
  projects/            # Knowledge pages (one markdown file per project)
    beacon.md
    atlas.md
    wanderly.md
  me.md                # Personal patterns, preferences, working style
  ideas.md             # Standalone ideas and brainstorms
  status.md            # Quick summary of all projects
  cross-cutting.md     # Cross-project insights and connections
  aliases.json         # Maps project name variants to canonical slugs
  .ingest-state.json   # Tracks which sessions have been processed
  ingest.log           # Log output from scheduled runs
```

**projects/** — One wiki page per project, iteratively refined with each new session. These are the core output — structured knowledge that compounds over time.

**thoughts/** — Raw extracted thoughts before merging. JSONL format, one file per day. Useful for debugging or reviewing what was extracted.

**me.md** — Personal memory: your working patterns, tool preferences, recurring strategies. Things that aren't about a specific project but about how you work.

**ideas.md** — Standalone ideas, brainstorms, and opportunities that don't belong to an existing project yet. Ideas that graduate into real projects get their own page.

---

## Install

### Requirements
- At least one LLM API key: [Anthropic](https://console.anthropic.com/settings/keys), [OpenAI](https://platform.openai.com/api-keys), or [Google AI](https://aistudio.google.com/apikey)
- That's it. The installer handles Python automatically via [uv](https://docs.astral.sh/uv/) — no system Python needed, no version conflicts, nothing to configure.

### macOS / Linux

```bash
curl -fsSL https://gyrus.sh/install | bash
```

Or clone and run:

```bash
git clone https://github.com/prismindanalytics/gyrus.git
cd gyrus && ./install.sh
```

### Windows

```powershell
git clone https://github.com/prismindanalytics/gyrus.git
cd gyrus; .\install.ps1
```

### What the installer does

1. Asks for API keys (Anthropic, OpenAI, Google — Enter to skip any; at least one required)
2. Lets you choose a storage location (default `~/.gyrus/`, or a cloud-synced folder)
3. Lets you choose sync frequency (hourly by default, or 30 min / 4h / 12h / daily)
4. Installs skills for your AI tools (`/gyrus` command for Claude Code)
5. **Immediately scans all existing sessions and builds your knowledge base**

### Choose Your Models

Gyrus works with any supported LLM. Configure anytime in `~/.gyrus/config.json`:

```json
{
  "extract_model": "gpt-4.1-mini",
  "merge_model": "sonnet"
}
```

**Available models (16):**
- **Anthropic:** `haiku` (Claude Haiku 4.5), `sonnet` (Claude Sonnet 4.6), `opus` (Claude Opus 4.6)
- **OpenAI:** `gpt-4.1-mini`, `gpt-4.1-nano`, `gpt-4.1`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.4`, `gpt-5.4-pro`, `o3`, `o4-mini`
- **Google:** `gemini-flash` (Gemini 3 Flash), `gemini-lite` (Gemini 3.1 Flash Lite), `gemini-pro` (Gemini 3.1 Pro)

Or pass any raw model ID (e.g. `claude-sonnet-4-20250514`).

### Full config.json Reference

```json
{
  "extract_model": "gpt-4.1-mini",
  "merge_model": "sonnet",
  "excluded_tools": []
}
```

API keys go in `~/.gyrus/.env` (not config.json):
```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AI...
```

| Key | Default | Description |
|-----|---------|-------------|
| `extract_model` | `gpt-4.1-mini` | Model for extracting thoughts from sessions |
| `merge_model` | `sonnet` | Model for merging thoughts into knowledge pages (stronger) |
| `excluded_tools` | `[]` | Tools to skip during ingestion (e.g. `["antigravity"]`) |
| `repo_groups` | `{}` | Map repo folder names to canonical project names (e.g. `{"backend": "myapp", "frontend": "myapp"}`) |
| `parallel_extractions` | `4` | Number of parallel extraction workers |
| `digest.enabled` | `false` | Enable daily digest after each ingestion run |
| `digest.email` | — | Email address to send digest to |
| `digest.provider` | `"resend"` | Email provider: `"resend"` or `"smtp"` |
| `digest.resend_api_key` | — | Resend API key (if provider is resend) |

**Recommendation:** Run `gyrus compare` to benchmark on your own data. It tests all available models, generates sample wiki pages, and an AI judge grades quality. You choose both extraction and merge models.

### CLI Reference

| Command | Description |
|---------|-------------|
| `gyrus compare` | Benchmark models on your sessions, pick extraction + merge models |
| `gyrus status` | Interactively review and set project statuses |
| `gyrus digest` | Generate a digest of recent activity |
| `gyrus eval` | Run prompt quality eval against golden fixtures |
| `gyrus curate` | Create golden test fixtures from real sessions |
| `gyrus update` | Update Gyrus to the latest version from GitHub |
| `--dry-run` | Run extraction without saving (for testing) |
| `--backfill` | Rebuild knowledge pages from existing thoughts |
| `--base-dir PATH` | Use a custom base directory (default: `~/.gyrus`) |

---

## What Gets Extracted

Gyrus is ruthlessly selective. From a 2-hour coding session, it might extract 2-3 thoughts:

| Extracted | Not Extracted |
|-----------|---------------|
| "Decided to kill SideProject — market too crowded" | `git commit -m "fix typo"` |
| "YC demo deadline April 1 — need metrics ready" | Tool calls and file operations |
| "Position as real-time analytics layer for YC batch" | "Yes", "OK", "let me check" |
| "Pricing: Free (3 reviews), Pro $4.99/mo" | CSS changes and config tweaks |

Non-project knowledge (your working patterns, tool preferences, cross-cutting insights) goes into `~/.gyrus/me.md` — a personal memory page that also compounds over time.

Standalone ideas and brainstorms go into `~/.gyrus/ideas.md` — a living idea backlog. When an idea graduates into a real project, it gets its own page.

### Three-way classification

Every extracted thought is classified as one of:

- **project** — About an existing or named project → merged into `projects/<slug>.md`
- **idea** — A new concept or opportunity not tied to an existing project → merged into `ideas.md`
- **meta** — Working patterns, tool preferences, recurring strategies → merged into `me.md`

---

## Where Gyrus Reads Sessions

Gyrus automatically finds session files for each tool. You don't need to configure paths — it checks the default locations:

| Tool | macOS | Linux | Windows |
|------|-------|-------|---------|
| Claude Code | `~/.claude/projects/` | `~/.claude/projects/` | `%APPDATA%\Claude\projects\` |
| Claude Cowork | `~/Library/Application Support/Claude/local-agent-mode-sessions/` | `~/.config/Claude/local-agent-mode-sessions/` | `%APPDATA%\Claude\local-agent-mode-sessions\` |
| Codex | `~/.codex/sessions/` | `~/.codex/sessions/` | `%APPDATA%\codex\sessions\` |
| Antigravity | `~/.gemini/antigravity/brain/` | `~/.gemini/antigravity/brain/` | `%USERPROFILE%\.gemini\antigravity\brain\` |

If a tool isn't installed, Gyrus skips it silently — no errors, no cost.

---

## How Knowledge Pages Work

Each project gets a wiki-style markdown page that is **iteratively refined** — not appended to.

### The merge process

1. New thoughts arrive for a project (e.g. "beacon")
2. Gyrus reads the existing `projects/beacon.md` page
3. Sends both to the merge model: "Here's the current page and new thoughts. Update the page."
4. The model integrates new information — updating sections, adding decisions, resolving contradictions
5. The updated page is saved with an incremented version number

### What makes this different from appending

- **Week 1:** "Pulse is a real-time analytics dashboard"
- **Week 2:** The Overview section now includes target market and competitive positioning
- **Week 3:** Architecture section added, Key Decisions updated with kill/commit choices
- **Week 4:** Timeline refined, contradictions resolved, stale info removed

The page gets **deeper and more accurate** over time, not just longer. Old information that's been superseded gets updated in place.

### Manual edits are preserved

You can edit any knowledge page by hand. Gyrus reads the current page before each merge, so your manual edits are treated as part of the existing knowledge and built upon.

### Version tracking

Each page has a hidden version comment at the bottom (`<!-- version: N -->`). You can track how many merge passes have refined it.

---

## Cross-Machine Sync

Gyrus stores everything as plain markdown files. Sync them however you want:

- **iCloud / Dropbox / Google Drive** — point `~/.gyrus/` to a synced folder
- **Git** — `cd ~/.gyrus && git init && git remote add origin ...`
- **Obsidian** — set your vault path to `~/.gyrus/`
- **Notion** — optional adapter (`--storage=notion`) for browsable UI

Run `./install.sh` on each machine. Same knowledge base, everywhere.

---

## Stays In Sync Automatically

Once installed, Gyrus runs in the background. No new sessions = no API calls = **zero cost**. It only spends when there's actual new work to process.

- **Scheduled sync** — cron job (macOS/Linux) or Scheduled Task (Windows) at whatever frequency you choose during install: every 30 min, hourly, every 4 hours, or daily. If nothing changed since the last run, it exits immediately — no LLM calls, no cost.
- **Tool skills** — installs a `/gyrus` command into Claude Code and instructions for Codex, so your AI tools can query the knowledge base mid-session. Context flows both ways.
- **Self-update** — `python3 ~/.gyrus/ingest.py --update` pulls the latest version from GitHub.

### Verifying your sync is running

```bash
# Check the cron job exists (macOS/Linux)
crontab -l | grep gyrus

# Check recent runs
tail -20 ~/.gyrus/ingest.log

# Run manually to test
cd ~/.gyrus && uv run --python 3.12 ingest.py --anthropic-key $ANTHROPIC_API_KEY
```

On Windows:
```powershell
# Check the scheduled task
Get-ScheduledTask -TaskName "GyrusIngestion"

# Check recent runs
Get-Content "$env:USERPROFILE\.gyrus\ingest.log" -Tail 20
```

### What triggers a sync

Each run checks session file modification times against `.ingest-state.json`. Only sessions modified since the last run are processed. If nothing changed, the script exits in <1 second with zero API calls.

---

## Cost

| Component | Cost |
|-----------|------|
| Thought extraction (Haiku) | ~$0.01 per session |
| Knowledge merging (Sonnet) | ~$0.05 per project page update |
| Typical monthly (active user) | **~$5-15/month** |
| Cloud accounts needed | **Zero** |

---

## Knowledge Compounds

Raw data flows through LLM extraction into a structured, compounding knowledge base. Each merge pass doesn't just append — it deepens understanding, resolves contradictions, and surfaces connections you didn't see.

> Week 1: "Pulse is a real-time analytics dashboard"
> Week 2: "Pulse targets early-stage startups, positioning against heavyweight BI tools"
> Week 3: "Pulse's moat is sub-second query latency + native payment integration"
> Week 4: "Pulse needs demo by end of month. Ship churn predictor or cut scope."

Pages are LLM-maintained drafts — review and edit them like any other doc.

---

## Daily Digest

Get a daily email summarizing what changed across your projects:

```json
{
  "digest": {
    "enabled": true,
    "email": "you@example.com",
    "provider": "resend",
    "resend_api_key": "re_..."
  }
}
```

The digest runs automatically after each ingestion. Generate one on demand with `gyrus digest`. Supports Resend (recommended) and SMTP (Gmail, etc.).

---

## Quality Framework

Gyrus includes an eval framework for iteratively improving extraction and merge quality.

```bash
# Create golden test fixtures from your sessions
gyrus curate

# Run eval against golden fixtures
gyrus eval

# Save prompt version for comparison
gyrus eval --eval-save-prompt v1

# Compare two prompt versions
gyrus eval --eval-compare v1 v2

# Regression gate (exit 1 if quality dropped)
gyrus eval --eval-regression
```

The eval scores extraction on 5 metrics (recall, precision, noise rejection, project attribution, count calibration) and merge on 5 metrics (hallucination detection, content completeness, structural integrity, staleness detection, append-only compliance).

Current extraction quality: **0.90** composite across 7 golden fixtures.

---

## FAQ

**How is this different from Mem0 / OpenMemory?**
Those store extracted facts in vector databases. Gyrus maintains human-readable wiki pages in plain markdown. You can open, read, and edit them. No server to run.

**How is this different from AGENTS.md / .cursorrules?**
Those are static instruction files you write manually. Gyrus automatically extracts and maintains knowledge from your actual work sessions.

**Does it work with [tool X]?**
Currently supports Claude Code, Claude Cowork, OpenAI Codex, and Google Antigravity. More tools added on request — if a tool writes session files to disk, adding support is ~30 lines of Python.

**Will it read my private conversations?**
Gyrus runs locally on your machine by default. Session data is sent to your chosen LLM API for extraction/merging (same as using any AI tool), but your knowledge base stays as local markdown files. Nothing is stored in any cloud unless you opt in — you can choose to sync via iCloud, Dropbox, Notion, or Git for cross-machine access.

**Can I edit the wiki pages manually?**
Yes. Gyrus reads the existing page before each merge, so your manual edits are preserved and built upon.

---

## Troubleshooting

**"No new sessions found"**
- Check that your AI tool is in the supported list and has sessions on disk
- Run `ls` on the session path for your tool (see [Where Gyrus Reads Sessions](#where-gyrus-reads-sessions))
- If you excluded a tool during install, check `excluded_tools` in `config.json`

**"API key not found"**
- Gyrus auto-loads `~/.gyrus/.env`. Make sure your key is there:
  ```bash
  cat ~/.gyrus/.env
  # Should show: ANTHROPIC_API_KEY=sk-ant-...
  ```
- Or pass it directly: `ingest.py --anthropic-key sk-ant-...`

**Cron job isn't running**
- Check it exists: `crontab -l | grep gyrus`
- Check logs: `tail -50 ~/.gyrus/ingest.log`
- On macOS, cron needs Full Disk Access in System Settings → Privacy & Security

**Project names are duplicated (e.g. "Pulse" and "pulse-app")**
- Edit `~/.gyrus/aliases.json` to map variants to a canonical slug:
  ```json
  [
    {"alias": "beacon-app", "canonical_slug": "beacon"},
    {"alias": "Pulse App", "canonical_slug": "pulse"}
  ]
  ```
- Gyrus also auto-resolves via fuzzy matching (>80% similarity)

**Knowledge page has stale info**
- Edit the page directly — Gyrus will preserve your changes on the next merge
- Or delete the page and let the next run rebuild it from thoughts

---

## Contributing

### Adding support for a new AI tool

All tool support lives in `ingest.py`. You need two functions:

**1. Session finder** — tells Gyrus where to look:

```python
def find_newtool_sessions(state):
    """Find new/modified session files for NewTool."""
    sessions = []
    for session_file in glob.glob(os.path.join(NEWTOOL_PATH, "*.json")):
        mtime = os.path.getmtime(session_file)
        session_id = Path(session_file).stem
        last_processed = state["processed_sessions"].get(
            f"newtool:{session_id}", 0
        )
        if mtime > last_processed:
            sessions.append({
                "type": "newtool",
                "path": session_file,
                "session_id": session_id,
                "mtime": mtime,
                "state_key": f"newtool:{session_id}",
            })
    return sessions
```

**2. Conversation extractor** — reads the session file into plain text:

```python
def extract_newtool_conversation(path, max_chars=30000):
    """Extract conversation text from a NewTool session file."""
    lines = []
    with open(path) as f:
        data = json.load(f)
    for msg in data.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if content and role in ("user", "assistant"):
            lines.append(f"{role}: {content[:3000]}")
    return "\n".join(lines)[:max_chars]
```

**3. Wire it up** — add to the session collection and extraction dispatch in `main()`:

```python
# In the session collection list:
find_newtool_sessions(state) +

# In the extraction dispatch dict:
"newtool": lambda s: extract_newtool_conversation(s["path"]),
```

**4. Test** — run with `--dry-run` to verify sessions are found without making API calls.

PRs welcome.

---

## Update

```bash
gyrus update
```

Downloads the latest scripts from GitHub. Your knowledge base, config, and API keys are preserved.

## Uninstall

```bash
curl -fsSL https://gyrus.sh/uninstall | bash
```

Removes the cron job, Claude Code skill, and `~/.gyrus/` directory. Warns you to back up your knowledge base first.

On Windows:
```powershell
Unregister-ScheduledTask -TaskName "GyrusIngestion" -Confirm:$false
Remove-Item "$env:USERPROFILE\.claude\commands\gyrus.md" -ErrorAction SilentlyContinue
Remove-Item "$env:USERPROFILE\.gyrus" -Recurse -Force
# Optional: remove uv
Remove-Item "$env:USERPROFILE\.local\bin\uv.exe" -ErrorAction SilentlyContinue
```

---

## License

MIT

---

Built by [Prismind Analytics](https://prismindanalytics.com) — because your AI tools should share a brain.
