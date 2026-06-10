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

## Export to connected services

When the user says "push to [service]", "export to [service]", or "sync to [service]":

1. Read the project page(s): `cat ~/.gyrus/projects/*.md`
2. If you have MCP tools for the target service (Notion, Linear, Slack, GitHub, Google Docs, Confluence, Jira), use them — e.g. one page/doc per project, decisions as issues, digests as messages
3. If no matching MCP tool is configured, say so and suggest adding the server to `~/.codex/config.toml` (or doing the export from a tool that has it connected)

For "export everything", iterate over all files in `~/.gyrus/projects/` and report what was exported. Confirm the target and scope before pushing (one project vs all).

## What NOT to do

- Don't modify the files — Gyrus manages them automatically
- Don't treat code-level details as strategic knowledge
