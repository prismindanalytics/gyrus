# Security Policy

## How Gyrus handles your data

- **Local by default.** Your knowledge base is plain markdown files on your machine (`~/.gyrus/`). Nothing leaves your machine unless you explicitly opt in to sync. The opt-in channels are: a private GitHub repo you own (recommended, set up via `gyrus init` — auto pull/push on every run), or the optional Notion adapter. Your `~/.gyrus/.env` secrets and per-machine state (`config.json`, `.ingest-state.json`) are gitignored and never synced.
- **LLM API calls.** Session text is sent to your configured LLM provider (Anthropic, OpenAI, or Google) for extraction and merging. This is the same data flow as using those AI tools directly.
- **API keys.** Stored in `~/.gyrus/.env` with 600 permissions (owner-only read/write). Never committed to git, never logged.

## Reporting a vulnerability

If you discover a security issue, please email **security@prismindanalytics.com** rather than opening a public issue.

We will acknowledge receipt within 48 hours and aim to release a fix within 7 days for critical issues.

## Supported versions

Gyrus uses a date-based version scheme (`2026.MM.DD.N`). The latest date-based release is the supported version — update with `gyrus update` to stay current.

| Version | Supported |
|---------|-----------|
| Latest date-based release (`2026.MM.DD.N`) | Yes |
| Older date-based releases | Update with `gyrus update` |
