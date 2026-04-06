# Security Policy

## How Gyrus handles your data

- **Local by default.** Your knowledge base is plain markdown files on your machine (`~/.gyrus/`). Nothing is sent to any cloud service unless you explicitly opt in to sync (iCloud, Dropbox, Notion, etc.).
- **LLM API calls.** Session text is sent to your configured LLM provider (Anthropic, OpenAI, or Google) for extraction and merging. This is the same data flow as using those AI tools directly.
- **API keys.** Stored in `~/.gyrus/.env` with 600 permissions (owner-only read/write). Never committed to git, never logged.

## Reporting a vulnerability

If you discover a security issue, please email **security@prismindanalytics.com** rather than opening a public issue.

We will acknowledge receipt within 48 hours and aim to release a fix within 7 days for critical issues.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |
