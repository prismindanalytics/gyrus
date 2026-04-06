# Gyrus Integration for Codex

Add these instructions to your Codex `~/.codex/instructions.md` or project-level AGENTS.md.

## Instructions to add

At the start of any session, check Gyrus for relevant context:

```bash
# List all projects
ls ~/.gyrus/projects/

# Read a specific project (replace PROJECT with actual name)
cat ~/.gyrus/projects/PROJECT.md

# Search across all knowledge
grep -ri "SEARCH_TERM" ~/.gyrus/projects/

# Check overall status
cat ~/.gyrus/status.md
```

## What the knowledge base contains

Each project page is a structured wiki document with:
- Status, priority, and stage
- Overview of what the project is
- Key decisions with dates
- Open questions
- Connections to other projects
- Recent activity timeline

## When to check Gyrus

- Before starting strategic work: read the project page for context
- When the user asks "what did I decide about X?" or "has this been explored?"
- When you notice cross-project connections

## What NOT to do

- Don't modify the files — Gyrus manages them automatically
- Don't treat code-level details as strategic knowledge
