# Gyrus — Query your knowledge base

You have access to Gyrus, a knowledge base built from all your AI tool sessions (Claude Code, Claude Cowork, Codex, Cursor, Copilot, and more). It lives as markdown files in `~/.gyrus/`.

## How to use

Read the local markdown files directly.

### Browse all projects
```bash
ls ~/.gyrus/projects/
```

### Read a project page
```bash
cat ~/.gyrus/projects/PROJECT_NAME.md
```

### Search across all knowledge
```bash
grep -ri "SEARCH_TERM" ~/.gyrus/projects/
```

### Check overall status
```bash
cat ~/.gyrus/status.md
```

### Read personal memory
```bash
cat ~/.gyrus/me.md
```

### Read cross-project insights
```bash
cat ~/.gyrus/cross-cutting.md
```

## When to use

- User asks "what did we decide about X?" — search the knowledge base
- User asks "has this been explored before?" — search projects
- At the start of a project session — read that project's page for context
- User says "gyrus", "check gyrus", or "what do we know about" — query the knowledge base

## Guidelines

- Present results as a concise summary, not raw file dumps
- Highlight key decisions, open questions, and recent activity
- Note when information might be stale (check dates in the pages)
- The knowledge base updates automatically — you're always reading the latest version
