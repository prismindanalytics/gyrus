# Gyrus — Query your knowledge base

You have access to Gyrus, a knowledge base built from all your AI tool sessions (Claude Code, Cowork, Codex, Antigravity). It lives as markdown files in `~/.gyrus/`.

## Query the knowledge base

```bash
ls ~/.gyrus/projects/              # browse all projects
cat ~/.gyrus/projects/PROJECT.md   # read a project page
grep -ri "SEARCH_TERM" ~/.gyrus/   # search across everything
cat ~/.gyrus/status.md             # project statuses
cat ~/.gyrus/me.md                 # personal patterns
cat ~/.gyrus/latest-digest.md      # latest activity digest
```

## Run Gyrus

```bash
gyrus                 # run ingestion
gyrus compare         # benchmark and choose models
gyrus status          # review project statuses
gyrus digest          # generate activity digest
gyrus update          # update to latest version
```

## Export to connected services

When the user says "push to [service]", "export to [service]", or "sync to [service]", check which MCP servers are available and use them to export Gyrus project pages.

### How to export

1. Read the project page(s): `cat ~/.gyrus/projects/*.md`
2. Detect which relevant MCP tools are available to you
3. Use the appropriate MCP tool to create/update content in the target service

### Supported destinations (via MCP)

| Service | MCP tool | What to create |
|---------|----------|----------------|
| **Notion** | `notion_create_page`, `notion_update_block` | One Notion page per project |
| **Linear** | `linear_create_issue`, `linear_create_project` | Project status as Linear project, decisions as issues |
| **Slack** | `slack_post_message` | Daily digest or project summary to a channel |
| **GitHub** | `github_create_or_update_file` | Wiki pages in a repo, or update README |
| **Google Docs** | `google_docs_create`, `google_docs_update` | One doc per project |
| **Confluence** | `confluence_create_page` | One page per project in a space |
| **Jira** | `jira_create_issue` | Open questions as Jira tickets |

If the user asks to export but the target MCP isn't connected, suggest they connect it first (Settings → MCP Servers in Claude Code).

### Export all projects

If user says "push everything to Notion" or "export all to Slack":
1. List all project pages: `ls ~/.gyrus/projects/`
2. For each `.md` file, read it and push to the target
3. Report what was exported

## When to use

- User asks "what did we decide about X?" → search the knowledge base
- User asks "has this been explored before?" → search projects
- At the start of a session → read the relevant project page for context
- User says "gyrus", "check gyrus", "what do we know about" → query
- User says "push to [service]" or "export to [service]" → export via MCP
- User says "send digest to Slack" → read digest, post via Slack MCP

## Guidelines

- Present results as concise summaries, not raw file dumps
- Highlight key decisions, open questions, and recent activity
- Note when information might be stale (check dates in the pages)
- For exports: confirm the target and scope before pushing (one project vs all)
- The knowledge base updates automatically via cron
