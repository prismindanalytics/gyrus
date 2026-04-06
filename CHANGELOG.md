# Changelog

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
