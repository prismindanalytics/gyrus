# Contributing to Gyrus

Thanks for helping make context portable across AI tools. Bug fixes, new session
adapters, documentation improvements, and prompt-quality work are welcome.

## Development setup

Gyrus supports Python 3.10+ and has no runtime dependencies. With
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/prismindanalytics/gyrus.git
cd gyrus
uv run --python 3.12 python -m unittest test_gyrus -v
uv build
```

You can also run the tests with `python3 -m unittest test_gyrus -v` when a
compatible Python is already installed.

## Making a change

- Keep the core runtime standard-library-only unless a dependency has been
  discussed first.
- Preserve macOS, Linux, and Windows behavior. Use `pathlib` or existing path
  helpers instead of platform-specific string handling.
- Add a focused regression test for behavior changes. Session-adapter tests
  should use small, synthetic fixtures that match the real on-disk schema.
- Never commit real transcripts, knowledge-base pages, API keys, email
  addresses, or other personal data. Sanitize fixtures and command output.
- Treat ingestion state carefully: a failed read or model call must remain
  retryable rather than being recorded as successfully processed.

Before opening a pull request, run the full unit suite and build the wheel. In
the PR description, explain the user-visible behavior, the platforms affected,
and how you verified it.

For vulnerabilities or accidental secret exposure, follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.
