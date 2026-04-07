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

### Push to Notion (requires Notion MCP connection)

If the user has Notion MCP connected, you can push Gyrus project pages to Notion:

1. Read the project page: `cat ~/.gyrus/projects/PROJECT_NAME.md`
2. Use the Notion MCP `notion_create_page` tool to create a page in the user's workspace
3. Or use `notion_update_block` to update an existing page

The user can say "push my projects to Notion" or "sync gyrus to Notion".

### Run Gyrus commands
```bash
gyrus                 # run ingestion
gyrus compare         # benchmark and choose models
gyrus status          # review project statuses
gyrus digest          # generate activity digest
gyrus update          # update to latest version
```

## When to use

- User asks "what did we decide about X?" — search the knowledge base
- User asks "has this been explored before?" — search projects
- At the start of a project session — read that project's page for context
- User says "gyrus", "check gyrus", or "what do we know about" — query the knowledge base
- User says "push to Notion" or "sync to Notion" — push pages via Notion MCP

## Guidelines

- Present results as a concise summary, not raw file dumps
- Highlight key decisions, open questions, and recent activity
- Note when information might be stale (check dates in the pages)
- The knowledge base updates automatically — you're always reading the latest version
