# Security Policy

## How Gyrus handles your data

- **Local knowledge base.** Gyrus stores generated thoughts and pages as files under `~/.gyrus/` (normally linked to `~/gyrus-local/`). It does not operate a hosted account or telemetry service.
- **Model data flow.** With a local model, session and knowledge-base content stays on your machine. With Anthropic, OpenAI, or Google models, relevant session text and existing page content are sent to the provider you configure for extraction and merging. Review that provider's data policy before use.
- **Optional sync and storage.** GitHub sync uploads the knowledge-base files and non-secret configuration to a private repository you own. The Notion adapter sends stored content to Notion. Neither integration is required.
- **Secrets.** Put API keys and mail credentials in `~/.gyrus/.env`, not `config.json`. The installers restrict this file to the current user and generated Git repositories ignore it. Do not commit or share `.env`.
- **Redaction and sync guards.** Common API-token, private-key, authorization-header, and URL-credential patterns are redacted before model calls by default (`redact_sensitive_data: true`). Git sync stages only documented knowledge-base paths and refuses public GitHub repositories unless `allow_public_sync` is explicitly enabled. Redaction is a defense-in-depth measure, not a guarantee that sensitive chat content is safe to upload.
- **Session sensitivity.** AI transcripts and generated pages can contain source code, customer information, or credentials copied into a chat. Review the supported session locations and exclude tools you do not want Gyrus to process.

## Reporting a vulnerability

If you discover a security issue, please email **security@prismindanalytics.com** rather than opening a public issue.

We will acknowledge receipt within 48 hours and aim to release a fix within 7 days for critical issues.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | Yes       |
| 0.2.x and earlier | No |
