---
name: gyrus
description: Query the Gyrus knowledge base. Trigger with "check gyrus", "what did I decide about", "has this been explored", "what do we know about", or when you need context from previous AI sessions.
---

# Gyrus — Knowledge Base

Gyrus is a knowledge base built automatically from all your AI tool sessions (Claude Code, Claude Cowork, Codex, Cursor, Copilot, and more). It lives as local markdown files in `~/.gyrus/`.

## How to read

The knowledge base is plain markdown files. Read them directly:

- `~/.gyrus/projects/` — one wiki page per project
- `~/.gyrus/status.md` — overview of all projects
- `~/.gyrus/me.md` — personal memory (working patterns, preferences)
- `~/.gyrus/cross-cutting.md` — insights that span multiple projects

Each project page contains: status, overview, key decisions, open questions, connections to other projects, and recent activity.

## When to use

- Before starting strategic work: read the project page for context
- When the user asks "what did I decide about X?" or "has this been explored?"
- When you notice cross-project connections worth surfacing
- When the user says "gyrus" or "check gyrus" or "what do we know about"

## Guidelines

- Present results as concise summaries, not raw file contents
- Highlight key decisions, open questions, and recent activity
- Note when information might be stale (check dates)
- Don't modify the files — Gyrus manages them automatically
