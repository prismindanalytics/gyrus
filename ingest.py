#!/usr/bin/env python3
"""
Gyrus Ingestion Script
Reads AI tool sessions (Claude Code, Cowork, Codex, Antigravity, Cursor
— plus Copilot, OpenCode, Cline, Continue.dev, Aider, Gemini CLI if present),
extracts key thoughts via Claude API, and builds an iterative knowledge base.

Zero signup required — only needs an Anthropic API key.
Knowledge pages are local markdown files by default.
https://gyrus.sh
"""

__version__ = "2026.04.19.0"

import argparse
import atexit
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import platform
import socket


# Windows' default console encoding (cp1252) can't emit the emoji we use in
# status lines. Reconfigure stdio to UTF-8 with a `replace` fallback so a
# stray non-ASCII character can never raise UnicodeEncodeError mid-run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass  # older Python or non-reconfigurable stream — tolerated


# ─── Lockfile (prevents concurrent ingest runs) ───

def _lock_path():
    """Get lock file path — always local, never in synced folder."""
    import tempfile
    lock_dir = Path(tempfile.gettempdir()) / "gyrus"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / ".gyrus.lock"


def _acquire_lock(base_dir):
    """Acquire a lockfile to prevent concurrent ingestion runs (e.g. cron
    firing while an interactive run is still going). Stored in /tmp so it
    never travels with git sync.
    Returns True if acquired, False if another instance is running."""
    lock_path = _lock_path()
    if lock_path.exists():
        try:
            lock_data = json.loads(lock_path.read_text())
            lock_age = time.time() - lock_data.get("time", 0)
            lock_machine = lock_data.get("machine", "unknown")
            # Stale lock (older than 30 minutes) — steal it
            if lock_age > 1800:
                print(f"  Stale lock from {lock_machine} ({lock_age/60:.0f}m ago) — overriding")
            else:
                print(f"  Another Gyrus instance is running on {lock_machine} "
                      f"({lock_age/60:.0f}m ago). Skipping.")
                return False
        except (json.JSONDecodeError, IOError, OSError):
            pass  # Corrupt or inaccessible lock — override it

    try:
        lock_path.write_text(json.dumps({
            "machine": socket.gethostname(),
            "pid": os.getpid(),
            "time": time.time(),
        }))
        atexit.register(lambda: lock_path.unlink(missing_ok=True))
    except OSError:
        pass  # Can't write lock — proceed anyway
    return True


def _release_lock(base_dir):
    """Release the lockfile."""
    try:
        _lock_path().unlink(missing_ok=True)
    except OSError:
        pass

from storage import MarkdownStorage

_SYSTEM = platform.system()
_MACHINE = socket.gethostname()

# ─── Paths (cross-platform) ───

_HOME = Path.home()
_APPDATA = os.environ.get("APPDATA", "")

def _p(*parts):
    """Resolve a path from home directory, with platform-specific overrides."""
    return str(_HOME.joinpath(*parts))


# All supported tool paths
PATHS = {}

def _resolve_all_paths():
    global PATHS
    PATHS = {
        # Claude Code — same everywhere
        "claude-code": _p(".claude", "projects"),

        # Claude Desktop (Cowork / agent mode sessions)
        "cowork": (
            _p("Library", "Application Support", "Claude", "local-agent-mode-sessions")
            if _SYSTEM == "Darwin"
            else str(Path(_APPDATA) / "Claude" / "local-agent-mode-sessions")
            if _SYSTEM == "Windows"
            else _p(".config", "Claude", "local-agent-mode-sessions")
        ),

        # Codex (OpenAI)
        "codex": _p(".codex", "sessions"),

        # Antigravity / Gemini
        "antigravity": _p(".gemini", "antigravity", "brain"),

        # Cursor — SQLite in app storage
        "cursor": (
            _p("Library", "Application Support", "Cursor", "User", "workspaceStorage")
            if _SYSTEM == "Darwin"
            else str(Path(_APPDATA) / "Cursor" / "User" / "workspaceStorage")
            if _SYSTEM == "Windows"
            else _p(".config", "Cursor", "User", "workspaceStorage")
        ),

        # Windsurf (Codeium) — protobuf, harder to parse
        "windsurf": _p(".codeium", "windsurf", "cascade"),

        # GitHub Copilot (VS Code chat sessions)
        "copilot": (
            _p("Library", "Application Support", "Code", "User", "workspaceStorage")
            if _SYSTEM == "Darwin"
            else str(Path(_APPDATA) / "Code" / "User" / "workspaceStorage")
            if _SYSTEM == "Windows"
            else _p(".config", "Code", "User", "workspaceStorage")
        ),

        # Aider — per-project markdown history
        "aider": None,  # searched dynamically in project dirs

        # Continue.dev
        "continue": _p(".continue", "sessions"),

        # Cline (VS Code extension)
        "cline": (
            _p("Library", "Application Support", "Code", "User", "globalStorage",
               "saoudrizwan.claude-dev", "tasks")
            if _SYSTEM == "Darwin"
            else str(Path(_APPDATA) / "Code" / "User" / "globalStorage" /
                      "saoudrizwan.claude-dev" / "tasks")
            if _SYSTEM == "Windows"
            else _p(".config", "Code", "User", "globalStorage",
                     "saoudrizwan.claude-dev", "tasks")
        ),

        # OpenCode
        "opencode": _p(".local", "share", "opencode", "storage", "session"),

        # Kiro (AWS)
        "kiro": _p(".kiro"),
    }

_resolve_all_paths()

# Backward compat
CLAUDE_CODE_BASE = PATHS["claude-code"]
COWORK_BASE = PATHS["cowork"]
ANTIGRAVITY_BRAIN = PATHS["antigravity"]
CODEX_BASE = PATHS["codex"]

# ─── Prompts ───

EXTRACTION_PROMPT = """You are extracting important thoughts from an AI conversation session.

Read the following conversation and extract what matters beyond the current session — decisions, ideas, insights, and context that would be valuable to recall weeks or months from now. Output a JSON array of thought objects.

Each thought should be:
- A strategic decision or direction change
- A new idea, concept, or brainstorm worth remembering
- A status change (something was built, shipped, decided, killed, pivoted)
- A connection between projects, people, or domains
- An unresolved question worth tracking
- A commitment, deadline, or next step

Each thought object has these fields:
  "content": "The thought itself — be specific, include names/numbers/dates. Only state what is explicitly present in the conversation. Never infer or assume details not stated."
  "project": "project-name" or null
  "tags": ["decision", "idea", "insight", "status", "question", etc.]
  "kind": "project" | "idea" | "meta"

CRITICAL — How to set "project":
- The "project" field must be the PRODUCT name, not a feature, sub-task, or module name.
- If working on a feature within a larger product (e.g., adding a dashboard to "Acme App"), use the PRODUCT name ("Acme App"), NOT the feature name ("dashboard").
- If a WORKSPACE is specified below, use that as the project name unless the conversation is clearly about a DIFFERENT product.

How to classify "kind":
- "project": About building or developing a PRODUCT that already has a repo, codebase, or deployment. Active development work on an existing product.
- "idea": A new concept, brainstorm, or opportunity NOT yet started. "What if we built X", naming a potential product, exploring a market, pricing brainstorms. If the session is mostly brainstorming about something that doesn't exist yet, ALL thoughts from it should be "idea" with project set to null.
- "meta": About working patterns, tool preferences, daily schedules, productivity insights, or cross-cutting themes not tied to a specific project.

DO NOT extract:
- Technical implementation details (migration steps, schema designs, code changes, config tweaks)
- Tool calls, file operations, terminal commands
- Conversation filler ("yes", "ok", "let me check", "sounds good")
- Anything only useful within that coding session
- Casual remarks or vague intentions ("I should probably...", "maybe we could...")
- What the AI assistant plans to do next (that's the assistant's action, not a user decision)
- DO NOT invent or hallucinate project details not present in the conversation

If the session has NO extractable thoughts, return an empty array: []

Be selective. Aim for the MINIMUM number of thoughts that capture the session's strategic value. Each thought should be a distinct decision, insight, or status change. If two thoughts are about the same decision, combine them. A typical session yields 2-4 thoughts. Fewer is better than more.

EXAMPLE INPUT: "Let's switch to JWT. Use RS256 signing. 15-minute access tokens, 7-day refresh tokens in httpOnly cookies."
GOOD extraction: [{"content": "Auth switching to JWT with RS256, 15-min access / 7-day refresh tokens in httpOnly cookies", "project": "my-app", "tags": ["decision"], "kind": "project"}]
BAD extraction: Three separate thoughts for JWT, RS256, and token config. That's one decision, not three.

EXAMPLE INPUT: "Fix the CSS on the header. The logo is 2px off."
GOOD extraction: []
BAD extraction: [{"content": "Header CSS adjusted..."}] — this is a trivial implementation task with zero strategic value.

"""

MERGE_PROMPT = """You are maintaining a wiki page for a project. This page should help someone understand what this project is, where it stands, and what was decided — based only on evidence from the thoughts below.

CURRENT KNOWLEDGE PAGE:
{page_content}

NEW THOUGHTS TO MERGE:
{new_thoughts}

RULES:
1. INTEGRATE new thoughts into the existing page. Build on what's there, don't rewrite from scratch.
2. ONLY state what the thoughts explicitly say. Never infer, assume, or embellish details that aren't in the input.
3. If a thought contradicts existing content, note the contradiction with dates — don't silently overwrite.
4. Use dates from the thoughts' timestamps, not today's date.
5. If a section has no relevant information, leave it minimal rather than inventing content.
6. "Key Decisions" and "Timeline & History" are append-only — never remove entries.
7. Mark the status based on the most recent evidence. If there's no recent activity, mark as dormant.

Output the COMPLETE updated page in this markdown structure:

# ProjectName

## Status
status | stage | Priority: P1/P2/P3 | Division: division-name
Last activity: YYYY-MM-DD | Machine: machine-name

## Overview
What this project does, who it's for, and why it exists. Write based on evidence from the thoughts, not assumptions. 1-3 paragraphs.

## Architecture & Technical Stack
Languages, frameworks, infrastructure, key technical decisions. Only include details mentioned in the thoughts.

## Business Model & Market
Revenue model, pricing, target audience — only if discussed in the thoughts.

## Key Decisions
Chronological log of significant decisions. Append-only.
- [YYYY-MM-DD] Decision description (source: tool-name)

## Open Questions
Unresolved questions from the thoughts.
- Question text (raised: YYYY-MM-DD)

## Connections & Dependencies
How this project relates to other projects.
- [Entity]: Relationship description

## Timeline & History
Chronological record of significant events. Append-only.
- [YYYY-MM-DD] What happened (source: tool-name)

## Current Sprint / Next Steps
What's actively being worked on, based on the most recent thoughts.

After the page, on its own line, output:
CHANGE_SUMMARY: one sentence describing what changed
"""

KNOWLEDGE_PAGE_TEMPLATE = """# {name}

## Status
unknown | unknown | Priority: unknown | Division: unknown
Last activity: {date}

## Overview
(No information yet — will be filled as thoughts are merged.)

## Architecture & Technical Stack
(No technical details yet.)

## Business Model & Market
(No business model details yet.)

## Key Decisions
(None recorded)

## Open Questions
(None recorded)

## Connections & Dependencies
(None identified)

## Timeline & History
- [{date}] Knowledge page created (source: gyrus)

## Current Sprint / Next Steps
(Nothing planned yet.)
"""

ME_MERGE_PROMPT = """You are maintaining a personal knowledge page — a living document about the user behind all these projects. This captures patterns, preferences, strategies, and context that span across projects.

CURRENT PAGE:
{page_content}

NEW THOUGHTS TO MERGE:
{new_thoughts}

INSTRUCTIONS:
1. INTEGRATE new thoughts into the existing page. Build on what's there.
2. This is about the PERSON, not any single project. Capture meta-level patterns.
3. Update sections as understanding deepens — especially Working Style and Strategic Patterns.
4. If thoughts reveal cross-project strategies, recurring decision patterns, or personal preferences, capture them.
5. Tools & Machines should track which AI tools and machines are actively being used.

Output the COMPLETE updated page:

# Me

## Working Style
How this person works: tools, habits, decision-making patterns, work rhythm. Write in third person.

## Strategic Patterns
Recurring strategies and principles that show up across projects. Not one-off decisions but patterns.

## Recurring Decisions
Chronological log of meta-level decisions (not project-specific ones).
- [YYYY-MM-DD] Decision (source: tool-name)

## Tools & Machines
Which AI tools and machines are actively in use.

## Cross-Project Themes
Themes, markets, or technologies that span multiple projects.

After the page, on its own line, output:
CHANGE_SUMMARY: one sentence describing what changed
"""

ME_PAGE_TEMPLATE = """# Me

## Working Style
(No information yet.)

## Strategic Patterns
(No patterns identified yet.)

## Recurring Decisions
(None recorded)

## Tools & Machines
(No tools tracked yet.)

## Cross-Project Themes
(No themes identified yet.)
"""

IDEAS_MERGE_PROMPT = """You are maintaining an idea backlog — a living document that captures new concepts, brainstorms, opportunities, and "what if" thinking that hasn't yet become a project.

CURRENT PAGE:
{page_content}

NEW IDEAS TO MERGE:
{new_thoughts}

INSTRUCTIONS:
1. INTEGRATE new ideas into the existing page. Build on what's there.
2. Each idea should be a clear, self-contained entry with enough context to understand it later.
3. If a new idea relates to or builds on an existing one, merge them — don't duplicate.
4. If an idea has clearly evolved into an active project (you see it in the thoughts with a project name), mark it as "→ Became [project-name]" and move it to the Graduated section.
5. Group related ideas under themes when natural clusters emerge.
6. Keep the energy of the original brainstorm — don't over-formalize.

Output the COMPLETE updated page:

# Ideas

## Active Ideas
Ideas worth exploring further. Each entry: date, the idea, and any context.

## Themes
Natural clusters of related ideas that keep coming up.

## Graduated
Ideas that became real projects. Brief note + link to the project.

## Parked
Ideas that were considered but shelved, with a note on why.

After the page, on its own line, output:
CHANGE_SUMMARY: one sentence describing what changed
"""

IDEAS_PAGE_TEMPLATE = """# Ideas

## Active Ideas
(No ideas captured yet.)

## Themes
(No themes identified yet.)

## Graduated
(None yet.)

## Parked
(None yet.)
"""

CROSS_REFERENCE_PROMPT = """You are analyzing project knowledge pages to find cross-project connections, contradictions, and patterns.

PROJECT SUMMARIES:
{summaries}

NEW THOUGHTS THIS BATCH (not yet in knowledge pages):
{new_thoughts}

INSTRUCTIONS:
1. Find connections between projects that aren't already noted in their Connections sections.
2. Find contradictions (e.g., killed project referenced as active elsewhere, conflicting strategies).
3. Find patterns (e.g., same market thesis being tested in multiple projects).

For each finding, output a JSON object. Output a JSON array:
[{{"type": "connection", "projects": ["slug1", "slug2"], "description": "..."}},
 {{"type": "contradiction", "projects": ["slug1"], "description": "..."}},
 {{"type": "pattern", "projects": ["slug1", "slug2", "slug3"], "description": "..."}}]

Return [] if nothing new found. Be selective — only flag genuinely useful findings.
"""

# ─── Session Discovery ───


def _extract_repo_name(workspace_folder):
    """Extract the repo/project name from a workspace folder path.

    Examples:
      -Users-alice-Documents-GitHub-backend → backend
      -Users-alice-Documents-GitHub-my-app--claude-worktrees-funny-murdock → my-app
      -Users-alice-Documents-iOS-MyApp → MyApp
      -Users-alice → (empty — home dir, no specific repo)
    """
    if not workspace_folder:
        return ""
    # The folder name uses dashes instead of path separators
    # Find the last meaningful segment after common prefixes
    parts = workspace_folder.strip("-").split("-")

    # Reconstruct the path to find the repo name
    # Pattern: -Users-{user}-Documents-GitHub-{repo} or -Users-{user}-Documents-iOS-{repo}
    folder = workspace_folder
    for prefix in ("-Users-", "Users-"):
        if folder.startswith(prefix):
            folder = folder[len(prefix):]
            break
    # Skip the username segment (everything up to Documents, Projects, etc.)
    for marker in ("-Documents-GitHub-", "-Documents-iOS-", "-Documents-",
                   "-Projects-", "-repos-", "-code-", "-dev-", "-src-",
                   "-work-"):
        idx = folder.find(marker)
        if idx >= 0:
            folder = folder[idx + len(marker):]
            break
    else:
        # No known marker found — might be just "-Users-username"
        if not any(c.isalpha() for c in folder.replace("-", "")):
            return ""
        # Use the whole remaining string
        pass

    if not folder:
        return ""

    # Handle worktrees: {repo}--claude-worktrees-{branch} → {repo}
    if "--claude-worktrees-" in folder:
        folder = folder.split("--claude-worktrees-")[0]

    return folder


def find_claude_code_sessions(state):
    sessions = []
    for jsonl in glob.glob(os.path.join(CLAUDE_CODE_BASE, "*", "*.jsonl")):
        if "/subagents/" in jsonl or "\\subagents\\" in jsonl:
            continue
        mtime = os.path.getmtime(jsonl)
        session_id = Path(jsonl).stem
        last_processed = state["processed_sessions"].get(f"code:{session_id}", 0)
        if mtime > last_processed:
            workspace = _extract_repo_name(Path(jsonl).parent.name)
            sessions.append({
                "type": "claude-code", "path": jsonl,
                "session_id": session_id, "mtime": mtime,
                "state_key": f"code:{session_id}",
                "workspace": workspace,
            })
    return sessions


def find_cowork_sessions(state):
    sessions = []
    for session_dir in glob.glob(os.path.join(COWORK_BASE, "*", "*")):
        if not os.path.isdir(session_dir):
            continue
        for json_file in glob.glob(os.path.join(session_dir, "local_*.json")):
            session_id = Path(json_file).stem
            # The actual session directory contains the conversation JSONL
            session_subdir = os.path.join(session_dir, session_id)
            if not os.path.isdir(session_subdir):
                continue
            # Find the conversation JSONL inside the session directory
            # Structure: local_<uuid>/.claude/projects/<name>/<id>.jsonl
            conv_jsonls = glob.glob(os.path.join(
                session_subdir, ".claude", "projects", "*", "*.jsonl"
            ))
            # Filter out subagent files
            conv_jsonls = [
                j for j in conv_jsonls
                if "/subagents/" not in j and "\\subagents\\" not in j
            ]
            if not conv_jsonls:
                continue
            # Use the newest conversation JSONL
            conv_jsonl = max(conv_jsonls, key=os.path.getmtime)
            mtime = os.path.getmtime(conv_jsonl)
            last_processed = state["processed_sessions"].get(f"cowork:{session_id}", 0)
            if mtime > last_processed:
                # Read metadata for title context
                try:
                    meta = json.load(open(json_file))
                    title = meta.get("title", "")
                except Exception:
                    title = ""
                output_dir = os.path.join(session_subdir, "outputs")
                # Extract workspace from the cowork session path
                # Structure: COWORK_BASE/{workspace}/{group}/local_<uuid>/...
                workspace = _extract_repo_name(Path(session_dir).parent.name)
                sessions.append({
                    "type": "cowork",
                    "path": conv_jsonl,
                    "metadata_path": json_file,
                    "title": title,
                    "output_dir": output_dir if os.path.isdir(output_dir) else None,
                    "session_id": session_id, "mtime": mtime,
                    "state_key": f"cowork:{session_id}",
                    "workspace": workspace,
                })
    return sessions


def _extract_workspace_from_content(file_paths):
    """Extract repo name from file:// paths found in content files."""
    import re
    pattern = re.compile(r'file:///Users/[^/]+/Documents/(?:GitHub|iOS)/([^/\s"\'<>]+)')
    for fpath in file_paths:
        try:
            content = Path(fpath).read_text(errors="ignore")
            match = pattern.search(content)
            if match:
                repo = match.group(1)
                # Strip worktree suffixes
                if "--claude-worktrees-" in repo:
                    repo = repo.split("--claude-worktrees-")[0]
                return repo
        except (OSError, UnicodeDecodeError):
            continue
    return ""


def find_antigravity_sessions(state):
    sessions = []
    if not os.path.isdir(ANTIGRAVITY_BRAIN):
        return sessions
    for session_dir in glob.glob(os.path.join(ANTIGRAVITY_BRAIN, "*")):
        if not os.path.isdir(session_dir):
            continue
        session_id = Path(session_dir).name
        md_files = glob.glob(os.path.join(session_dir, "*.md"))
        txt_files = glob.glob(os.path.join(session_dir, "*.txt"))
        all_files = md_files + txt_files
        if not all_files:
            continue
        mtime = max(os.path.getmtime(f) for f in all_files)
        last_processed = state["processed_sessions"].get(f"antigravity:{session_id}", 0)
        if mtime > last_processed:
            workspace = _extract_workspace_from_content(md_files + txt_files)
            sessions.append({
                "type": "antigravity", "path": session_dir,
                "session_id": session_id, "mtime": mtime,
                "state_key": f"antigravity:{session_id}",
                "workspace": workspace,
            })
    return sessions


def _extract_workspace_from_codex(jsonl_path):
    """Extract repo name from <cwd> tags in Codex JSONL."""
    import re
    pattern = re.compile(r'<cwd>/Users/[^/]+/Documents/(?:GitHub|iOS)/([^<]+)</cwd>')
    try:
        with open(jsonl_path, errors="ignore") as f:
            for line in f:
                match = pattern.search(line)
                if match:
                    repo = match.group(1).strip("/")
                    if "--claude-worktrees-" in repo:
                        repo = repo.split("--claude-worktrees-")[0]
                    return repo
                # Also check for cwd in JSON structure
                if '"cwd"' in line or '"working_directory"' in line:
                    try:
                        d = json.loads(line)
                        cwd = d.get("cwd") or d.get("working_directory", "")
                        if "/Documents/GitHub/" in cwd:
                            return cwd.split("/Documents/GitHub/")[-1].split("/")[0]
                        if "/Documents/iOS/" in cwd:
                            return cwd.split("/Documents/iOS/")[-1].split("/")[0]
                    except (json.JSONDecodeError, AttributeError):
                        pass
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def find_codex_sessions(state):
    sessions = []
    for jsonl in glob.glob(os.path.join(CODEX_BASE, "**", "*.jsonl"), recursive=True):
        mtime = os.path.getmtime(jsonl)
        session_id = Path(jsonl).stem
        last_processed = state["processed_sessions"].get(f"codex:{session_id}", 0)
        if mtime > last_processed:
            workspace = _extract_workspace_from_codex(jsonl)
            sessions.append({
                "type": "codex", "path": jsonl,
                "session_id": session_id, "mtime": mtime,
                "state_key": f"codex:{session_id}",
                "workspace": workspace,
            })
    return sessions


def find_cursor_sessions(state):
    """Find Cursor workspace SQLite databases with chat history."""
    sessions = []
    base = PATHS["cursor"]
    if not os.path.isdir(base):
        return sessions
    for ws_dir in glob.glob(os.path.join(base, "*")):
        db_path = os.path.join(ws_dir, "state.vscdb")
        if not os.path.isfile(db_path):
            continue
        mtime = os.path.getmtime(db_path)
        session_id = Path(ws_dir).name
        if mtime > state["processed_sessions"].get(f"cursor:{session_id}", 0):
            sessions.append({
                "type": "cursor", "path": db_path,
                "session_id": session_id, "mtime": mtime,
                "state_key": f"cursor:{session_id}",
                "workspace": "",
            })
    return sessions


def find_copilot_sessions(state):
    """Find GitHub Copilot chat session JSONL files in VS Code workspace storage."""
    sessions = []
    base = PATHS["copilot"]
    if not os.path.isdir(base):
        return sessions
    for jsonl in glob.glob(os.path.join(base, "*", "chatSessions", "*.jsonl")):
        mtime = os.path.getmtime(jsonl)
        session_id = Path(jsonl).stem
        if mtime > state["processed_sessions"].get(f"copilot:{session_id}", 0):
            sessions.append({
                "type": "copilot", "path": jsonl,
                "session_id": session_id, "mtime": mtime,
                "state_key": f"copilot:{session_id}",
                "workspace": "",
            })
    return sessions


def find_cline_sessions(state):
    """Find Cline task conversation files."""
    sessions = []
    base = PATHS["cline"]
    if not os.path.isdir(base):
        return sessions
    for task_dir in glob.glob(os.path.join(base, "*")):
        if not os.path.isdir(task_dir):
            continue
        api_file = os.path.join(task_dir, "api_conversation_history.json")
        if not os.path.isfile(api_file):
            continue
        mtime = os.path.getmtime(api_file)
        session_id = Path(task_dir).name
        if mtime > state["processed_sessions"].get(f"cline:{session_id}", 0):
            sessions.append({
                "type": "cline", "path": api_file,
                "session_id": session_id, "mtime": mtime,
                "state_key": f"cline:{session_id}",
                "workspace": "",
            })
    return sessions


def find_continue_sessions(state):
    """Find Continue.dev session JSON files."""
    sessions = []
    base = PATHS["continue"]
    if not os.path.isdir(base):
        return sessions
    for json_file in glob.glob(os.path.join(base, "*.json")):
        if Path(json_file).name == "sessions.json":
            continue  # skip index file
        mtime = os.path.getmtime(json_file)
        session_id = Path(json_file).stem
        if mtime > state["processed_sessions"].get(f"continue:{session_id}", 0):
            sessions.append({
                "type": "continue", "path": json_file,
                "session_id": session_id, "mtime": mtime,
                "state_key": f"continue:{session_id}",
                "workspace": "",
            })
    return sessions


def find_aider_sessions(state):
    """Find Aider chat history markdown files in common project directories."""
    sessions = []
    # Search in common project directories (NOT $HOME — too slow with iCloud/Library)
    search_dirs = []
    for d in (_HOME / "Documents", _HOME / "Projects", _HOME / "repos",
              _HOME / "code", _HOME / "dev", _HOME / "src"):
        if d.is_dir():
            search_dirs.append(str(d))
    seen = set()
    for search_dir in search_dirs:
        for md_file in glob.glob(os.path.join(search_dir, "**", ".aider.chat.history.md"), recursive=True):
            if md_file in seen:
                continue
            seen.add(md_file)
            mtime = os.path.getmtime(md_file)
            session_id = f"aider-{Path(md_file).parent.name}"
            if mtime > state["processed_sessions"].get(f"aider:{session_id}", 0):
                sessions.append({
                    "type": "aider", "path": md_file,
                    "session_id": session_id, "mtime": mtime,
                    "state_key": f"aider:{session_id}",
                    "workspace": Path(md_file).parent.name,
                })
    return sessions


def find_opencode_sessions(state):
    """Find OpenCode session JSON files."""
    sessions = []
    base = PATHS["opencode"]
    if not os.path.isdir(base):
        return sessions
    for json_file in glob.glob(os.path.join(base, "**", "*.json"), recursive=True):
        mtime = os.path.getmtime(json_file)
        session_id = Path(json_file).stem
        if mtime > state["processed_sessions"].get(f"opencode:{session_id}", 0):
            sessions.append({
                "type": "opencode", "path": json_file,
                "session_id": session_id, "mtime": mtime,
                "state_key": f"opencode:{session_id}",
                "workspace": "",
            })
    return sessions


# ─── Session Extraction ───


def extract_antigravity_session(session_dir, max_chars=30000):
    parts = []
    for ext in ("*.md", "*.txt"):
        for f in sorted(glob.glob(os.path.join(session_dir, ext))):
            try:
                with open(f) as fh:
                    parts.append(fh.read())
            except Exception:
                pass
    return "\n---\n".join(parts)[:max_chars]


def extract_codex_conversation(path, max_chars=30000):
    lines = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())

                    # Old format (2025): {"type": "message", "role": "user", "content": [...]}
                    role = entry.get("role", "")
                    content = entry.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    if content and role in ("user", "assistant"):
                        lines.append(f"{role}: {content[:2000]}")
                        continue

                    # New format (2026+): {"type": "response_item"|"event_msg", "payload": {...}}
                    payload = entry.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    entry_type = entry.get("type", "")

                    if entry_type == "response_item":
                        for c in payload.get("content", []):
                            if isinstance(c, dict) and c.get("text"):
                                ctype = c.get("type", "")
                                r = "user" if ctype == "input_text" else "assistant"
                                lines.append(f"{r}: {c['text'][:2000]}")
                    elif entry_type == "event_msg":
                        msg = payload.get("message", "")
                        if msg and isinstance(msg, str) and len(msg) > 20:
                            lines.append(f"assistant: {msg[:2000]}")

                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return "\n".join(lines)[:max_chars]


def extract_claude_code_conversation(path, max_chars=30000):
    lines = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    msg_type = entry.get("type", "")
                    if msg_type not in ("human", "assistant"):
                        continue
                    message = entry.get("message", {})
                    role = message.get("role", msg_type)
                    content = message.get("content", "")
                    if isinstance(content, list):
                        text_parts = []
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    text_parts.append(block.get("text", ""))
                                elif block.get("type") == "tool_use":
                                    text_parts.append(
                                        f"[tool: {block.get('name', '?')}]"
                                    )
                                elif block.get("type") == "tool_result":
                                    res = block.get("content", "")
                                    if isinstance(res, list):
                                        res = " ".join(
                                            r.get("text", "")
                                            for r in res
                                            if isinstance(r, dict)
                                        )
                                    text_parts.append(f"[result: {str(res)[:500]}]")
                        content = " ".join(text_parts)
                    if content:
                        lines.append(f"{role}: {content[:3000]}")
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return "\n".join(lines)[:max_chars]


def extract_cursor_conversation(path, max_chars=30000):
    """Extract conversation from Cursor's state.vscdb SQLite database."""
    lines = []
    try:
        import sqlite3
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        # Cursor stores composer/chat data in cursorDiskKV table
        cursor.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE '%composer%' "
            "OR key LIKE '%chat%' ORDER BY key"
        )
        for key, value in cursor.fetchall():
            if not value:
                continue
            try:
                data = json.loads(value)
                # Handle composer conversations
                if isinstance(data, dict):
                    for msg in data.get("conversation", data.get("messages", [])):
                        role = msg.get("role", msg.get("type", ""))
                        content = msg.get("content", msg.get("text", ""))
                        if isinstance(content, list):
                            content = " ".join(
                                c.get("text", "") for c in content if isinstance(c, dict)
                            )
                        if content and role in ("user", "assistant", "human"):
                            lines.append(f"{role}: {content[:2000]}")
                elif isinstance(data, list):
                    for msg in data:
                        if isinstance(msg, dict):
                            role = msg.get("role", "")
                            content = msg.get("content", "")
                            if content and role in ("user", "assistant"):
                                lines.append(f"{role}: {content[:2000]}")
            except (json.JSONDecodeError, TypeError):
                pass
        conn.close()
    except Exception as e:
        print(f"    Cursor extract error: {e}")
    return "\n".join(lines)[:max_chars]


def extract_copilot_conversation(path, max_chars=30000):
    """Extract conversation from GitHub Copilot chat JSONL files."""
    lines = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    role = entry.get("role", "")
                    content = entry.get("content", entry.get("message", ""))
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    if content and role in ("user", "assistant"):
                        lines.append(f"{role}: {content[:2000]}")
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return "\n".join(lines)[:max_chars]


def extract_cline_conversation(path, max_chars=30000):
    """Extract conversation from Cline's api_conversation_history.json."""
    lines = []
    try:
        with open(path) as f:
            data = json.load(f)
        messages = data if isinstance(data, list) else data.get("messages", [])
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                content = " ".join(text_parts)
            if content and role in ("user", "assistant", "human"):
                lines.append(f"{role}: {content[:2000]}")
    except Exception:
        pass
    return "\n".join(lines)[:max_chars]


def extract_continue_conversation(path, max_chars=30000):
    """Extract conversation from Continue.dev session JSON files."""
    lines = []
    try:
        with open(path) as f:
            data = json.load(f)
        # Continue stores history as a list of steps or messages
        history = data.get("history", data.get("steps", data.get("messages", [])))
        if isinstance(history, list):
            for step in history:
                if isinstance(step, dict):
                    role = step.get("role", step.get("name", ""))
                    content = step.get("content", step.get("message", step.get("description", "")))
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    if content and isinstance(content, str):
                        lines.append(f"{role or 'unknown'}: {content[:2000]}")
    except Exception:
        pass
    return "\n".join(lines)[:max_chars]


def extract_aider_conversation(path, max_chars=30000):
    """Extract conversation from Aider's .aider.chat.history.md files."""
    try:
        with open(path) as f:
            return f.read()[:max_chars]
    except Exception:
        return ""


def extract_opencode_conversation(path, max_chars=30000):
    """Extract conversation from OpenCode session JSON files."""
    lines = []
    try:
        with open(path) as f:
            data = json.load(f)
        messages = data.get("messages", data.get("conversation", []))
        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, dict):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    if content and role in ("user", "assistant"):
                        lines.append(f"{role}: {content[:2000]}")
    except Exception:
        pass
    return "\n".join(lines)[:max_chars]


def extract_cowork_conversation(path, output_dir=None, max_chars=30000):
    """Extract conversation from a Cowork session JSONL file.
    Format: one JSON object per line with type/role/message fields."""
    lines = []
    try:
        with open(path) as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                # Skip non-message rows (queue-operation, tool_result, etc.)
                row_type = row.get("type", "")
                if row_type in ("queue-operation", "tool_use", "tool_result"):
                    continue

                # Extract role and content from the message envelope
                msg = row.get("message", row)
                role = msg.get("role", row_type)
                content = msg.get("content", "")

                if isinstance(content, list):
                    # Extract text blocks from content array
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    content = " ".join(text_parts)

                if content and role in ("human", "assistant", "user"):
                    lines.append(f"{role}: {content[:3000]}")
    except Exception:
        pass

    if output_dir and os.path.isdir(output_dir):
        for fname in sorted(os.listdir(output_dir))[:20]:
            fpath = os.path.join(output_dir, fname)
            if os.path.isfile(fpath) and os.path.getsize(fpath) < 50000:
                try:
                    with open(fpath) as f:
                        lines.append(f"\n--- Output: {fname} ---\n{f.read()[:5000]}")
                except Exception:
                    pass

    return "\n".join(lines)[:max_chars]


def find_tool_memory_files(max_chars=10000):
    """Find AI tool memory/rules files for bonus context during extraction.
    These are manually curated files that tools use for instructions —
    they often contain project decisions, architecture notes, and preferences."""
    memories = []

    # Claude Code: ~/.claude/CLAUDE.md and project-level CLAUDE.md files
    global_claude_md = _HOME / ".claude" / "CLAUDE.md"
    if global_claude_md.is_file():
        try:
            memories.append(("claude-code-global", global_claude_md.read_text()[:3000]))
        except Exception:
            pass

    # Search common project directories for tool memory files
    memory_filenames = [
        "CLAUDE.md", ".cursorrules", ".windsurfrules",
        "AGENTS.md", "codex.md", ".clinerules",
    ]
    search_dirs = [_HOME]
    for d in (_HOME / "Documents", _HOME / "Projects", _HOME / "repos",
              _HOME / "code", _HOME / "dev", _HOME / "src",
              _HOME / "Documents" / "GitHub"):
        if d.is_dir():
            search_dirs.append(d)

    seen = set()
    for search_dir in search_dirs:
        # Only search 2 levels deep to avoid being too slow
        for depth_pattern in ["*", "*/*"]:
            for name in memory_filenames:
                for f in Path(search_dir).glob(f"{depth_pattern}/{name}"):
                    if f in seen or not f.is_file():
                        continue
                    seen.add(f)
                    try:
                        content = f.read_text()[:2000]
                        if len(content) > 50:  # Skip near-empty files
                            project_hint = f.parent.name
                            memories.append((f"memory-{project_hint}-{name}", content))
                    except Exception:
                        pass

    # Cursor rules directory
    cursor_rules = _HOME / ".cursor" / "rules"
    if cursor_rules.is_dir():
        for f in cursor_rules.glob("*.md"):
            try:
                content = f.read_text()[:2000]
                if len(content) > 50:
                    memories.append((f"cursor-rule-{f.stem}", content))
            except Exception:
                pass

    # Truncate total to max_chars
    result = []
    total = 0
    for name, content in memories:
        if total + len(content) > max_chars:
            break
        result.append((name, content))
        total += len(content)

    return result


# ─── LLM Configuration ───

# Model presets — user-friendly names mapped to provider + model ID
MODEL_CATALOG = {
    # Anthropic
    "haiku":            {"provider": "anthropic", "model": "claude-haiku-4-5",           "display": "Claude Haiku 4.5"},
    "sonnet":           {"provider": "anthropic", "model": "claude-sonnet-4-6",          "display": "Claude Sonnet 4.6"},
    "opus":             {"provider": "anthropic", "model": "claude-opus-4-6",            "display": "Claude Opus 4.6"},
    # OpenAI
    "gpt-5.4":          {"provider": "openai", "model": "gpt-5.4",                      "display": "GPT-5.4"},
    "gpt-5.4-mini":     {"provider": "openai", "model": "gpt-5.4-mini",                 "display": "GPT-5.4 Mini"},
    "gpt-5.4-nano":     {"provider": "openai", "model": "gpt-5.4-nano",                 "display": "GPT-5.4 Nano"},
    "gpt-5.4-pro":      {"provider": "openai", "model": "gpt-5.4-pro",                  "display": "GPT-5.4 Pro"},
    "gpt-4.1":          {"provider": "openai", "model": "gpt-4.1",                      "display": "GPT-4.1"},
    "gpt-4.1-mini":     {"provider": "openai", "model": "gpt-4.1-mini",                 "display": "GPT-4.1 Mini"},
    "gpt-4.1-nano":     {"provider": "openai", "model": "gpt-4.1-nano",                 "display": "GPT-4.1 Nano"},
    "o3":               {"provider": "openai", "model": "o3",                            "display": "o3"},
    "o4-mini":          {"provider": "openai", "model": "o4-mini",                       "display": "o4-mini"},
    # Google
    "gemini-flash":     {"provider": "google", "model": "gemini-3-flash-preview",        "display": "Gemini 3 Flash"},
    "gemini-lite":      {"provider": "google", "model": "gemini-3.1-flash-lite-preview", "display": "Gemini 3.1 Flash Lite"},
    "gemini-pro":       {"provider": "google", "model": "gemini-3.1-pro-preview",        "display": "Gemini 3.1 Pro"},
}

# Pricing: (input_per_mtok, output_per_mtok)
MODEL_PRICING = {
    "haiku":        (1.00,  5.00),
    "sonnet":       (3.00, 15.00),
    "opus":         (5.00, 25.00),
    "gpt-5.4":      (2.50, 15.00),
    "gpt-5.4-mini": (0.75,  4.50),
    "gpt-5.4-nano": (0.20,  1.25),
    "gpt-5.4-pro":  (30.0, 180.0),
    "gpt-4.1":      (2.00,  8.00),
    "gpt-4.1-mini": (0.40,  1.60),
    "gpt-4.1-nano": (0.10,  0.40),
    "o3":           (2.00,  8.00),
    "o4-mini":      (1.10,  4.40),
    "gemini-flash": (0.15,  0.60),
    "gemini-lite":  (0.00,  0.00),  # free tier
    "gemini-pro":   (1.25, 10.00),
}

# Defaults
DEFAULT_EXTRACT_MODEL = "gpt-4.1-mini"
DEFAULT_MERGE_MODEL = "sonnet"


def _display_name(name_or_id):
    """Get human-readable display name for a model."""
    if name_or_id in MODEL_CATALOG:
        return MODEL_CATALOG[name_or_id].get("display", name_or_id)
    return name_or_id


def _resolve_model(name_or_id):
    """Resolve a model name to {provider, model}. Accepts catalog names or raw model IDs."""
    if name_or_id in MODEL_CATALOG:
        return MODEL_CATALOG[name_or_id]
    # Try to infer provider from model ID
    if "claude" in name_or_id or "haiku" in name_or_id or "sonnet" in name_or_id or "opus" in name_or_id:
        return {"provider": "anthropic", "model": name_or_id}
    elif "gpt" in name_or_id or name_or_id.startswith("o3") or name_or_id.startswith("o4"):
        return {"provider": "openai", "model": name_or_id}
    elif "gemini" in name_or_id:
        return {"provider": "google", "model": name_or_id}
    # Default to anthropic
    return {"provider": "anthropic", "model": name_or_id}


def _call_anthropic(model, messages, max_tokens, api_key, temperature=0):
    """Call Anthropic Messages API."""
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }).encode()

    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )

    with urlopen(req) as resp:
        data = json.loads(resp.read())
        return data["content"][0]["text"]


def _call_openai(model, messages, max_tokens, api_key, temperature=0):
    """Call OpenAI Chat Completions API."""
    body = json.dumps({
        "model": model,
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }).encode()

    req = Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
    )

    with urlopen(req) as resp:
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]


def _call_google(model, messages, max_tokens, api_key, temperature=0):
    """Call Google Gemini API."""
    # Convert OpenAI-style messages to Gemini format
    contents = []
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    body = json.dumps({
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }).encode()

    req = Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        data=body,
        headers={"content-type": "application/json"},
    )

    with urlopen(req) as resp:
        data = json.loads(resp.read())
        return data["candidates"][0]["content"]["parts"][0]["text"]


# Global config — set during main() init
_config = {
    "extract_model": DEFAULT_EXTRACT_MODEL,
    "merge_model": DEFAULT_MERGE_MODEL,
    "keys": {},  # {"anthropic": "...", "openai": "...", "google": "..."}
}

# Cost tracking per run
_usage = {
    "extract_calls": 0,
    "merge_calls": 0,
    "input_tokens_est": 0,
    "output_tokens_est": 0,
}

# Rough per-call cost estimates by model
# Based on ~5K input + ~1K output (extract) or ~10K input + ~4K output (merge)
# Using the higher merge estimate as the per-call average
_COST_PER_CALL = {
    # Anthropic (per ~3K tok call: ~2K input + ~1K output)
    "haiku": 0.015,           # $1/$5 per MTok
    "sonnet": 0.09,           # $3/$15 per MTok
    "opus": 0.15,             # $5/$25 per MTok
    # OpenAI
    "gpt-5.4": 0.02,         # $2.50/$15 per MTok
    "gpt-5.4-mini": 0.0075,  # $0.75/$4.50 per MTok
    "gpt-5.4-nano": 0.002,   # $0.20/$1.25 per MTok
    "gpt-5.4-pro": 0.24,     # $30/$180 per MTok
    "gpt-4.1": 0.05,         # $2/$8 per MTok
    "gpt-4.1-mini": 0.01,   # $0.40/$1.60 per MTok
    "gpt-4.1-nano": 0.003,  # $0.10/$0.40 per MTok
    "o3": 0.05,              # $2/$8 per MTok
    "o4-mini": 0.03,         # $1.10/$4.40 per MTok
    # Google
    "gemini-flash": 0.005,   # $0.15/$0.60 per MTok
    "gemini-lite": 0.001,    # free tier
    "gemini-pro": 0.05,      # $1.25/$10 per MTok
}


def call_llm(prompt, role="extract", max_tokens=4096, model_override=None):
    """Unified LLM call. Role is 'extract' or 'merge' — picks the configured model."""
    model_name = model_override or (_config["extract_model"] if role == "extract" else _config["merge_model"])
    if role == "merge":
        max_tokens = max(max_tokens, 8192)

    # Track usage
    if role == "extract":
        _usage["extract_calls"] += 1
    else:
        _usage["merge_calls"] += 1

    resolved = _resolve_model(model_name)
    provider = resolved["provider"]
    model_id = resolved["model"]

    api_key = _config["keys"].get(provider)
    if not api_key:
        raise ValueError(
            f"No API key for provider '{provider}'. "
            f"Set --{provider}-key or {provider.upper()}_API_KEY environment variable."
        )

    messages = [{"role": "user", "content": prompt}]

    callers = {
        "anthropic": _call_anthropic,
        "openai": _call_openai,
        "google": _call_google,
    }

    caller = callers.get(provider)
    if not caller:
        raise ValueError(f"Unknown provider: {provider}")

    # Retry on transient errors (500, 502, 503, 529, timeouts)
    for attempt in range(3):
        try:
            return caller(model_id, messages, max_tokens, api_key)
        except Exception as e:
            err_str = str(e)
            retriable = any(code in err_str for code in ["500", "502", "503", "529", "timed out"])
            if retriable and attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s backoff
                continue
            raise


def _strip_json_fences(text):
    """Safely extract JSON from markdown code fences."""
    if "```json" in text:
        parts = text.split("```json", 1)
        if len(parts) > 1:
            inner = parts[1]
            text = inner.split("```")[0] if "```" in inner else inner
    elif "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
    return text.strip()


def call_claude(text, anthropic_key, workspace="", repo_groups=None):
    """Extract thoughts — uses configured extraction model."""
    # Build workspace context header
    workspace_header = ""
    if workspace:
        mapped = workspace
        if repo_groups and workspace in repo_groups:
            mapped = repo_groups[workspace]
        workspace_header = f"WORKSPACE: {workspace}"
        if mapped != workspace:
            workspace_header += f" (this repo is part of the '{mapped}' product)"
        workspace_header += "\n\n"

    prompt = EXTRACTION_PROMPT + workspace_header + "CONVERSATION:\n" + text
    try:
        text = call_llm(prompt, role="extract", max_tokens=4096)
        text = _strip_json_fences(text)
        try:
            thoughts = json.loads(text)
        except json.JSONDecodeError:
            # Try to recover truncated JSON arrays: "[{...}, {..." → "[{...}]"
            if text.startswith("["):
                # Find the last complete object
                last_close = text.rfind("}")
                if last_close > 0:
                    recovered = text[:last_close + 1] + "]"
                    thoughts = json.loads(recovered)
                else:
                    thoughts = []
            else:
                thoughts = []
        # Normalize: some models return strings instead of objects
        normalized = []
        for t in thoughts:
            if isinstance(t, dict):
                normalized.append(t)
            elif isinstance(t, str) and len(t) > 10:
                normalized.append({"content": t, "project": None, "tags": [], "kind": "project"})
        return normalized
    except (HTTPError, json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        print(f"  LLM extraction error: {e}")
        return []


def call_sonnet(prompt, anthropic_key, max_tokens=8192):
    """Merge knowledge — uses configured merge model."""
    return call_llm(prompt, role="merge", max_tokens=max_tokens)


# ─── Knowledge Pipeline ───


def resolve_aliases(thoughts, store, repo_groups=None):
    """Phase 1a: Resolve project names to canonical slugs.

    Priority: repo_groups (workspace mapping) > exact alias > fuzzy alias > new slug.
    """
    alias_list = store.get_aliases()
    aliases = {a["alias"]: a["canonical_slug"] for a in alias_list}
    repo_groups = repo_groups or {}
    _uuid_re = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

    for t in thoughts:
        project = t.get("project")
        workspace = t.get("workspace", "")

        if not project:
            # Only map workspace to project for "project" kind thoughts.
            # meta/idea thoughts without a project should stay unlinked
            # so they route to me.md / ideas.md correctly.
            kind = t.get("kind", "project")
            if kind == "project" and workspace and workspace in repo_groups:
                t["project"] = repo_groups[workspace]
                t["canonical_project"] = repo_groups[workspace]
            continue

        # Priority 1: If workspace maps to a repo_group, use that
        if workspace and workspace in repo_groups:
            canonical = repo_groups[workspace]
            t["canonical_project"] = canonical
            if project not in aliases:
                aliases[project] = canonical
                store.save_alias(project, canonical)
                print(f"    Workspace override: '{project}' -> '{canonical}' (repo: {workspace})")
            continue

        # Priority 2: Exact alias match
        if project in aliases:
            t["canonical_project"] = aliases[project]
            continue

        # Priority 3: Fuzzy match (skip UUID slugs)
        best_match = None
        best_score = 0
        project_lower = project.lower().replace(" ", "").replace("-", "").replace("_", "")
        for alias, slug in aliases.items():
            if _uuid_re.match(slug):
                continue  # never match to a UUID slug
            alias_lower = alias.lower().replace(" ", "").replace("-", "").replace("_", "")
            score = SequenceMatcher(None, project_lower, alias_lower).ratio()
            if score > best_score:
                best_score = score
                best_match = slug

        if best_score > 0.75 and best_match:
            t["canonical_project"] = best_match
            aliases[project] = best_match
            store.save_alias(project, best_match)
            print(f"    Auto-aliased '{project}' -> '{best_match}' (score: {best_score:.2f})")
        else:
            # Priority 4: Create a new slug from the project name.
            # Use `project`, NOT `workspace`. The LLM reads the actual
            # conversation and names the project semantically; workspace is
            # just the folder files happen to live in. A thought tagged
            # `project="kidworthy"` inside a calledthird directory is about
            # kidworthy — don't overwrite it with the folder name.
            # (We're guaranteed to have a project here: the `if not project:`
            # branch at the top of the loop `continue`s before we get here.)
            slug = project.lower().replace(" ", "-")
            slug = "".join(c for c in slug if c.isalnum() or c == "-")
            # Never create UUID slugs — skip if the result looks like a UUID
            if _uuid_re.match(slug):
                continue
            t["canonical_project"] = slug
            aliases[project] = slug
            store.save_alias(project, slug)
            print(f"    New project alias: '{project}' -> '{slug}'")

    return thoughts


def deduplicate_thoughts(thoughts, store):
    """Phase 1b: Mark duplicate thoughts."""
    by_project = defaultdict(list)
    for t in thoughts:
        cp = t.get("canonical_project")
        if cp:
            by_project[cp].append(t)

    skipped = 0
    for cp, project_thoughts in by_project.items():
        existing = store.get_recent_thoughts(cp, limit=30)
        existing_prefixes = {t["content"][:80] for t in existing if t.get("content")}

        for t in project_thoughts:
            prefix = t["content"][:80]
            if prefix in existing_prefixes:
                t["skipped"] = True
                t["skip_reason"] = "duplicate"
                skipped += 1
            else:
                existing_prefixes.add(prefix)

    if skipped:
        print(f"  Dedup: skipped {skipped} duplicate thoughts")
    return thoughts


def persist_thought_metadata(thoughts, store):
    """Persist aliasing/dedup metadata for thoughts saved before normalization."""
    for t in thoughts:
        if not t.get("id"):
            continue

        updates = {}
        if "canonical_project" in t:
            updates["canonical_project"] = t.get("canonical_project")
        if "skipped" in t:
            updates["skipped"] = t.get("skipped", False)
        if "skip_reason" in t:
            updates["skip_reason"] = t.get("skip_reason")
        if t.get("skipped"):
            updates["processed"] = True

        if updates:
            store.update_thought(t["id"], updates)

    return thoughts


def merge_into_knowledge_pages(thoughts_by_project, store, anthropic_key):
    """Phase 2: Merge new thoughts into knowledge pages using Sonnet."""
    for slug, thoughts in thoughts_by_project.items():
        if not thoughts:
            continue

        print(f"\n  Merging {len(thoughts)} thoughts into '{slug}'...")

        # Read existing page
        page_content, version = store.get_page(slug)
        if not page_content:
            today = datetime.now().strftime("%Y-%m-%d")
            display_name = slug.replace("-", " ").title()
            page_content = KNOWLEDGE_PAGE_TEMPLATE.format(name=display_name, date=today)

        # Format thoughts for prompt
        thought_lines = []
        for t in thoughts:
            stale = "[STALE - project may be killed/paused] " if t.get("_stale") else ""
            machine_tag = f", machine: {t['machine']}" if t.get("machine") else ""
            thought_lines.append(
                f"- {stale}[{t.get('source', 'unknown')}, "
                f"{t.get('created_at', 'unknown')[:10]}{machine_tag}] {t['content']}"
            )
        new_thoughts_text = "\n".join(thought_lines)

        # Call Sonnet to merge
        prompt = MERGE_PROMPT.format(
            page_content=page_content,
            new_thoughts=new_thoughts_text,
        )

        try:
            response_text = call_sonnet(prompt, anthropic_key)
        except (HTTPError, KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
            print(f"    Merge API error: {e}")
            continue

        # Extract change summary
        change_summary = ""
        if "CHANGE_SUMMARY:" in response_text:
            parts = response_text.split("CHANGE_SUMMARY:")
            updated_content = parts[0].strip()
            change_summary = parts[1].strip()
        else:
            updated_content = response_text.strip()

        # Save updated page
        new_version = version + 1
        store.save_page(slug, updated_content, new_version)

        # Mark thoughts as merged
        for t in thoughts:
            if t.get("id"):
                store.update_thought(t["id"], {
                    "merged_into_page": slug,
                    "processed": True,
                    "canonical_project": t.get("canonical_project", slug),
                })

        print(f"    ✓ Updated '{slug}' v{new_version}: {change_summary[:80]}")
        time.sleep(1)  # Rate limiting for Anthropic API


def merge_into_me_page(thoughts, store, anthropic_key):
    """Merge project-less / meta thoughts into me.md."""
    if not thoughts:
        return

    print(f"\n  Merging {len(thoughts)} thoughts into 'me'...")

    page_content, version = store.get_page("me")
    if not page_content:
        page_content = ME_PAGE_TEMPLATE

    thought_lines = []
    for t in thoughts:
        machine_tag = f", machine: {t['machine']}" if t.get("machine") else ""
        thought_lines.append(
            f"- [{t.get('source', 'unknown')}, "
            f"{t.get('created_at', 'unknown')[:10]}{machine_tag}] {t['content']}"
        )

    prompt = ME_MERGE_PROMPT.format(
        page_content=page_content,
        new_thoughts="\n".join(thought_lines),
    )

    try:
        response_text = call_sonnet(prompt, anthropic_key)
    except (HTTPError, KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
        print(f"    Me page merge error: {e}")
        return

    change_summary = ""
    if "CHANGE_SUMMARY:" in response_text:
        parts = response_text.split("CHANGE_SUMMARY:")
        updated_content = parts[0].strip()
        change_summary = parts[1].strip()
    else:
        updated_content = response_text.strip()

    new_version = version + 1
    store.save_page("me", updated_content, new_version)

    for t in thoughts:
        if t.get("id"):
            store.update_thought(t["id"], {
                "merged_into_page": "me",
                "processed": True,
            })

    print(f"    ✓ Updated 'me' v{new_version}: {change_summary[:80]}")


def merge_into_ideas_page(thoughts, store, anthropic_key):
    """Merge idea-kind thoughts into ideas.md."""
    if not thoughts:
        return

    print(f"\n  Merging {len(thoughts)} ideas into 'ideas'...")

    page_content, version = store.get_page("ideas")
    if not page_content:
        page_content = IDEAS_PAGE_TEMPLATE

    thought_lines = []
    for t in thoughts:
        thought_lines.append(
            f"- [{t.get('source', 'unknown')}, "
            f"{t.get('created_at', 'unknown')[:10]}] {t['content']}"
        )

    prompt = IDEAS_MERGE_PROMPT.format(
        page_content=page_content,
        new_thoughts="\n".join(thought_lines),
    )

    try:
        response_text = call_sonnet(prompt, anthropic_key)
    except (HTTPError, KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
        print(f"    Ideas page merge error: {e}")
        return

    change_summary = ""
    if "CHANGE_SUMMARY:" in response_text:
        parts = response_text.split("CHANGE_SUMMARY:")
        updated_content = parts[0].strip()
        change_summary = parts[1].strip()
    else:
        updated_content = response_text.strip()

    new_version = version + 1
    store.save_page("ideas", updated_content, new_version)

    for t in thoughts:
        if t.get("id"):
            store.update_thought(t["id"], {
                "merged_into_page": "ideas",
                "processed": True,
            })

    print(f"    ✓ Updated 'ideas' v{new_version}: {change_summary[:80]}")


def run_cross_reference_scan(store, anthropic_key, new_thoughts=None):
    """Phase 3: Cross-reference scan across all knowledge pages."""
    print("\n  Running cross-reference scan...")

    pages = store.get_all_pages()
    if not pages:
        print("    No knowledge pages to cross-reference")
        return

    # Build summaries
    summaries = []
    for p in pages:
        content = p["content"]
        overview = ""
        if "## Overview" in content:
            start = content.find("## Overview") + len("## Overview")
            end = content.find("\n##", start)
            overview = content[start:end].strip()[:300] if end > 0 else content[start:start + 300].strip()
        connections = ""
        if "## Connections" in content:
            start = content.find("## Connections") + len("## Connections")
            end = content.find("\n##", start)
            connections = content[start:end].strip()[:200] if end > 0 else content[start:start + 200].strip()
        summaries.append(f"- **{p['slug']}**: {overview}\n  Connections: {connections or 'none'}")

    new_thoughts_text = ""
    if new_thoughts:
        new_thoughts_text = "\n".join(
            f"- [{t.get('source', '?')}, {t.get('canonical_project', '?')}] {t['content'][:150]}"
            for t in new_thoughts[:30]
        )

    prompt = CROSS_REFERENCE_PROMPT.format(
        summaries="\n".join(summaries),
        new_thoughts=new_thoughts_text or "(none)",
    )

    try:
        response_text = call_sonnet(prompt, anthropic_key, max_tokens=2048)

        response_text = _strip_json_fences(response_text)

        findings = json.loads(response_text.strip())

        if findings:
            print(f"    Found {len(findings)} cross-references:")
            for f in findings:
                desc = f.get("description", "")
                ftype = f.get("type", "unknown")
                projects = f.get("projects", [])
                print(f"      [{ftype}] {', '.join(projects)}: {desc[:80]}")

                # Save as cross-cutting thought
                store.save_thought({
                    "content": f"[{ftype}] {desc}",
                    "source": "gyrus",
                    "session_id": "cross-reference-scan",
                    "project": None,
                    "tags": ["cross-reference", ftype] + projects,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
        else:
            print("    No new cross-references found")

    except (HTTPError, json.JSONDecodeError, KeyError) as e:
        print(f"    Cross-reference scan error: {e}")


def _parse_status_overrides(store):
    """Read user-edited status.md for status overrides.

    Format: each line like `- **project-slug**: active | ...` or `- **project-slug**: killed`
    The first word after the colon is the status override.
    """
    status_path = store.base_dir / "status.md" if hasattr(store, "base_dir") else Path.home() / ".gyrus" / "status.md"
    overrides = {}
    if not status_path.exists():
        return overrides
    for line in status_path.read_text().splitlines():
        line = line.strip()
        if not line.startswith("- **"):
            continue
        # Parse: - **slug**: status | ...
        try:
            slug = line.split("**")[1]
            rest = line.split("**: ", 1)[1] if "**: " in line else ""
            if rest:
                # First word is the status
                status_word = rest.split("|")[0].strip().split()[0].lower()
                if status_word in ("active", "killed", "dormant", "paused", "brainstorm", "idea"):
                    overrides[slug] = status_word
        except (IndexError, ValueError):
            continue
    return overrides


# ─── Dataless / iCloud safety ──────────────────────────────────────────────
# macOS "Optimize Mac Storage" can evict iCloud Drive file contents while
# keeping their metadata (flag SF_DATALESS, 0x40000000). Opening such a file
# normally triggers on-demand materialization — but if the file provider is
# stuck/offline, open() blocks forever with no output. We defend against that
# by (a) detecting the flag via stat() and (b) time-boxing every read.

_SF_DATALESS = 0x40000000


def _is_dataless(path):
    """True if macOS has evicted this file's data (cheap metadata-only check)."""
    try:
        return bool(getattr(path.stat(), "st_flags", 0) & _SF_DATALESS)
    except OSError:
        return False


class _ReadTimeout(Exception):
    pass


def _read_text_safe(path, timeout_s=5):
    """Read a file's text with hard timeout + dataless skip.
    Returns the text, or None if dataless / timed out / unreadable."""
    if _is_dataless(path):
        return None
    try:
        import signal as _sig
        has_alarm = hasattr(_sig, "SIGALRM")
    except ImportError:
        has_alarm = False
    if not has_alarm:
        try:
            return path.read_text()
        except (OSError, UnicodeDecodeError):
            return None

    def _handler(signum, frame):
        raise _ReadTimeout()

    prev = _sig.signal(_sig.SIGALRM, _handler)
    _sig.alarm(timeout_s)
    try:
        return path.read_text()
    except (_ReadTimeout, OSError, UnicodeDecodeError):
        return None
    finally:
        _sig.alarm(0)
        _sig.signal(_sig.SIGALRM, prev)


def _get_project_recency(store):
    """Get the most recent thought date per project.

    Streams thoughts files with per-file timeout + dataless-skip so a stuck
    iCloud sync can't freeze `gyrus status`. Prints live progress so the user
    always sees forward motion.
    """
    recency = {}
    thoughts_dir = store.base_dir / "thoughts" if hasattr(store, "base_dir") else Path.home() / ".gyrus" / "thoughts"
    if not thoughts_dir.exists():
        return recency
    files = sorted(thoughts_dir.glob("*.jsonl"), reverse=True)
    total = len(files)
    if total == 0:
        return recency
    skipped = []
    for i, jsonl_file in enumerate(files, 1):
        sys.stdout.write(f"\r  scanning thoughts {i}/{total} {jsonl_file.stem}   ")
        sys.stdout.flush()
        text = _read_text_safe(jsonl_file, timeout_s=5)
        if text is None:
            skipped.append(jsonl_file.name)
            continue
        for line in text.splitlines():
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            cp = t.get("canonical_project") or t.get("merged_into_page")
            if cp and cp not in recency:
                created = t.get("created_at", "")[:10]
                if created:
                    recency[cp] = created
    sys.stdout.write("\r" + " " * 72 + "\r")
    sys.stdout.flush()
    if skipped:
        print(f"  ⚠️  skipped {len(skipped)} dataless/stuck thoughts file(s): "
              f"{', '.join(skipped[:3])}{'…' if len(skipped) > 3 else ''}")
        print(f"     force download with:  brctl download \"{thoughts_dir}\"")
    return recency


def _print_heartbeat(base_dir):
    """One-line liveness signal printed on every invocation.
    Uses stat only (filename-based date), never opens files, so it can never
    hang even if every thought file is dataless.
    """
    thoughts_dir = base_dir / "thoughts"
    if not thoughts_dir.exists():
        print("  ⚠️  no thoughts/ dir yet — run `gyrus` once to ingest")
        return
    # Filenames are YYYY-MM-DD.jsonl so lexicographic sort == chronological
    files = sorted(thoughts_dir.glob("*.jsonl"))
    if not files:
        print("  ⚠️  no thoughts yet — run `gyrus` to ingest")
        return
    newest = files[-1]
    try:
        last_date = datetime.strptime(newest.stem, "%Y-%m-%d").date()
    except ValueError:
        return
    days_ago = (datetime.now().date() - last_date).days
    warn = ""
    if days_ago >= 3:
        warn = "  ⚠️  ingest looks stale — check launchd/cron (`gyrus --show-log`)"
    print(f"  gyrus v{__version__} · last thought: {last_date} "
          f"({days_ago}d ago){warn}")


# ─── Git sync ──────────────────────────────────────────────────────────────
# Gyrus uses a private GitHub repo for cross-machine sync. Every run pulls
# from origin before ingest and pushes after. All operations are non-fatal:
# a network failure never blocks local ingest. Set up via `gyrus init`.

def _git_run(args, cwd, timeout=60):
    """Run a git command. Returns (returncode, stdout, stderr). Never raises."""
    import subprocess
    try:
        r = subprocess.run(
            ["git"] + args, cwd=str(cwd), timeout=timeout,
            capture_output=True, text=True,
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return 1, "", str(e)


def _git_is_repo(base_dir):
    return (Path(base_dir) / ".git").exists()


def _git_remote_url(base_dir):
    if not _git_is_repo(base_dir):
        return None
    rc, out, _ = _git_run(["remote", "get-url", "origin"], base_dir, timeout=5)
    return out if rc == 0 and out else None


def _git_identity_args(base_dir):
    """Return leading `-c` args for `git commit` that guarantee an author
    identity exists. Respects existing user.email/user.name — only fills
    the gap, so a commit on a box without `git config --global user.email`
    still works and users who've configured git keep their real identity."""
    args = []
    rc, out, _ = _git_run(["config", "user.email"], base_dir, timeout=5)
    if rc != 0 or not out:
        args.extend(["-c", "user.email=gyrus@localhost"])
    rc, out, _ = _git_run(["config", "user.name"], base_dir, timeout=5)
    if rc != 0 or not out:
        args.extend(["-c", "user.name=gyrus"])
    return args


def _git_pull(base_dir, quiet=True):
    """Rebase-pull from origin. Non-fatal. Returns (ok, short_message).
    No-ops silently if there's no upstream yet (first run after init)."""
    if not _git_remote_url(base_dir):
        return True, "no remote"
    # If there's no upstream configured yet, nothing to pull
    rc_up, _, _ = _git_run(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        base_dir, timeout=5,
    )
    if rc_up != 0:
        return True, "no upstream yet"
    rc, _, err = _git_run(
        ["pull", "--rebase", "--autostash", "--quiet"],
        base_dir, timeout=30,
    )
    if rc == 0:
        return True, "pulled"
    # Most common failure: no network / auth — keep short
    msg = (err.splitlines()[-1] if err else "failed")[:80]
    return False, msg


def _git_commit_push(base_dir, message, quiet=True):
    """Stage, commit, and push any changes. Non-fatal. Returns (ok, summary).
    Uses `push -u origin HEAD` so upstream is set on the first successful
    push — handles the post-`gh repo create` case where the remote exists
    but the initial push lost a race against GitHub backend propagation.
    Retries once on non-fast-forward (auto-pull then re-push)."""
    if not _git_remote_url(base_dir):
        return True, "no remote"
    _git_run(["add", "-A"], base_dir, timeout=15)
    rc, staged, _ = _git_run(
        ["diff", "--cached", "--name-only"], base_dir, timeout=10,
    )
    if not staged:
        # No new work — but check for local commits that haven't been pushed
        # (e.g. the initial commit from `gh repo create --push` that lost the
        # GitHub-propagation race). Skip the network call in the normal case.
        rc_ahead, ahead, _ = _git_run(
            ["rev-list", "--count", "HEAD", "--not", "--remotes=origin"],
            base_dir, timeout=5,
        )
        if rc_ahead == 0 and ahead and ahead != "0":
            rc, _, err = _git_run(
                ["push", "-u", "origin", "HEAD", "--quiet"],
                base_dir, timeout=30,
            )
            if rc == 0:
                return True, f"pushed {ahead} pending commit(s)"
            return False, f"push failed: {err[:60]}"
        return True, "nothing to commit"
    n_files = len(staged.splitlines())
    rc, _, err = _git_run(
        _git_identity_args(base_dir) + ["commit", "-m", message, "--quiet"],
        base_dir, timeout=15,
    )
    if rc != 0:
        return False, f"commit failed: {err[:60]}"
    rc, _, err = _git_run(
        ["push", "-u", "origin", "HEAD", "--quiet"], base_dir, timeout=30,
    )
    if rc != 0:
        # Remote moved — pull-rebase then retry once
        _git_run(["pull", "--rebase", "--autostash", "--quiet"],
                 base_dir, timeout=30)
        rc, _, err = _git_run(
            ["push", "-u", "origin", "HEAD", "--quiet"], base_dir, timeout=30,
        )
    if rc != 0:
        return False, f"push failed: {err[:60]}"
    return True, f"pushed {n_files} file(s)"


def _autosync_pull(base_dir):
    """Quiet pull on every run. Prints a single line if something happened."""
    if not _git_remote_url(base_dir):
        return
    ok, msg = _git_pull(base_dir)
    if ok and msg == "pulled":
        print("  ↻ pulled latest from origin")
    elif not ok:
        print(f"  ⚠️  git pull failed ({msg}) — continuing with local state")


def _autosync_push(base_dir, message):
    """Quiet commit+push at the end of a successful command."""
    if not _git_remote_url(base_dir):
        return
    ok, msg = _git_commit_push(base_dir, message)
    if ok and msg.startswith("pushed"):
        print(f"  ↑ synced to origin ({msg})")
    elif not ok:
        print(f"  ⚠️  git push failed ({msg}) — will retry next run")


# ─── Doctor: diagnostic health check ───────────────────────────────────────

# Known cloud-sync path markers. First match wins. Apple's unified
# Library/CloudStorage dir covers most modern providers on macOS; legacy
# per-vendor folders in the home dir catch older installs + Linux/Windows.
_CLOUD_SYNC_MARKERS = [
    ("Mobile Documents/com~apple~CloudDocs", "iCloud Drive"),
    ("Library/CloudStorage/GoogleDrive",     "Google Drive"),
    ("Library/CloudStorage/Dropbox",         "Dropbox"),
    ("Library/CloudStorage/OneDrive",        "OneDrive"),
    ("Library/CloudStorage/Box",             "Box"),
    ("Library/CloudStorage/",                "macOS cloud sync"),
    ("/Dropbox/",                            "Dropbox"),
    ("/Google Drive/",                       "Google Drive"),
    ("/GoogleDrive/",                        "Google Drive"),
    ("/OneDrive/",                           "OneDrive"),
    ("/OneDrive - ",                         "OneDrive"),  # Windows multi-account suffix
    ("/Box Sync/",                           "Box"),
    ("/Box/",                                "Box"),
    ("/Sync/",                               "Sync.com"),
    ("/pCloud Drive/",                       "pCloud"),
    ("/Proton Drive/",                       "Proton Drive"),
]


def _detect_cloud_sync(path):
    """Return the provider name if `path` is inside a known cloud-sync folder,
    else None. Handles symlinks, iCloud Desktop/Documents redirection, and
    paths that don't exist yet (checks the closest existing ancestor).
    Path-separator-agnostic so Windows backslash paths match our forward-slash
    markers."""
    p = Path(path).expanduser()
    candidates = [str(p)]
    try:
        candidates.append(str(p.resolve()))
    except OSError:
        pass
    if not p.exists() and p.parent.exists():
        try:
            candidates.append(str(p.parent.resolve() / p.name))
        except OSError:
            pass
    for c in candidates:
        c_fwd = c.replace("\\", "/")
        for marker, name in _CLOUD_SYNC_MARKERS:
            if marker in c_fwd:
                return name
    return None


def _doctor_check_storage(base_dir):
    """Warn if gyrus is stored in a cloud-synced folder (eviction / lock risk)."""
    resolved = base_dir.resolve()
    provider = _detect_cloud_sync(resolved)
    if provider:
        return ("warn", "storage",
                f"~/.gyrus → {resolved} ({provider})",
                f"{provider} can lock/evict files and hang reads.\n"
                "     Use a plain local path (e.g. ~/gyrus-local) + "
                "`gyrus init` for GitHub sync instead.")
    return ("ok", "storage", f"local filesystem ({resolved})", None)


def _doctor_check_dataless(base_dir):
    """Scan for iCloud-evicted files that will hang on read."""
    dataless = []
    # Check the top-level gyrus files and the thoughts/projects dirs
    for subdir in ["", "thoughts", "projects"]:
        d = base_dir / subdir if subdir else base_dir
        if not d.exists() or not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and _is_dataless(p):
                dataless.append(p.relative_to(base_dir))
    if not dataless:
        return ("ok", "dataless files", "none", None)
    names = ", ".join(str(p) for p in dataless[:3])
    if len(dataless) > 3:
        names += f", +{len(dataless) - 3} more"
    return ("fail", "dataless files",
            f"{len(dataless)} evicted ({names})",
            f"killall bird fileproviderd; sleep 5; brctl download \"{base_dir}\"")


def _doctor_check_schedule():
    """Detect whether an hourly cron or launchd job is set up for gyrus."""
    import subprocess
    try:
        ct = subprocess.run(["crontab", "-l"], capture_output=True,
                            text=True, timeout=5)
        cron_has_gyrus = "gyrus" in (ct.stdout or "") or "ingest.py" in (ct.stdout or "")
    except (subprocess.SubprocessError, FileNotFoundError):
        cron_has_gyrus = False
    launchagents = Path.home() / "Library" / "LaunchAgents"
    launchd_files = []
    if launchagents.exists():
        launchd_files = [f.name for f in launchagents.iterdir()
                         if "gyrus" in f.name.lower()]
    if cron_has_gyrus:
        return ("ok", "schedule", "hourly cron configured", None)
    if launchd_files:
        return ("ok", "schedule",
                f"launchd: {', '.join(launchd_files)}", None)
    return ("warn", "schedule", "no cron / launchd found",
            "add to crontab:  0 * * * * ~/.local/bin/gyrus")


def _doctor_check_env(base_dir):
    """Verify .env + at least one model API key."""
    env_file = base_dir / ".env"
    if not env_file.exists():
        return ("fail", "API keys", "no .env file",
                f"create {env_file} with ANTHROPIC_API_KEY=sk-...")
    try:
        text = _read_text_safe(env_file, timeout_s=5)
    except OSError:
        text = None
    if text is None:
        return ("fail", "API keys", ".env unreadable (dataless?)", None)
    keys_found = []
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(k + "=") and len(line) > len(k) + 5:
                keys_found.append(k)
                break
    if not keys_found:
        return ("fail", "API keys", ".env has no model keys",
                "add ANTHROPIC_API_KEY=sk-... to " + str(env_file))
    return ("ok", "API keys", ", ".join(keys_found), None)


def _doctor_check_sources():
    """Check that at least one AI-tool session source is reachable."""
    total = 0
    hits = []
    for name in ("claude-code", "cowork", "codex", "antigravity", "cursor"):
        base = PATHS.get(name)
        if not base or not Path(base).exists():
            continue
        # Count *.jsonl files two levels deep (don't recurse into everything)
        try:
            cnt = len(list(Path(base).glob("*/*.jsonl")))
            cnt += len(list(Path(base).glob("*/*/*.jsonl")))
        except OSError:
            cnt = 0
        if cnt:
            hits.append(f"{name} ({cnt})")
            total += cnt
    if total == 0:
        return ("fail", "session sources",
                "no AI-tool sessions found on disk",
                "check that Claude Code / Cowork / Cursor etc are installed")
    return ("ok", "session sources", ", ".join(hits), None)


def _doctor_check_backlog(base_dir):
    """Count unprocessed Claude Code sessions vs state file."""
    state_path = base_dir / ".ingest-state.json"
    if not state_path.exists():
        return ("warn", "backlog", "no .ingest-state.json yet", None)
    text = _read_text_safe(state_path, timeout_s=5)
    if text is None:
        return ("fail", "backlog", ".ingest-state.json unreadable", None)
    try:
        state = json.loads(text)
    except json.JSONDecodeError:
        return ("fail", "backlog", "corrupt .ingest-state.json",
                f"rm {state_path} and re-run (will reprocess)")
    processed = state.get("processed_sessions", {})
    # Count sessions newer than their last-processed mtime
    unprocessed = 0
    cc_base = PATHS.get("claude-code")
    if cc_base and Path(cc_base).exists():
        for jsonl in Path(cc_base).glob("*/*.jsonl"):
            if "/subagents/" in str(jsonl):
                continue
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            key = f"code:{jsonl.stem}"
            if mtime > processed.get(key, 0):
                unprocessed += 1
    if unprocessed == 0:
        return ("ok", "backlog", "fully caught up", None)
    status = "warn" if unprocessed < 20 else "fail"
    return (status, "backlog",
            f"{unprocessed} unprocessed sessions since last run",
            "run `gyrus` to process them")


def _doctor_check_lockfile():
    """Detect a stale gyrus lockfile."""
    lock = _lock_path()
    if not lock.exists():
        return ("ok", "lockfile", "none held", None)
    try:
        data = json.loads(lock.read_text())
        age_min = (time.time() - data.get("time", 0)) / 60
        machine = data.get("machine", "?")
    except (OSError, json.JSONDecodeError):
        return ("warn", "lockfile", "present but unreadable",
                f"rm {lock}")
    if age_min > 30:
        return ("warn", "lockfile",
                f"stale: {age_min:.0f}m old on {machine}",
                f"rm {lock}")
    return ("ok", "lockfile", f"fresh: {age_min:.0f}m old on {machine}", None)


def _doctor_check_git_sync(base_dir):
    """Check whether GitHub sync is configured and reachable."""
    if not _git_is_repo(base_dir):
        return ("warn", "git sync", "not a git repo",
                "run `gyrus init` to set up cross-machine sync")
    remote = _git_remote_url(base_dir)
    if not remote:
        return ("warn", "git sync", "no origin remote",
                "run `gyrus init` or add remote manually")
    # Reachability (short timeout — if offline, don't block doctor)
    rc, _, err = _git_run(["ls-remote", "--heads", "origin"],
                          base_dir, timeout=5)
    if rc != 0:
        return ("warn", "git sync", f"origin unreachable: {err[:40]}", None)
    # Ahead/behind
    rc, out, _ = _git_run(
        ["rev-list", "--count", "--left-right", "HEAD...@{u}"],
        base_dir, timeout=5,
    )
    if rc == 0 and out and "\t" in out:
        ahead, behind = out.split("\t")
        tag = (f"ahead {ahead}" if ahead != "0" else "") + \
              (f", behind {behind}" if behind != "0" else "")
        summary = tag.strip(", ") or "in sync"
    else:
        summary = "ready"
    return ("ok", "git sync", f"{remote} ({summary})", None)


def _doctor_check_freshness(base_dir):
    """Re-use heartbeat logic: how old is the newest thought?"""
    thoughts_dir = base_dir / "thoughts"
    if not thoughts_dir.exists():
        return ("warn", "ingest freshness", "no thoughts/ dir", None)
    files = sorted(thoughts_dir.glob("*.jsonl"))
    if not files:
        return ("warn", "ingest freshness", "no thoughts yet", None)
    newest = files[-1]
    try:
        last_date = datetime.strptime(newest.stem, "%Y-%m-%d").date()
    except ValueError:
        return ("warn", "ingest freshness",
                f"can't parse date from {newest.name}", None)
    days = (datetime.now().date() - last_date).days
    if days == 0:
        return ("ok", "ingest freshness", f"today ({last_date})", None)
    if days <= 2:
        return ("ok", "ingest freshness",
                f"{days}d ago ({last_date})", None)
    status = "warn" if days <= 7 else "fail"
    return (status, "ingest freshness",
            f"{days}d ago ({last_date}) — stalled",
            "check recent ingest.log output for errors")


# ─── Doctor fixes (invoked by --fix) ──────────────────────────────────────
# Each fixer returns (ok: bool, message: str). We deliberately keep the set
# small and safe: no data migrations, no LLM-costing operations, no global
# state changes beyond what the installer would also do.

def _doctor_fix_lockfile():
    """Remove the stale lockfile — the check only flags this when it's >30m old."""
    lock = _lock_path()
    if not lock.exists():
        return True, "no lockfile to remove"
    try:
        lock.unlink()
        return True, "removed stale lockfile"
    except OSError as e:
        return False, f"couldn't remove: {e}"


def _doctor_fix_schedule():
    """Install an hourly cron entry if none exists. Mirrors `gyrus init`."""
    import subprocess
    if _has_gyrus_cron():
        return True, "cron already configured"
    gyrus_bin = _which("gyrus") or str(Path.home() / ".local" / "bin" / "gyrus")
    cron_line = f"0 * * * * {gyrus_bin} >/dev/null 2>&1"
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True,
                           text=True, timeout=5)
        existing = r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError):
        return False, "crontab not available on this system"
    new_cron = (existing.rstrip() + "\n" + cron_line + "\n").lstrip("\n")
    try:
        p = subprocess.run(["crontab", "-"], input=new_cron,
                           text=True, timeout=10)
        if p.returncode == 0:
            return True, "installed hourly cron"
    except subprocess.SubprocessError:
        pass
    return False, "crontab install failed"


def _doctor_fix_dataless(base_dir):
    """Ask iCloud to materialize dataless files. macOS-only, no daemon kill."""
    import subprocess
    if sys.platform != "darwin":
        return True, "not macOS — no-op"
    if not _which("brctl"):
        return False, "brctl not available"
    try:
        subprocess.run(["brctl", "download", str(base_dir)],
                       timeout=60, check=False, capture_output=True)
    except subprocess.SubprocessError as e:
        return False, f"brctl failed: {e}"
    # Give iCloud a moment, then re-scan
    time.sleep(3)
    remaining = []
    for sub in ("", "thoughts", "projects"):
        d = base_dir / sub if sub else base_dir
        if not d.exists() or not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and _is_dataless(p):
                remaining.append(p.name)
    if not remaining:
        return True, "all files materialized"
    return False, (f"{len(remaining)} still dataless "
                   f"(try: killall bird fileproviderd)")


def _doctor_fix_git_sync(base_dir):
    """Initialize a local repo if missing, or pull+push if configured."""
    if not _git_is_repo(base_dir):
        rc, _, err = _git_run(
            ["init", "--initial-branch=main", "--quiet"],
            base_dir, timeout=10,
        )
        if rc != 0:
            return False, f"git init failed: {err[:60]}"
        gitignore = base_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_DEFAULT_GITIGNORE)
        _git_run(["add", "-A"], base_dir, timeout=15)
        _git_run(
            _git_identity_args(base_dir) + ["commit", "-m", "gyrus: initial", "--quiet"],
            base_dir, timeout=15,
        )
        return True, "initialized local repo (add remote via `gyrus init`)"
    if not _git_remote_url(base_dir):
        return False, "no remote — run `gyrus init` to configure GitHub sync"
    ok_pull, pull_msg = _git_pull(base_dir)
    if not ok_pull:
        return False, f"pull failed: {pull_msg}"
    ok_push, push_msg = _git_commit_push(
        base_dir,
        f"gyrus doctor --fix · {datetime.now():%Y-%m-%d %H:%M}",
    )
    return ok_push, (f"pull: {pull_msg}; push: {push_msg}"
                     if ok_push else f"push failed: {push_msg}")


# Labels (from _doctor_check_*) that have a corresponding auto-fix.
_DOCTOR_FIXERS = {
    "lockfile":       lambda base: _doctor_fix_lockfile(),
    "schedule":       lambda base: _doctor_fix_schedule(),
    "dataless files": lambda base: _doctor_fix_dataless(base),
    "git sync":       lambda base: _doctor_fix_git_sync(base),
}


def run_doctor(base_dir, fix=False):
    """Run all diagnostic checks. If fix=True, attempt safe auto-fixes inline."""
    print()
    print("─" * 64)
    title = "🩺 gyrus doctor" + ("  (--fix enabled)" if fix else "")
    print(f"  {title}  —  {base_dir}")
    print("─" * 64)

    checks = [
        _doctor_check_storage(base_dir),
        _doctor_check_dataless(base_dir),
        _doctor_check_freshness(base_dir),
        _doctor_check_schedule(),
        _doctor_check_git_sync(base_dir),
        _doctor_check_env(base_dir),
        _doctor_check_sources(),
        _doctor_check_backlog(base_dir),
        _doctor_check_lockfile(),
    ]

    icons = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
    fixes_applied = 0
    fixes_failed = 0
    for status, label, msg, hint in checks:
        print(f"  {icons[status]} {label:18s}  {msg}")
        if hint:
            for line in hint.splitlines():
                print(f"       {line}")
        if fix and status != "ok" and label in _DOCTOR_FIXERS:
            print(f"       [--fix] attempting…")
            ok, result = _DOCTOR_FIXERS[label](base_dir)
            mark = "✓" if ok else "✗"
            print(f"       [--fix] {mark} {result}")
            if ok:
                fixes_applied += 1
            else:
                fixes_failed += 1

    print()
    fails = sum(1 for c in checks if c[0] == "fail")
    warns = sum(1 for c in checks if c[0] == "warn")
    if fails == 0 and warns == 0:
        print("  ✨ all checks passed")
    else:
        print(f"  summary: {fails} critical, {warns} warnings")
        if fix:
            print(f"  fixes:   {fixes_applied} applied, {fixes_failed} couldn't run")
            print(f"  → re-run `gyrus doctor` to verify")
        else:
            # Most-likely-cause heuristic
            if any(c[1] == "dataless files" and c[0] == "fail" for c in checks):
                print("  → dataless files are the most common cause of silent failures.")
                print("    Every cron run that reads or appends to a dataless file hangs")
                print("    until macOS kills it. Run the suggested brctl download above.")
            elif any(c[1] == "schedule" and c[0] != "ok" for c in checks):
                print("  → no scheduled job means gyrus isn't being run automatically.")
            print("  → try `gyrus doctor --fix` to auto-patch what's safe.")
    print()
    return 0 if fails == 0 else 1


# ─── Setup wizard: `gyrus init` ───────────────────────────────────────────

_DEFAULT_GITIGNORE = """\
# secrets
.env

# python
__pycache__/
*.pyc

# gyrus code (managed by `gyrus update`, not sync)
ingest.py
storage.py
storage_notion.py
eval_prompts.py
model-comparison.html

# per-machine
.ingest-state.json
ingest.log
latest-digest.md
"""


def _prompt(msg, default=""):
    """Read a line with an optional default. EOF-safe for non-tty."""
    try:
        resp = input(msg).strip()
    except EOFError:
        resp = ""
    return resp or default


def _prompt_yn(msg, default="y"):
    ans = _prompt(msg, default).lower()
    return ans.startswith("y")


def _which(cmd):
    import shutil
    return shutil.which(cmd)


def run_init(clone_url=None, location=None):
    """Interactive setup wizard. Painless by design: every step has a sensible
    default and is optional. Safe to re-run."""
    import subprocess
    import shutil as _shutil

    print()
    print("  🌱  gyrus setup")
    print()

    # ─── Step 1: storage location ──────────────────────────────
    if clone_url:
        return _init_clone(clone_url, location)

    default_loc = Path.home() / "gyrus-local"
    loc = Path(location) if location else Path(
        _prompt(f"  (1/4) Storage location  [{default_loc}]: ",
                str(default_loc))
    ).expanduser()

    provider = _detect_cloud_sync(loc)
    if provider:
        print(f"  ⚠️  that location is inside {provider} — not recommended.")
        print(f"      {provider} can lock or evict files and cause silent hangs.")
        print(f"      For cross-machine sync, gyrus sets up GitHub in step 3 —")
        print(f"      you don't need {provider} for that.")
        if not _prompt_yn("      Continue anyway? [y/N]: ", "n"):
            print("  Aborted.")
            return 1

    loc.mkdir(parents=True, exist_ok=True)
    (loc / "thoughts").mkdir(exist_ok=True)
    (loc / "projects").mkdir(exist_ok=True)

    # Copy code files from current install if we're moving
    src_dir = Path(__file__).resolve().parent
    if src_dir != loc:
        for fname in ("ingest.py", "storage.py", "storage_notion.py",
                      "eval_prompts.py"):
            src = src_dir / fname
            dst = loc / fname
            if src.exists() and not dst.exists():
                _shutil.copy2(src, dst)
        print(f"    ✓ copied gyrus code to {loc}")

    # Symlink ~/.gyrus
    gyrus_home = Path.home() / ".gyrus"
    if gyrus_home.is_symlink() or gyrus_home.exists():
        current_target = gyrus_home.resolve() if gyrus_home.is_symlink() else gyrus_home
        if current_target != loc:
            backup = Path.home() / f".gyrus.backup-{int(time.time())}"
            gyrus_home.rename(backup)
            print(f"    moved old ~/.gyrus → {backup.name}")
            gyrus_home.symlink_to(loc)
    else:
        gyrus_home.symlink_to(loc)
    print(f"    ✓ ~/.gyrus → {loc}")

    # ─── Step 2: API key ────────────────────────────────────────
    print()
    print("  (2/4) Anthropic API key")
    env_file = loc / ".env"
    existing_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                existing_key = line.split("=", 1)[1].strip().strip("\"'")
                break
    if existing_key:
        print(f"    ✓ found existing key ({existing_key[:10]}…)")
    else:
        print("    Get one at: https://console.anthropic.com/settings/keys")
        key = _prompt("    ANTHROPIC_API_KEY: ")
        if key:
            env_file.write_text(f"ANTHROPIC_API_KEY={key}\n")
            env_file.chmod(0o600)
            print(f"    ✓ saved to {env_file.name} (0600)")
        else:
            print("    ⚠️  skipped — add later to " + str(env_file))

    # ─── Step 3: GitHub sync ───────────────────────────────────
    print()
    print("  (3/4) GitHub sync (recommended for cross-machine)")
    if _git_remote_url(loc):
        print(f"    ✓ already configured ({_git_remote_url(loc)})")
    elif _prompt_yn("    Set up now? [Y/n]: ", "y"):
        _init_github_repo(loc)
    else:
        print("    skipped — you can run `gyrus init` again anytime")

    # ─── Step 4: schedule ──────────────────────────────────────
    print()
    print("  (4/4) Hourly schedule")
    if _has_gyrus_cron():
        print("    ✓ cron already configured")
    elif _prompt_yn("    Run `gyrus` every hour via cron? [Y/n]: ", "y"):
        _init_cron()

    # ─── Done ───────────────────────────────────────────────────
    print()
    print("  🎉  setup complete")
    print()
    print("     next:")
    print("       gyrus          # run first ingest")
    print("       gyrus doctor   # confirm health")
    if _git_remote_url(loc):
        print("       gyrus init --clone <url>   # on your other Macs")
    print()
    return 0


def _init_clone(clone_url, location=None):
    """Clone an existing knowledge-base repo onto a second machine."""
    import subprocess
    default_loc = Path.home() / "gyrus-local"
    loc = Path(location) if location else default_loc
    loc = loc.expanduser()

    provider = _detect_cloud_sync(loc)
    if provider:
        print(f"  ⚠️  target location is inside {provider} — not recommended.")
        print(f"      {provider} can lock or evict files and hang reads.")
        if not _prompt_yn("      Continue anyway? [y/N]: ", "n"):
            print("  Aborted.")
            return 1

    # Normalize URL
    if not clone_url.startswith(("http://", "https://", "git@", "ssh://")):
        if "/" in clone_url and not clone_url.startswith("github.com"):
            clone_url = "https://github.com/" + clone_url
        elif clone_url.startswith("github.com"):
            clone_url = "https://" + clone_url

    print(f"  cloning {clone_url} → {loc}")
    if loc.exists() and any(loc.iterdir()):
        print(f"  ⚠️  {loc} already exists and is non-empty")
        return 1
    r = subprocess.run(["git", "clone", clone_url, str(loc)], timeout=180)
    if r.returncode != 0:
        print("    ✗ clone failed")
        return 1
    print(f"    ✓ cloned")

    # Copy code from the current install if the repo doesn't have it
    src_dir = Path(__file__).resolve().parent
    import shutil as _shutil
    for fname in ("ingest.py", "storage.py", "storage_notion.py",
                  "eval_prompts.py"):
        src = src_dir / fname
        dst = loc / fname
        if src.exists() and not dst.exists():
            _shutil.copy2(src, dst)

    # Symlink ~/.gyrus
    gyrus_home = Path.home() / ".gyrus"
    if gyrus_home.is_symlink() or gyrus_home.exists():
        backup = Path.home() / f".gyrus.backup-{int(time.time())}"
        gyrus_home.rename(backup)
        print(f"    moved old ~/.gyrus → {backup.name}")
    gyrus_home.symlink_to(loc)
    print(f"    ✓ ~/.gyrus → {loc}")

    # API key
    print()
    env_file = loc / ".env"
    if not env_file.exists():
        key = os.environ.get("ANTHROPIC_API_KEY") or _prompt(
            "  Anthropic API key: ")
        if key:
            env_file.write_text(f"ANTHROPIC_API_KEY={key}\n")
            env_file.chmod(0o600)
            print("    ✓ saved .env")

    # Cron
    print()
    if not _has_gyrus_cron() and _prompt_yn(
        "  Run `gyrus` every hour via cron? [Y/n]: ", "y"
    ):
        _init_cron()

    print()
    print("  🎉  clone complete — your knowledge base is ready")
    print()
    return 0


def _init_github_repo(loc):
    """Create a private GitHub repo and wire up auto-sync."""
    import subprocess
    if not _which("gh"):
        print("    ⚠️  gh CLI not installed. Install: brew install gh")
        print("       then re-run: gyrus init")
        return
    auth = subprocess.run(["gh", "auth", "status"],
                          capture_output=True, text=True, timeout=10)
    if auth.returncode != 0:
        print("    ⚠️  gh not authenticated. Run: gh auth login")
        print("       then re-run: gyrus init")
        return

    # Init local repo if needed
    if not _git_is_repo(loc):
        _git_run(["init", "--initial-branch=main"], loc, timeout=10)
        gitignore = loc / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_DEFAULT_GITIGNORE)
        _git_run(["add", "-A"], loc, timeout=15)
        _git_run(
            _git_identity_args(loc) + ["commit", "-m", "gyrus: initial", "--quiet"],
            loc, timeout=15,
        )

    default_name = "gyrus-knowledge"
    name = _prompt(f"    Repo name [{default_name}]: ", default_name)
    r = subprocess.run(
        ["gh", "repo", "create", name, "--private",
         "--source", str(loc), "--remote", "origin", "--push"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode == 0:
        print(f"    ✓ created private repo + initial push")
        print(f"    ✓ auto-sync enabled (every run pulls & pushes)")
    else:
        msg = (r.stderr or "").strip().splitlines()
        tail = msg[-1] if msg else "unknown error"
        print(f"    ⚠️  gh repo create failed: {tail}")
        print(f"       you can set this up manually later")


def _has_gyrus_cron():
    """Check whether crontab already has a gyrus entry."""
    import subprocess
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True,
                           text=True, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    text = r.stdout or ""
    return "gyrus" in text or "ingest.py" in text


def _init_cron():
    """Add `0 * * * * gyrus` to the current user's crontab."""
    import subprocess
    gyrus_bin = _which("gyrus") or str(Path.home() / ".local" / "bin" / "gyrus")
    cron_line = f"0 * * * * {gyrus_bin} >/dev/null 2>&1"
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True,
                           text=True, timeout=5)
        existing = r.stdout if r.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError):
        print("    ⚠️  crontab not available")
        print(f"       add manually: {cron_line}")
        return
    new_cron = existing.rstrip() + "\n" + cron_line + "\n"
    new_cron = new_cron.lstrip("\n")
    try:
        p = subprocess.run(["crontab", "-"], input=new_cron,
                           text=True, timeout=10)
        if p.returncode == 0:
            print(f"    ✓ added to crontab (hourly)")
            return
    except subprocess.SubprocessError:
        pass
    print(f"    ⚠️  crontab install failed — add manually:")
    print(f"       {cron_line}")


def run_merge(store, slugs, yes=False):
    """Merge one or more source slugs into a target slug.

    `slugs` is a list where the LAST element is the target and the rest are
    sources. Rewrites aliases.json, rewrites canonical_project on every
    matching thought in the JSONL log, removes orphan project pages, and
    writes the updated status.md. Leaves regeneration of the target page
    to the next ingest run (or `gyrus --backfill`).

    Safe to run multiple times: it's idempotent per-source-slug.
    """
    if len(slugs) < 2:
        print("  usage: gyrus merge <from-slug> [<from-slug>...] <into-slug>")
        return 2

    into = slugs[-1]
    from_slugs = [s for s in slugs[:-1] if s != into]  # drop self-merges
    if not from_slugs:
        print(f"  nothing to merge (all source slugs equal '{into}')")
        return 0

    print()
    print(f"  🔀 Merge into '{into}':")
    for s in from_slugs:
        print(f"      ← {s}")

    # Count what's affected
    thoughts_dir = store.base_dir / "thoughts"
    projects_dir = store.base_dir / "projects"
    affected_thoughts = 0
    affected_alias_rows = 0
    affected_pages = []

    aliases = store.get_aliases()
    for a in aliases:
        if a.get("canonical_slug") in from_slugs:
            affected_alias_rows += 1

    if thoughts_dir.exists():
        for jsonl_file in thoughts_dir.glob("*.jsonl"):
            text = _read_text_safe(jsonl_file)
            if text is None:
                continue
            for line in text.splitlines():
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if t.get("canonical_project") in from_slugs:
                    affected_thoughts += 1

    for s in from_slugs:
        p = projects_dir / f"{s}.md"
        if p.exists():
            affected_pages.append(p)

    print(f"      {affected_alias_rows} alias row(s), "
          f"{affected_thoughts} thought(s), "
          f"{len(affected_pages)} orphan page(s)")

    if not yes and sys.stdin.isatty():
        ans = _prompt("\n  Proceed? [Y/n]: ", "y").lower()
        if not ans.startswith("y"):
            print("  Aborted.")
            return 1

    # 1. Rewrite aliases.json
    changed_aliases = 0
    for a in aliases:
        if a.get("canonical_slug") in from_slugs:
            a["canonical_slug"] = into
            changed_aliases += 1
    # Also add explicit alias rows so `from_slug` itself routes to `into`
    # on any future raw lookup (covers projects that never had their own row)
    existing_alias_names = {a["alias"].lower() for a in aliases}
    for s in from_slugs:
        if s.lower() not in existing_alias_names:
            aliases.append({"alias": s, "canonical_slug": into})
            changed_aliases += 1
    store.aliases_file.write_text(json.dumps(aliases, indent=2))
    print(f"    ✓ rewrote {changed_aliases} alias row(s)")

    # 2. Rewrite thoughts in JSONL files (in-place)
    rewritten_thoughts = 0
    if thoughts_dir.exists():
        for jsonl_file in sorted(thoughts_dir.glob("*.jsonl")):
            text = _read_text_safe(jsonl_file)
            if text is None:
                continue
            new_lines = []
            touched = False
            for line in text.splitlines():
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    new_lines.append(line)
                    continue
                if t.get("canonical_project") in from_slugs:
                    t["canonical_project"] = into
                    rewritten_thoughts += 1
                    touched = True
                new_lines.append(json.dumps(t))
            if touched:
                jsonl_file.write_text("\n".join(new_lines) + "\n")
    print(f"    ✓ rewrote {rewritten_thoughts} thought record(s)")

    # 3. Remove orphan project pages
    for p in affected_pages:
        try:
            p.unlink()
            print(f"    ✓ removed orphan page: projects/{p.name}")
        except OSError as e:
            print(f"    ⚠️  couldn't remove {p.name}: {e}")

    # 4. Regenerate status.md if possible (best-effort)
    try:
        pages = store.get_all_pages()
        if pages:
            # Rewrite status.md by dropping merged slugs — cheap approximation
            status_path = store.base_dir / "status.md"
            if status_path.exists():
                lines = status_path.read_text().splitlines()
                kept = []
                drops = 0
                for line in lines:
                    if any(f"**{s}**:" in line for s in from_slugs):
                        drops += 1
                        continue
                    kept.append(line)
                if drops:
                    status_path.write_text("\n".join(kept) + "\n")
                    print(f"    ✓ removed {drops} row(s) from status.md")
    except OSError:
        pass

    print()
    print(f"  → run `gyrus --backfill` to regenerate projects/{into}.md")
    print(f"    from the merged thoughts, or just wait for the next `gyrus` run.")
    return 0


def run_sync(base_dir):
    """Manual sync: pull from origin, then commit+push any local changes."""
    if not _git_is_repo(base_dir):
        print("  no git repo — run `gyrus init` to set up GitHub sync")
        return 1
    remote = _git_remote_url(base_dir)
    if not remote:
        print("  no origin remote — run `gyrus init` to set up GitHub sync")
        return 1
    print(f"  remote: {remote}")
    print("  pulling…")
    ok, msg = _git_pull(base_dir)
    print(f"    {'✓' if ok else '✗'} {msg}")
    print("  pushing…")
    ok, msg = _git_commit_push(
        base_dir,
        f"gyrus sync · {datetime.now():%Y-%m-%d %H:%M} · manual",
    )
    print(f"    {'✓' if ok else '✗'} {msg}")
    return 0 if ok else 1


def review_project_status(store):
    """Interactive CLI to review and set project statuses. Writes to status.md."""
    pages = store.get_all_pages()
    if not pages:
        return

    recency = _get_project_recency(store)
    today = datetime.now().date()
    overrides = _parse_status_overrides(store)

    print(f"\n  Review project statuses ({len(pages)} projects)")
    print(f"  For each project, confirm or change the status.")
    print(f"  Options: [Enter]=keep, a=active, k=killed, d=dormant, p=paused, b=brainstorm\n")

    updated = {}
    for p in sorted(pages, key=lambda x: x["slug"]):
        slug = p["slug"]
        # Skip special pages
        if slug in ("ideas", "me", "cross-cutting"):
            continue

        # Detect current status from page content
        content = p["content"]
        detected_status = "unknown"
        if "## Status" in content:
            start = content.find("## Status") + len("## Status")
            end = content.find("\n##", start)
            status_line = content[start:end].strip() if end > 0 else content[start:start + 120].strip()
            first_word = status_line.split("|")[0].strip().split()[0].lower() if status_line else "unknown"
            if first_word in ("active", "killed", "dormant", "paused"):
                detected_status = first_word
        else:
            status_line = ""

        # Use override if exists
        if slug in overrides:
            detected_status = overrides[slug]

        # Recency signal
        last_date = recency.get(slug, "unknown")
        if last_date != "unknown":
            try:
                days_ago = (today - datetime.fromisoformat(last_date).date()).days
                if days_ago > 60 and detected_status == "active":
                    detected_status = "dormant"  # suggest dormant if >60 days
                recency_str = f"{days_ago}d ago"
            except (ValueError, TypeError):
                recency_str = last_date
        else:
            recency_str = "?"

        # Status color hint
        indicator = {"active": "🟢", "killed": "🔴", "dormant": "🟡", "paused": "⏸️", "brainstorm": "💡", "unknown": "❓"}.get(detected_status, "❓")

        try:
            choice = input(f"  {indicator} {slug} [{detected_status}] (last: {recency_str}): ").strip().lower()
        except EOFError:
            choice = ""

        if choice == "a":
            updated[slug] = "active"
        elif choice == "k":
            updated[slug] = "killed"
        elif choice == "d":
            updated[slug] = "dormant"
        elif choice == "p":
            updated[slug] = "paused"
        elif choice == "b":
            updated[slug] = "brainstorm"
        elif choice:
            updated[slug] = choice  # custom status
        else:
            updated[slug] = detected_status  # keep current

    # Write status.md as editable file
    _write_status_md(store, pages, updated, recency)
    print(f"\n  ✓ Saved to status.md — edit anytime to change project statuses")
    return updated


def generate_status(store):
    """Generate status.md from all knowledge pages, respecting user overrides."""
    pages = store.get_all_pages()
    recency = _get_project_recency(store)
    overrides = _parse_status_overrides(store)

    # Apply recency-based status detection
    today = datetime.now().date()
    statuses = {}
    for p in pages:
        slug = p["slug"]
        if slug in overrides:
            statuses[slug] = overrides[slug]
            continue
        # Detect from page content
        content = p["content"]
        detected = "unknown"
        if "## Status" in content:
            start = content.find("## Status") + len("## Status")
            end = content.find("\n##", start)
            status_line = content[start:end].strip() if end > 0 else ""
            first_word = status_line.split("|")[0].strip().split()[0].lower() if status_line else "unknown"
            if first_word in ("active", "killed", "dormant", "paused"):
                detected = first_word
        # Apply recency rule
        last_date = recency.get(slug, "")
        if last_date:
            try:
                days_ago = (today - datetime.fromisoformat(last_date).date()).days
                if days_ago > 60 and detected == "active":
                    detected = "dormant"
            except (ValueError, TypeError):
                pass
        statuses[slug] = detected

    _write_status_md(store, pages, statuses, recency)


def _write_status_md(store, pages, statuses, recency):
    """Write status.md in a user-editable format."""
    lines = [
        "# Gyrus — Project Status",
        "",
        f"_Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        "_Edit this file to change project statuses. Gyrus reads it on each run._",
        "_Valid statuses: active, killed, dormant, paused, brainstorm_",
        "",
    ]

    # Group by status
    by_status = defaultdict(list)
    for p in pages:
        slug = p["slug"]
        if slug in ("ideas", "me"):
            continue
        st = statuses.get(slug, "unknown")
        by_status[st].append(slug)

    status_order = ["active", "paused", "dormant", "brainstorm", "killed", "unknown"]
    status_emoji = {"active": "🟢", "killed": "🔴", "dormant": "🟡", "paused": "⏸️", "brainstorm": "💡", "unknown": "❓"}

    for st in status_order:
        slugs = by_status.get(st, [])
        if not slugs:
            continue
        lines.append(f"## {status_emoji.get(st, '')} {st.title()} ({len(slugs)})")
        lines.append("")
        for slug in sorted(slugs):
            last = recency.get(slug, "?")
            lines.append(f"- **{slug}**: {st} | last: {last}")
        lines.append("")

    store.write_status("\n".join(lines) + "\n")

    # Cross-cutting thoughts
    thoughts = store.get_thoughts(canonical_project=None, skipped=False, limit=200)
    # Filter to thoughts with no project (cross-cutting)
    cross_cutting = [t for t in thoughts if not t.get("canonical_project")]
    if cross_cutting:
        cc_lines = ["# Cross-Cutting Thoughts\n"]
        cc_lines.append(f"_{len(cross_cutting)} thoughts not tied to a specific project_\n")
        for t in cross_cutting:
            tags = ", ".join(t.get("tags", []))
            line = f"- [{t.get('source', '?')}] {t['content']}"
            if tags:
                line += f"  `{tags}`"
            cc_lines.append(line)
        store.write_cross_cutting("\n".join(cc_lines) + "\n")


# ─── Daily Digest ───


def _save_run_log(store, sessions, thoughts, cost):
    """Append a structured entry to the run log."""
    base = store.base_dir if hasattr(store, "base_dir") else Path.home() / ".gyrus"
    log_path = base / "runs.jsonl"

    # Count by tool
    by_tool = defaultdict(int)
    for s in sessions:
        by_tool[s["type"]] += 1

    # Count by project
    by_project = defaultdict(int)
    change_summaries = {}
    for t in thoughts:
        cp = t.get("canonical_project") or t.get("merged_into_page") or "uncategorized"
        by_project[cp] += 1

    # Read change summaries from pages (if available)
    for p in store.get_all_pages():
        content = p.get("content", "")
        if "CHANGE_SUMMARY:" in content:
            summary = content.split("CHANGE_SUMMARY:")[-1].strip().split("\n")[0]
            if summary:
                change_summaries[p["slug"]] = summary

    entry = {
        "timestamp": datetime.now().isoformat(),
        "machine": _MACHINE,
        "sessions": len(sessions),
        "thoughts": len(thoughts),
        "cost": round(cost, 3),
        "by_tool": dict(by_tool),
        "by_project": dict(by_project),
        "pages_updated": list(by_project.keys()),
        "extract_model": _config.get("extract_model", ""),
        "merge_model": _config.get("merge_model", ""),
    }

    with open(log_path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def show_run_log(base_dir, n=10):
    """Display recent run history."""
    log_path = Path(base_dir) / "runs.jsonl"
    if not log_path.exists():
        print("  No run history yet. Run 'gyrus' to start ingestion.")
        return

    entries = []
    for line in log_path.read_text().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        print("  No run history.")
        return

    recent = entries[-n:]
    print(f"\n  Last {len(recent)} runs:\n")
    print(f"  {'Date':<20} {'Machine':<15} {'Sessions':>8} {'Thoughts':>8} {'Cost':>8} {'Projects Updated'}")
    print(f"  {'─'*20} {'─'*15} {'─'*8} {'─'*8} {'─'*8} {'─'*30}")

    for e in recent:
        ts = e.get("timestamp", "")[:16].replace("T", " ")
        machine = e.get("machine", "?")[:14]
        sessions = e.get("sessions", 0)
        thoughts = e.get("thoughts", 0)
        cost = e.get("cost", 0)
        projects = e.get("pages_updated", [])

        if sessions == 0 and thoughts == 0:
            detail = "(no new sessions)"
        else:
            detail = ", ".join(projects[:5])
            if len(projects) > 5:
                detail += f" +{len(projects)-5} more"

        print(f"  {ts:<20} {machine:<15} {sessions:>8} {thoughts:>8} ${cost:>7.3f} {detail}")

    # Total cost
    total = sum(e.get("cost", 0) for e in entries)
    print(f"\n  Total cost (all runs): ${total:.2f}")


def sync_tool_context(store):
    """Write Gyrus read instructions to AI tool instruction files.

    Instead of copying project content (which gets stale), this tells
    each tool WHERE to read. The tool reads fresh data every session.
    Only writes once — skips if already configured.
    """
    gyrus_dir = store.base_dir if hasattr(store, "base_dir") else Path.home() / ".gyrus"

    pointer = (
        "# Gyrus Knowledge Base\n"
        "\n"
        "You have access to a knowledge base built from AI coding sessions.\n"
        f"Read the project page before starting work on any project.\n"
        "\n"
        f"  ls {gyrus_dir}/projects/              # list all projects\n"
        f"  cat {gyrus_dir}/projects/PROJECT.md    # read a project page\n"
        f"  cat {gyrus_dir}/status.md              # project statuses\n"
        f"  cat {gyrus_dir}/me.md                  # working patterns\n"
        f"  grep -ri \"SEARCH\" {gyrus_dir}/         # search everything\n"
    )

    marker = "# Gyrus Knowledge Base"
    targets = {}

    # Antigravity
    gemini_md = Path.home() / ".gemini" / "GEMINI.md"
    if gemini_md.parent.exists():
        targets["Antigravity (GEMINI.md)"] = gemini_md

    for label, path in targets.items():
        existing = path.read_text() if path.exists() else ""
        if marker in existing:
            continue  # already configured
        new_content = existing.rstrip() + "\n\n" + pointer if existing.strip() else pointer
        path.write_text(new_content + "\n")
        print(f"  ✓ {label}: added Gyrus read instructions")


def generate_digest(batch_thoughts, store, sessions):
    """Generate a daily digest summarizing what changed across projects."""
    today = datetime.now().strftime("%Y-%m-%d")

    # Group thoughts by project
    by_project = defaultdict(list)
    for t in batch_thoughts:
        cp = t.get("canonical_project") or t.get("merged_into_page") or "uncategorized"
        by_project[cp].append(t)

    # Group sessions by tool
    by_tool = defaultdict(int)
    for s in sessions:
        by_tool[s["type"]] += 1

    lines = [
        f"# Gyrus Daily Digest — {today}",
        "",
        f"**{len(sessions)} sessions processed** across "
        f"{', '.join(f'{v} {k}' for k, v in sorted(by_tool.items()))}",
        f"**{len(batch_thoughts)} thoughts extracted** across "
        f"**{len(by_project)} projects**",
        "",
    ]

    # Per-project summaries
    for project in sorted(by_project.keys(), key=lambda p: -len(by_project[p])):
        thoughts = by_project[project]
        tools = set(t.get("source", "?") for t in thoughts)
        lines.append(f"## {project} ({len(thoughts)} thoughts)")
        lines.append(f"_Sources: {', '.join(sorted(tools))}_")
        lines.append("")

        # Categorize thoughts
        decisions = [t for t in thoughts if "decision" in (t.get("tags") or [])]
        statuses = [t for t in thoughts if "status" in (t.get("tags") or [])]
        others = [t for t in thoughts if t not in decisions and t not in statuses]

        if decisions:
            lines.append("**Decisions:**")
            for t in decisions[:5]:
                lines.append(f"- {t.get('content', '')[:150]}")
            lines.append("")

        if statuses:
            lines.append("**Status changes:**")
            for t in statuses[:3]:
                lines.append(f"- {t.get('content', '')[:150]}")
            lines.append("")

        if others and not decisions and not statuses:
            # Show first few if no decisions/status
            for t in others[:3]:
                lines.append(f"- {t.get('content', '')[:150]}")
            lines.append("")

    return "\n".join(lines) + "\n"


def send_digest_email(digest, digest_config, base_dir):
    """Send digest via Resend API or SMTP."""
    provider = digest_config.get("provider", "resend")
    to_email = digest_config.get("email", "")
    if not to_email:
        return

    if provider == "resend":
        api_key = digest_config.get("resend_api_key") or os.environ.get("RESEND_API_KEY")
        if not api_key:
            print("  Digest email skipped: no RESEND_API_KEY")
            return
        from_email = digest_config.get("from_email", "digest@gyrus.sh")
        _send_resend(api_key, from_email, to_email, digest)
    elif provider == "smtp":
        _send_smtp(digest_config, to_email, digest)
    else:
        print(f"  Unknown digest provider: {provider}")


def _send_resend(api_key, from_email, to_email, digest):
    """Send email via Resend API."""
    from urllib.request import Request, urlopen
    today = datetime.now().strftime("%Y-%m-%d")

    # Convert markdown to simple HTML
    html = "<div style='font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto;'>"
    for line in digest.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            html += f"<h1 style='color: #7c3aed; font-size: 20px;'>{line[2:]}</h1>"
        elif line.startswith("## "):
            html += f"<h2 style='color: #333; font-size: 16px; margin-top: 20px; border-bottom: 1px solid #eee; padding-bottom: 4px;'>{line[3:]}</h2>"
        elif line.startswith("**") and line.endswith("**"):
            html += f"<p style='font-weight: 600; color: #333; margin: 8px 0 4px;'>{line.strip('*')}</p>"
        elif line.startswith("- "):
            html += f"<li style='color: #555; font-size: 14px; margin: 2px 0;'>{line[2:]}</li>"
        elif line.startswith("_") and line.endswith("_"):
            html += f"<p style='color: #999; font-size: 12px; font-style: italic;'>{line.strip('_')}</p>"
        elif line.startswith("**"):
            html += f"<p style='color: #333; font-size: 14px;'>{line}</p>"
        elif line:
            html += f"<p style='color: #555; font-size: 14px;'>{line}</p>"
    html += "<hr style='margin-top: 20px; border: none; border-top: 1px solid #eee;'>"
    html += "<p style='color: #aaa; font-size: 11px;'>Sent by <a href='https://gyrus.sh' style='color: #7c3aed;'>Gyrus</a></p>"
    html += "</div>"

    body = json.dumps({
        "from": from_email,
        "to": [to_email],
        "subject": f"Gyrus Digest — {today}",
        "html": html,
    }).encode()

    req = Request(
        "https://api.resend.com/emails",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urlopen(req, timeout=10) as resp:
            print(f"  ✓ Digest emailed to {to_email}")
    except Exception as e:
        print(f"  Digest email failed: {e}")


def _send_smtp(config, to_email, digest):
    """Send email via SMTP (Gmail etc)."""
    import smtplib
    from email.mime.text import MIMEText

    smtp_host = config.get("smtp_host", "smtp.gmail.com")
    smtp_port = config.get("smtp_port", 587)
    smtp_user = config.get("smtp_user", "")
    smtp_pass = config.get("smtp_pass") or os.environ.get("SMTP_PASSWORD", "")

    if not smtp_user or not smtp_pass:
        print("  Digest email skipped: no SMTP credentials")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    msg = MIMEText(digest)
    msg["Subject"] = f"Gyrus Digest — {today}"
    msg["From"] = smtp_user
    msg["To"] = to_email

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"  ✓ Digest emailed to {to_email}")
    except Exception as e:
        print(f"  Digest email failed: {e}")


# ─── Main ───


def _load_config(store):
    """Load config.json from the Gyrus base directory."""
    base = store.base_dir if hasattr(store, 'base_dir') else Path.home() / ".gyrus"
    config_path = base / "config.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def self_update(base_dir=None):
    """Download the latest Gyrus scripts from GitHub."""
    import urllib.request
    base = Path(base_dir) if base_dir else Path.home() / ".gyrus"
    repo_url = "https://raw.githubusercontent.com/prismindanalytics/gyrus/main"
    files = {
        "ingest.py": base / "ingest.py",
        "storage.py": base / "storage.py",
        "storage_notion.py": base / "storage_notion.py",
        "eval_prompts.py": base / "eval_prompts.py",
        "skills/codex/gyrus-instructions.md": base / "skills" / "codex" / "gyrus-instructions.md",
    }

    claude_cmd_dir = Path.home() / ".claude" / "commands"
    if claude_cmd_dir.parent.exists():
        files["skills/claude-code/gyrus.md"] = claude_cmd_dir / "gyrus.md"

    # Check for new version first
    try:
        req = urllib.request.Request(f"{repo_url}/ingest.py")
        with urllib.request.urlopen(req, timeout=10) as resp:
            remote_content = resp.read().decode("utf-8")
    except Exception as e:
        print(f"  Could not reach GitHub: {e}")
        return False

    # Extract remote version
    remote_version = None
    for line in remote_content.split("\n")[:20]:
        if line.startswith("__version__"):
            remote_version = line.split("=")[1].strip().strip('"').strip("'")
            break

    if remote_version and remote_version == __version__:
        print(f"  Already up to date (v{__version__})")
        return True

    print(f"  Updating: v{__version__} -> v{remote_version or 'latest'}")

    # Write ingest.py (already downloaded)
    target = files["ingest.py"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(remote_content)
    print(f"  Updated {target}")

    # Download remaining files
    for fname, target in list(files.items())[1:]:
        try:
            req = urllib.request.Request(f"{repo_url}/{fname}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read().decode("utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            print(f"  Updated {target}")
        except Exception as e:
            print(f"  Warning: could not update {fname}: {e}")

    print(f"  Done! Updated to v{remote_version or 'latest'}")
    return True


def compare_models(keys, base_dir, file_config=None):
    """Run extraction benchmark across available models and generate HTML comparison."""
    import webbrowser
    from concurrent.futures import ThreadPoolExecutor, as_completed

    file_config = file_config or {}
    store = MarkdownStorage(base_dir=str(base_dir))
    state = store.load_state()

    # Determine available models per provider
    available = []
    if keys.get("anthropic"):
        available += ["haiku", "sonnet"]
    if keys.get("openai"):
        available += ["gpt-4.1-nano", "gpt-4.1-mini", "gpt-5.4-nano", "gpt-5.4-mini"]
    if keys.get("google"):
        available += ["gemini-lite", "gemini-flash"]

    if not available:
        print("  No API keys provided. Cannot compare models.")
        return None

    display_names = [_display_name(m) for m in available]
    print(f"  Models to test: {', '.join(display_names)}")
    print(f"  (This will make ~{len(available) * 3} API calls, estimated cost ~$0.50-1.00)")

    # Pick 3 diverse sessions
    all_sessions = (
        find_claude_code_sessions(state) +
        find_cowork_sessions(state) +
        find_antigravity_sessions(state) +
        find_codex_sessions(state)
    )
    if not all_sessions:
        print("  No sessions found to test with.")
        return None

    # Pre-extract text to filter by actual content length (not file size)
    EXTRACTORS_LOCAL = {
        "claude-code": lambda s: extract_claude_code_conversation(s["path"]),
        "cowork": lambda s: extract_cowork_conversation(s["path"], s.get("output_dir")),
        "antigravity": lambda s: extract_antigravity_session(s["path"]),
        "codex": lambda s: extract_codex_conversation(s["path"]),
        "cursor": lambda s: extract_cursor_conversation(s["path"]),
        "copilot": lambda s: extract_copilot_conversation(s["path"]),
    }
    for s in all_sessions:
        fn = EXTRACTORS_LOCAL.get(s["type"])
        try:
            s["_text_len"] = len(fn(s)) if fn else 0
        except Exception:
            s["_text_len"] = 0

    all_sessions.sort(key=lambda s: s["_text_len"])
    # Need at least 500 chars of real conversation content
    viable = [s for s in all_sessions if s["_text_len"] > 500]
    if not viable:
        viable = all_sessions

    # Pick from the upper half (meatier sessions give better comparison)
    upper_half = viable[len(viable)//2:]
    picked = []
    seen_tools = set()
    for s in reversed(upper_half):  # largest first
        if s["type"] not in seen_tools and len(picked) < 3:
            seen_tools.add(s["type"])
            picked.append(s)
    remaining = [s for s in upper_half if s not in picked]
    if remaining and len(picked) < 3:
        step = max(1, len(remaining) // (3 - len(picked) + 1))
        for i in range(0, len(remaining), step):
            if len(picked) >= 3:
                break
            picked.append(remaining[i])
    sessions = picked[:3]

    # Extract text once
    EXTRACTORS = {
        "claude-code": lambda s: extract_claude_code_conversation(s["path"]),
        "cowork": lambda s: extract_cowork_conversation(s["path"], s.get("output_dir")),
        "antigravity": lambda s: extract_antigravity_session(s["path"]),
        "codex": lambda s: extract_codex_conversation(s["path"]),
        "cursor": lambda s: extract_cursor_conversation(s["path"]),
        "copilot": lambda s: extract_copilot_conversation(s["path"]),
        "cline": lambda s: extract_cline_conversation(s["path"]),
        "continue": lambda s: extract_continue_conversation(s["path"]),
        "aider": lambda s: extract_aider_conversation(s["path"]),
        "opencode": lambda s: extract_opencode_conversation(s["path"]),
    }

    texts = []
    for s in sessions:
        fn = EXTRACTORS.get(s["type"])
        t = fn(s) if fn else ""
        texts.append(t)
        ws = s.get("workspace", "")
        label = f"{s['type']}: {ws or s['session_id'][:20]}"
        print(f"    {label} ({len(t)//1024}KB)")
    print()

    # Set up global config for LLM calls
    _config["keys"] = {k: v for k, v in keys.items() if v}

    # Run all model × session combinations in parallel
    results = {}  # model -> [{"session_idx": i, "thoughts": [...], "time": t, "cost": c}]
    tasks = []

    def _run_one(model_name, session_idx, text, workspace):
        ws_header = ""
        if workspace:
            mapped = workspace
            rg = file_config.get("repo_groups", {})
            if workspace in rg:
                mapped = rg[workspace]
            ws_header = f"WORKSPACE: {workspace}"
            if mapped != workspace:
                ws_header += f" (this repo is part of the '{mapped}' product)"
            ws_header += "\n\n"
        prompt = EXTRACTION_PROMPT + ws_header + "CONVERSATION:\n" + text
        t0 = time.time()
        try:
            raw = call_llm(prompt, role="extract", max_tokens=4096, model_override=model_name)
            elapsed = time.time() - t0
            raw = _strip_json_fences(raw)
            thoughts = json.loads(raw.strip())
        except Exception:
            elapsed = time.time() - t0
            thoughts = []
        cost = _COST_PER_CALL.get(model_name, 0.01)
        return model_name, session_idx, thoughts, elapsed, cost

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for model_name in available:
            for i, (s, text) in enumerate(zip(sessions, texts)):
                if len(text) < 100:
                    continue
                ws = s.get("workspace", "")
                futures.append(executor.submit(_run_one, model_name, i, text, ws))

        for future in as_completed(futures):
            model_name, sidx, thoughts, elapsed, cost = future.result()
            if model_name not in results:
                results[model_name] = []
            results[model_name].append({
                "session_idx": sidx, "thoughts": thoughts,
                "time": elapsed, "cost": cost,
            })

    # Print progress summary
    for model_name in available:
        entries = results.get(model_name, [])
        total_thoughts = sum(len(e["thoughts"]) for e in entries)
        total_time = sum(e["time"] for e in entries)
        total_cost = sum(e["cost"] for e in entries)
        print(f"    {model_name}: {total_thoughts} thoughts, {total_time:.1f}s, ~${total_cost:.3f}")

    # ── Generate sample wiki pages per model ──
    # For each model, merge all thoughts into one sample page using the merge model
    print("\n  Generating sample wiki pages...")
    wiki_pages = {}  # model_name -> wiki page markdown string

    def _merge_for_model(model_name):
        entries = results.get(model_name, [])
        all_thoughts = []
        for e in entries:
            for t in e["thoughts"]:
                if isinstance(t, dict):
                    all_thoughts.append(t)
        if not all_thoughts:
            return model_name, ""
        # Group by project, pick largest cluster
        by_project = defaultdict(list)
        for t in all_thoughts:
            proj = t.get("project") or "unknown"
            by_project[proj].append(t)
        biggest = max(by_project.items(), key=lambda x: len(x[1]))
        proj_name, proj_thoughts = biggest
        # Format thoughts for merge prompt
        thought_strs = []
        for t in proj_thoughts:
            thought_strs.append(
                f"- [{t.get('kind', 'project')}] {t.get('content', '')}"
            )
        new_thoughts_text = "\n".join(thought_strs)
        # Create empty page template
        empty_page = f"# {proj_name}\n\n## Status\nunknown\n\n## Overview\n\n## Architecture & Technical Stack\n\n## Key Decisions\n\n## Timeline & History\n"
        prompt = MERGE_PROMPT.format(
            page_content=empty_page, new_thoughts=new_thoughts_text
        )
        try:
            # Use the configured merge model (thread-safe via model_override)
            merge = _config.get("merge_model", "sonnet")
            page = call_llm(prompt, role="merge", max_tokens=4096, model_override=merge)
            return model_name, page
        except Exception as e:
            return model_name, f"Error generating page: {e}"

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_merge_for_model, m): m for m in available}
        for future in as_completed(futures):
            model_name, page = future.result()
            wiki_pages[model_name] = page
            if page:
                print(f"    {model_name}: {len(page)} chars wiki page")
            else:
                print(f"    {model_name}: no thoughts to merge")

    # ── Grade each model's output using the strongest available model ──
    # Pick the best judge: sonnet > gpt-4.1 > gpt-4.1-mini > haiku > gemini-pro
    # Best judges: frontier models from each provider
    judge_priority = ["opus", "gpt-5.4", "gemini-pro", "sonnet", "gpt-4.1", "gpt-4.1-mini", "haiku"]
    judge_model = None
    for jp in judge_priority:
        resolved = _resolve_model(jp)
        if resolved["provider"] in _config["keys"]:
            judge_model = jp
            break

    grades = {}  # model -> {"score": 1-10, "summary": "...", "strengths": "...", "weaknesses": "..."}
    if judge_model and wiki_pages:
        print(f"\n  Grading results with {judge_model}...")
        # Use judge_model via model_override (thread-safe)

        # Build the grading prompt
        models_with_pages = [m for m in available if wiki_pages.get(m)]
        if models_with_pages:
            pages_text = ""
            for m in models_with_pages:
                page = wiki_pages[m]
                # Truncate to 2000 chars per model to fit in context
                pages_text += f"\n\n--- MODEL: {m} ---\n{page[:2000]}\n"

            grade_prompt = f"""You are a strict evaluator of wiki pages generated from AI coding session extractions.
Each page below was produced by a different extraction model. Your job is to grade quality based ONLY on what you see in each page.

CRITICAL: Do NOT invent, assume, or reference any information not present in the pages below. If a page mentions "GPT-5.2" or any specific detail, only mark it as accurate if it's plausible given the context. Flag anything that looks hallucinated.

Grade each model's wiki page on these criteria:
1. **Strategic Value** (1-10): Does it capture decisions and insights that matter weeks later? Or technical noise?
2. **Accuracy** (1-10): Does the content appear grounded in real decisions, or does it contain invented/hallucinated details?
3. **Completeness** (1-10): How thorough is the coverage?
4. **Signal-to-Noise** (1-10): Ratio of strategic insights to implementation filler.

Output a JSON object:
{{
  "model-name": {{
    "overall": 8,
    "strategic_value": 8,
    "accuracy": 9,
    "completeness": 7,
    "signal_to_noise": 8,
    "summary": "One factual sentence about this page's quality",
    "recommendation": "Best for X use case"
  }}
}}

PAGES TO GRADE:
{pages_text}

Output ONLY the JSON object."""

            try:
                raw = call_llm(grade_prompt, role="merge", max_tokens=2048, model_override=judge_model)
                raw = _strip_json_fences(raw)
                grades = json.loads(raw)
                for m, g in grades.items():
                    score = g.get("overall", "?")
                    summary = g.get("summary", "")[:60]
                    print(f"    {m}: {score}/10 — {summary}")
            except Exception as e:
                print(f"    Grading error: {e}")

    # Find best model: highest quality first, then cheapest among ties
    best_model = available[0]
    if grades:
        ranked = sorted(
            [(m, grades.get(m, {}).get("overall", 0),
              sum(e["cost"] for e in results.get(m, [])))
             for m in available],
            key=lambda x: (-x[1], x[2])  # highest score first, lowest cost as tiebreaker
        )
        best_model = ranked[0][0]

    # Generate HTML
    html_path = base_dir / "model-comparison.html"
    _generate_comparison_html(results, sessions, texts, available, html_path,
                              wiki_pages=wiki_pages, grades=grades,
                              recommended=best_model)
    print(f"\n  Comparison page: file://{html_path}")

    # Open in browser
    try:
        webbrowser.open(f"file://{html_path}")
    except Exception:
        pass

    # Print recommendation
    if grades.get(best_model):
        g = grades[best_model]
        print(f"\n  {'='*50}")
        print(f"  Recommended: {best_model} (score: {g.get('overall', '?')}/10)")
        print(f"  {g.get('summary', '')}")
        print(f"  {g.get('recommendation', '')}")
        print(f"  {'='*50}")

    # Find default index (the recommended model)
    best_idx = 0
    for i, m in enumerate(available):
        if m == best_model:
            best_idx = i
            break

    # Prompt for extraction model selection
    print("\n  EXTRACTION MODEL:")
    for i, model_name in enumerate(available, 1):
        entries = results.get(model_name, [])
        total = sum(len(e["thoughts"]) for e in entries)
        cost = sum(e["cost"] for e in entries)
        g = grades.get(model_name, {})
        score = g.get("overall", "")
        score_str = f" [{score}/10]" if score else ""
        rec = " ★" if model_name == best_model else ""
        print(f"  [{i}] {_display_name(model_name)}: {total} thoughts, ~${cost:.3f}/run{score_str}{rec}")

    try:
        choice = input(f"\n  Pick extraction model [{best_idx + 1}]: ").strip()
        idx = int(choice) - 1 if choice else best_idx
        if 0 <= idx < len(available):
            extract_chosen = available[idx]
        else:
            extract_chosen = available[best_idx]
    except (ValueError, EOFError):
        extract_chosen = available[best_idx]

    # Prompt for merge model selection
    merge_options = ["sonnet", "gpt-4.1", "gpt-5.4", "gemini-pro"]
    merge_available = [m for m in merge_options if _resolve_model(m)["provider"] in _config["keys"]]
    if not merge_available:
        merge_available = [extract_chosen]  # fallback to same model

    print("\n  MERGE MODEL (for wiki page generation):")
    for i, model_name in enumerate(merge_available, 1):
        rec = " ★" if i == 1 else ""
        print(f"  [{i}] {_display_name(model_name)}{rec}")

    try:
        choice = input(f"\n  Pick merge model [1]: ").strip()
        idx = int(choice) - 1 if choice else 0
        if 0 <= idx < len(merge_available):
            merge_chosen = merge_available[idx]
        else:
            merge_chosen = merge_available[0]
    except (ValueError, EOFError):
        merge_chosen = merge_available[0]

    # Write to config
    config_path = base_dir / "config.json"
    cfg = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    cfg["extract_model"] = extract_chosen
    cfg["merge_model"] = merge_chosen
    config_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"\n  ✓ Updated config.json:")
    print(f"    extract_model = {_display_name(extract_chosen)} ({extract_chosen})")
    print(f"    merge_model   = {_display_name(merge_chosen)} ({merge_chosen})")
    return extract_chosen


def _generate_comparison_html(results, sessions, texts, model_order, output_path,
                              wiki_pages=None, grades=None, recommended=None):
    """Generate a self-contained HTML comparison page."""
    import html as html_lib

    # Build data for template
    session_labels = []
    for s in sessions:
        ws = s.get("workspace", "")
        label = f"{s['type']}: {ws or s['session_id'][:20]}"
        session_labels.append(label)

    # Summary stats per model
    summaries = []
    for model_name in model_order:
        entries = results.get(model_name, [])
        total_thoughts = sum(len(e["thoughts"]) for e in entries)
        total_time = sum(e["time"] for e in entries)
        total_cost = sum(e["cost"] for e in entries)
        summaries.append({
            "model": model_name,
            "thoughts": total_thoughts,
            "time": round(total_time, 1),
            "cost": round(total_cost, 3),
        })

    # Add grades to summaries
    grades = grades or {}
    for s in summaries:
        g = grades.get(s["model"], {})
        s["overall"] = g.get("overall", "")
        s["strategic"] = g.get("strategic_value", "")
        s["accuracy"] = g.get("accuracy", "")
        s["signal_noise"] = g.get("signal_to_noise", "")
        s["summary"] = g.get("summary", "")
        s["recommendation"] = g.get("recommendation", "")

    # Sort by grade (if available), then thoughts
    summaries.sort(key=lambda x: (-(x["overall"] or 0), -x["thoughts"]))

    # Build thoughts HTML per model per session
    thoughts_html = {}
    for model_name in model_order:
        thoughts_html[model_name] = {}
        for entry in results.get(model_name, []):
            sidx = entry["session_idx"]
            cards = []
            for t in entry["thoughts"]:
                if not isinstance(t, dict):
                    continue
                proj = html_lib.escape(str(t.get("project", "") or "—"))
                kind = html_lib.escape(str(t.get("kind", "")))
                content = html_lib.escape(str(t.get("content", "")))
                tags = ", ".join(t.get("tags", []))
                cards.append(
                    f'<div class="thought">'
                    f'<span class="thought-proj">{proj}</span>'
                    f'<span class="thought-kind">{kind}</span>'
                    f'<p class="thought-content">{content}</p>'
                    f'<span class="thought-tags">{html_lib.escape(tags)}</span>'
                    f'</div>'
                )
            thoughts_html[model_name][sidx] = "\n".join(cards) if cards else '<p class="empty">No thoughts extracted</p>'

    # Build session tabs HTML
    tabs_html = ""
    for sidx, label in enumerate(session_labels):
        active = "active" if sidx == 0 else ""
        tabs_html += f'<button class="tab {active}" onclick="showSession({sidx})">{html_lib.escape(label)}</button>\n'

    # Build session panels
    panels_html = ""
    for sidx, label in enumerate(session_labels):
        display = "grid" if sidx == 0 else "none"
        cols = ""
        for model_name in model_order:
            th = thoughts_html.get(model_name, {}).get(sidx, '<p class="empty">—</p>')
            entries = [e for e in results.get(model_name, []) if e["session_idx"] == sidx]
            count = sum(len(e["thoughts"]) for e in entries)
            cols += f'<div class="model-col"><h4>{html_lib.escape(model_name)} <span class="count">({count})</span></h4>{th}</div>\n'
        panels_html += f'<div class="session-panel" id="session-{sidx}" style="display:{display}">{cols}</div>\n'

    # Wiki pages section
    wiki_section = ""
    if wiki_pages:
        wiki_tabs = ""
        wiki_panels = ""
        first = True
        for model_name in model_order:
            page_md = wiki_pages.get(model_name, "")
            if not page_md:
                continue
            active = "active" if first else ""
            display = "block" if first else "none"
            slug = model_name.replace(".", "-").replace(" ", "-")
            wiki_tabs += f'<button class="tab wiki-tab {active}" onclick="showWiki(\'{slug}\')">{html_lib.escape(model_name)}</button>\n'
            # Convert markdown to simple HTML (headers, bullets, paragraphs)
            page_html = ""
            for md_line in page_md.split("\n"):
                stripped = md_line.strip()
                if stripped.startswith("# "):
                    page_html += f'<h2 class="wiki-h1">{html_lib.escape(stripped[2:])}</h2>\n'
                elif stripped.startswith("## "):
                    page_html += f'<h3 class="wiki-h2">{html_lib.escape(stripped[3:])}</h3>\n'
                elif stripped.startswith("### "):
                    page_html += f'<h4 class="wiki-h3">{html_lib.escape(stripped[4:])}</h4>\n'
                elif stripped.startswith("- "):
                    page_html += f'<li>{html_lib.escape(stripped[2:])}</li>\n'
                elif stripped:
                    page_html += f'<p class="wiki-p">{html_lib.escape(stripped)}</p>\n'
            wiki_panels += f'<div class="wiki-panel" id="wiki-{slug}" style="display:{display}"><div class="wiki-content">{page_html}</div></div>\n'
            first = False

        if wiki_tabs:
            wiki_section = f"""
  <h3 style="color:#fff; margin: 2rem 0 0.75rem; font-size:1rem;">Sample Wiki Pages</h3>
  <p class="subtitle" style="margin-bottom:0.75rem;">Each model's extracted thoughts → merged into a wiki page (all merged by Sonnet)</p>
  <div class="tabs">{wiki_tabs}</div>
  {wiki_panels}
"""

    # Summary table rows
    table_rows = ""
    has_grades = any(s.get("overall") for s in summaries)
    for i, s in enumerate(summaries):
        is_rec = (recommended and s["model"] == recommended) or (not recommended and i == 0)
        badge = ' <span class="badge">★ recommended</span>' if is_rec else ""
        grade_cols = ""
        if has_grades:
            score = s.get("overall", "")
            score_class = "score-high" if isinstance(score, (int, float)) and score >= 8 else "score-mid" if isinstance(score, (int, float)) and score >= 6 else "score-low"
            grade_cols = (
                f'<td class="{score_class}">{score or "—"}</td>'
                f'<td>{s.get("strategic", "") or "—"}</td>'
                f'<td>{s.get("accuracy", "") or "—"}</td>'
                f'<td>{s.get("signal_noise", "") or "—"}</td>'
            )
        summary_col = f'<td class="summary-cell">{html_lib.escape(s.get("summary", ""))}</td>' if has_grades else ""
        table_rows += (
            f'<tr><td>{html_lib.escape(s["model"])}{badge}</td>'
            f'<td>{s["thoughts"]}</td>'
            f'<td>{s["time"]}s</td>'
            f'<td>${s["cost"]:.3f}</td>'
            f'{grade_cols}{summary_col}</tr>\n'
        )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Gyrus — Model Comparison</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #09090b; color: #d4d4d8; font-family: 'Inter', -apple-system, sans-serif; padding: 2rem; }}
  h1 {{ color: #fff; font-size: 1.5rem; margin-bottom: 0.5rem; }}
  h1 span {{ background: linear-gradient(135deg, #9966ff, #7c3aed); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .subtitle {{ color: #71717a; font-size: 0.85rem; margin-bottom: 2rem; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 2rem; }}
  th {{ text-align: left; padding: 0.75rem 1rem; color: #a1a1aa; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid #27272a; }}
  td {{ padding: 0.75rem 1rem; border-bottom: 1px solid #18181b; font-size: 0.9rem; }}
  tr:hover {{ background: #111114; }}
  .badge {{ background: #7c3aed; color: white; font-size: 0.65rem; padding: 0.15rem 0.5rem; border-radius: 4px; margin-left: 0.5rem; vertical-align: middle; }}
  .score-high {{ color: #4ade80; font-weight: 600; }}
  .score-mid {{ color: #fbbf24; }}
  .score-low {{ color: #f87171; }}
  .summary-cell {{ color: #a1a1aa; font-size: 0.8rem; max-width: 300px; }}
  .tabs {{ display: flex; gap: 0.5rem; margin-bottom: 1rem; flex-wrap: wrap; }}
  .tab {{ background: #18181b; border: 1px solid #27272a; color: #a1a1aa; padding: 0.5rem 1rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }}
  .tab.active {{ background: #27272a; color: #fff; border-color: #7c3aed; }}
  .tab:hover {{ background: #1e1e22; }}
  .session-panel {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1rem; }}
  .model-col {{ background: #111114; border: 1px solid #1e1e22; border-radius: 8px; padding: 1rem; max-height: 600px; overflow-y: auto; }}
  .model-col h4 {{ color: #fff; font-size: 0.85rem; margin-bottom: 0.75rem; position: sticky; top: 0; background: #111114; padding-bottom: 0.5rem; }}
  .count {{ color: #7c3aed; font-weight: normal; }}
  .thought {{ background: #18181b; border-radius: 6px; padding: 0.75rem; margin-bottom: 0.5rem; }}
  .thought-proj {{ color: #7c3aed; font-weight: 600; font-size: 0.8rem; }}
  .thought-kind {{ color: #52525b; font-size: 0.7rem; margin-left: 0.5rem; }}
  .thought-content {{ color: #d4d4d8; font-size: 0.8rem; margin-top: 0.35rem; line-height: 1.5; }}
  .thought-tags {{ color: #3f3f46; font-size: 0.7rem; }}
  .empty {{ color: #3f3f46; font-style: italic; font-size: 0.85rem; }}
  .wiki-content {{ background: #111114; border: 1px solid #1e1e22; border-radius: 8px; padding: 1.5rem 2rem; max-height: 700px; overflow-y: auto; }}
  .wiki-h1 {{ color: #9966ff; font-size: 1.3rem; margin: 0 0 0.75rem; }}
  .wiki-h2 {{ color: #fff; font-size: 1rem; margin: 1.25rem 0 0.5rem; border-bottom: 1px solid #1e1e22; padding-bottom: 0.3rem; }}
  .wiki-h3 {{ color: #a1a1aa; font-size: 0.9rem; margin: 0.75rem 0 0.4rem; }}
  .wiki-p {{ color: #d4d4d8; font-size: 0.85rem; line-height: 1.6; margin: 0.4rem 0; }}
  .wiki-content li {{ color: #d4d4d8; font-size: 0.85rem; line-height: 1.6; margin-left: 1.5rem; list-style: disc; }}
  .footer {{ margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #1e1e22; color: #52525b; font-size: 0.8rem; }}
  code {{ background: #18181b; padding: 0.15rem 0.4rem; border-radius: 3px; font-size: 0.8rem; color: #a1a1aa; }}
</style>
</head>
<body>
  <h1><span>Gyrus</span> — Model Comparison</h1>
  <p class="subtitle">Tested {len(model_order)} models on {len(sessions)} of your sessions</p>

  <table>
    <thead><tr><th>Model</th><th>Thoughts</th><th>Time</th><th>Est. Cost</th>{"<th>Score</th><th>Strategic</th><th>Accuracy</th><th>Signal/Noise</th><th>Assessment</th>" if has_grades else ""}</tr></thead>
    <tbody>{table_rows}</tbody>
  </table>

  <h3 style="color:#fff; margin-bottom:0.75rem; font-size:1rem;">Extracted Thoughts</h3>
  <div class="tabs">{tabs_html}</div>
  {panels_html}

  {wiki_section}

  <div class="footer">
    <p>To select a model: <code>Pick a number in the terminal</code> or edit <code>~/.gyrus/config.json</code></p>
  </div>

  <script>
    function showSession(idx) {{
      document.querySelectorAll('.session-panel').forEach(p => p.style.display = 'none');
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.getElementById('session-' + idx).style.display = 'grid';
      document.querySelectorAll('.tab')[idx].classList.add('active');
    }}
    function showWiki(model) {{
      document.querySelectorAll('.wiki-panel').forEach(p => p.style.display = 'none');
      document.querySelectorAll('.wiki-tab').forEach(t => t.classList.remove('active'));
      document.getElementById('wiki-' + model).style.display = 'block';
      event.target.classList.add('active');
    }}
  </script>
</body>
</html>"""

    Path(output_path).write_text(page)


def main():
    parser = argparse.ArgumentParser(description="Gyrus — knowledge ingestion")
    parser.add_argument("--version", action="version", version=f"Gyrus v{__version__}")
    parser.add_argument("--update", action="store_true",
                        help="Update Gyrus to the latest version from GitHub")
    parser.add_argument("--compare-models", action="store_true",
                        help="Compare extraction models on your sessions and pick one")
    parser.add_argument("--review-status", action="store_true",
                        help="Interactively review and set project statuses")
    parser.add_argument("--doctor", action="store_true",
                        help="Run diagnostic health checks")
    parser.add_argument("--fix", action="store_true",
                        help="With --doctor: attempt safe auto-fixes inline")
    parser.add_argument("--init", action="store_true",
                        help="First-time setup wizard (storage, key, GitHub, cron)")
    parser.add_argument("--clone", metavar="URL",
                        help="With --init: clone an existing knowledge-base repo")
    parser.add_argument("--init-location", metavar="PATH",
                        help="With --init: override default storage path")
    parser.add_argument("--sync", action="store_true",
                        help="Manually pull and push the git remote")
    parser.add_argument("--merge", nargs="+", metavar="SLUG",
                        help="Merge slugs: last SLUG is the target, others are sources. "
                             "Example: --merge calledthird-website calledthird")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip interactive confirmation (e.g. for --merge in scripts)")
    parser.add_argument("--no-autosync", action="store_true",
                        help="Skip the automatic git pull/push on this run")
    parser.add_argument("--digest", action="store_true",
                        help="Generate a digest from the latest ingestion run")
    parser.add_argument("--sync-context", action="store_true",
                        help="Write project context to AI tool instruction files")
    parser.add_argument("--show-log", action="store_true",
                        help="Show recent run history")
    parser.add_argument("--log-count", type=int, default=10,
                        help="Number of recent runs to show (default: 10)")
    parser.add_argument("--eval", action="store_true",
                        help="Run prompt quality eval against golden fixtures")
    parser.add_argument("--eval-curate", action="store_true",
                        help="Create golden test fixtures from real sessions")
    parser.add_argument("--eval-deep", action="store_true",
                        help="Include LLM-assisted hallucination spot-checks")
    parser.add_argument("--eval-type", choices=["extraction", "merge", "both"],
                        default="both", help="Which eval to run (default: both)")
    parser.add_argument("--eval-compare", nargs=2, metavar=("V1", "V2"),
                        help="Compare two saved prompt versions")
    parser.add_argument("--eval-regression", action="store_true",
                        help="Exit 1 if any metric dropped vs baseline")
    parser.add_argument("--eval-save-prompt", metavar="NAME",
                        help="Save current prompts as a named version")
    parser.add_argument("--eval-session", metavar="SESSION_ID",
                        help="Session ID for --eval-curate")
    parser.add_argument("--eval-fixture", metavar="ID",
                        help="Run eval on a single fixture")
    parser.add_argument("--anthropic-key",
                        help="Anthropic API key")
    parser.add_argument("--openai-key",
                        help="OpenAI API key (optional, for GPT models)")
    parser.add_argument("--google-key",
                        help="Google AI API key (optional, for Gemini models)")
    parser.add_argument("--extract-model", default=None,
                        help=f"Model for extraction (default: {DEFAULT_EXTRACT_MODEL}). "
                             f"Options: {', '.join(MODEL_CATALOG.keys())}")
    parser.add_argument("--merge-model", default=None,
                        help=f"Model for merging (default: {DEFAULT_MERGE_MODEL}). "
                             f"Options: {', '.join(MODEL_CATALOG.keys())}")
    parser.add_argument("--storage", default="markdown",
                        choices=["markdown", "notion"],
                        help="Storage backend (default: markdown)")
    parser.add_argument("--notion-key", default=None,
                        help="Notion API key (required if --storage=notion)")
    parser.add_argument("--notion-db", default=None,
                        help="Notion database ID for knowledge base")
    parser.add_argument("--notion-aliases-db", default=None,
                        help="Notion database ID for aliases")
    parser.add_argument("--base-dir", default=None,
                        help="Base directory for storage (default: ~/.gyrus)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill", action="store_true",
                        help="Rebuild knowledge pages from existing thoughts")
    args = parser.parse_args()

    # Handle --update early
    if args.update:
        base = Path(args.base_dir) if args.base_dir else Path.home() / ".gyrus"
        success = self_update(base)
        sys.exit(0 if success else 1)

    # Handle --init early — doesn't need an existing gyrus home
    if args.init:
        sys.exit(run_init(clone_url=args.clone, location=args.init_location))

    # Load .env file early (so Notion keys and other env vars are available)
    env_base = Path(args.base_dir) if args.base_dir else Path.home() / ".gyrus"
    env_file = env_base / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

    # Heartbeat — always tell the user whether ingest is alive, before any
    # expensive work. Stat-only, so it can't hang on dataless iCloud files.
    _print_heartbeat(env_base)

    # Auto-sync: pull latest from origin before any work. Non-fatal, quick,
    # silent if nothing changed. Skipped on --no-autosync or for --sync itself
    # (which does its own pull).
    if not args.no_autosync and not args.sync and not args.doctor:
        _autosync_pull(env_base)

    # Handle --sync early (manual pull + push, no ingest)
    if args.sync:
        sys.exit(run_sync(env_base))

    # Handle --merge early — rewrites aliases + thoughts, no LLM calls
    if args.merge:
        store = MarkdownStorage(base_dir=str(env_base))
        sys.exit(run_merge(store, args.merge, yes=args.yes))

    # Handle --compare-models early
    if args.compare_models:
        keys = {
            "anthropic": args.anthropic_key or os.environ.get("ANTHROPIC_API_KEY"),
            "openai": args.openai_key or os.environ.get("OPENAI_API_KEY"),
            "google": (args.google_key or os.environ.get("GOOGLE_API_KEY")
                       or os.environ.get("GEMINI_API_KEY")),
        }
        keys = {k: v for k, v in keys.items() if v}
        if not keys:
            parser.error("At least one API key required. Use --anthropic-key, --openai-key, or --google-key")
        file_config = _load_config(type("S", (), {"base_dir": env_base})())
        compare_models(keys, env_base, file_config)
        sys.exit(0)

    # Handle --review-status early
    if args.review_status:
        store = MarkdownStorage(base_dir=str(env_base))
        review_project_status(store)
        if not args.no_autosync:
            _autosync_push(env_base,
                           f"gyrus status · {datetime.now():%Y-%m-%d %H:%M}")
        sys.exit(0)

    # Handle --doctor early — never touches the network or API keys
    # (unless --fix is also set, in which case we may run `git pull`/`git push`
    # and `brctl download`, still no LLM calls / no $ cost)
    if args.doctor:
        sys.exit(run_doctor(env_base, fix=args.fix))

    # Handle --digest early
    if args.digest:
        store = MarkdownStorage(base_dir=str(env_base))
        file_config = _load_config(type("S", (), {"base_dir": env_base})())
        # Load recent thoughts (last 24h)
        all_thoughts = store.get_thoughts(skipped=False, order_desc=True, limit=500)
        today = datetime.now().date()
        recent = [t for t in all_thoughts
                  if t.get("created_at", "")[:10] and
                  (today - datetime.fromisoformat(t["created_at"][:10]).date()).days <= 1]
        if not recent:
            print("No new thoughts in the last 24 hours.")
        else:
            digest = generate_digest(recent, store, [])
            digest_path = env_base / "latest-digest.md"
            digest_path.write_text(digest)
            print(digest)
            print(f"\nSaved to: {digest_path}")
            # Email if configured
            digest_config = file_config.get("digest", {})
            if digest_config.get("email"):
                send_digest_email(digest, digest_config, env_base)
        if not args.no_autosync:
            _autosync_push(env_base,
                           f"gyrus digest · {datetime.now():%Y-%m-%d}")
        sys.exit(0)

    # Handle --show-log early
    if args.show_log:
        show_run_log(env_base, n=args.log_count)
        sys.exit(0)

    # Handle --sync-context early
    if args.sync_context:
        store = MarkdownStorage(base_dir=str(env_base))
        sync_tool_context(store)
        sys.exit(0)

    # Handle --eval, --eval-curate, --eval-save-prompt early
    if args.eval or args.eval_curate or args.eval_save_prompt:
        from eval_prompts import run_eval, run_curate, save_prompt_version
        file_config = _load_config(type("S", (), {"base_dir": env_base})())
        # Set up keys — read directly from .env since os.environ.setdefault may not have overridden
        env_keys = {}
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env_keys[k.strip()] = v.strip().strip("\"'")
        keys = {
            "anthropic": args.anthropic_key or env_keys.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"),
            "openai": args.openai_key or env_keys.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"),
            "google": (args.google_key or env_keys.get("GEMINI_API_KEY")
                       or env_keys.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")),
        }
        _config["keys"] = {k: v for k, v in keys.items() if v}
        _config["extract_model"] = file_config.get("extract_model", DEFAULT_EXTRACT_MODEL)
        _config["merge_model"] = file_config.get("merge_model", DEFAULT_MERGE_MODEL)
        if args.eval_save_prompt:
            save_prompt_version(env_base, args.eval_save_prompt,
                                EXTRACTION_PROMPT, MERGE_PROMPT)
            sys.exit(0)
        if args.eval_curate:
            run_curate(args, env_base)
        else:
            run_eval(args, env_base, file_config)
        sys.exit(0)

    if args.storage == "notion":
        try:
            from storage_notion import NotionStorage
        except ImportError:
            parser.error("Notion storage requires storage_notion.py. "
                         "Download it from https://github.com/prismindanalytics/gyrus")
        notion_key = (args.notion_key
                      or os.environ.get("NOTION_API_KEY")
                      or None)
        if not notion_key:
            parser.error("--notion-key or NOTION_API_KEY required for Notion storage")
        notion_db = (args.notion_db
                     or os.environ.get("NOTION_DB_ID")
                     or None)
        if not notion_db:
            parser.error("--notion-db or NOTION_DB_ID required. "
                         "Run: python3 -c \"from storage_notion import setup_notion_databases; "
                         "print(setup_notion_databases('YOUR_KEY'))\" to create databases.")
        store = NotionStorage(notion_key, notion_db, args.notion_aliases_db)
        print("  Storage: Notion")
    else:
        store = MarkdownStorage(base_dir=args.base_dir)
        print(f"  Storage: {store.base_dir}")

    # Acquire lock (prevents concurrent runs: e.g. cron + manual overlap)
    if not args.dry_run and not _acquire_lock(
        store.base_dir if hasattr(store, 'base_dir') else Path.home() / ".gyrus"
    ):
        return

    # Load config from file, then override with CLI args and env vars
    file_config = _load_config(store)

    # API keys: CLI > env var > .env file > config file
    anthropic_key = (args.anthropic_key
                     or os.environ.get("ANTHROPIC_API_KEY")
                     or file_config.get("anthropic_key"))
    openai_key = (args.openai_key
                  or os.environ.get("OPENAI_API_KEY")
                  or file_config.get("openai_key"))
    google_key = (args.google_key
                  or os.environ.get("GOOGLE_API_KEY")
                  or os.environ.get("GEMINI_API_KEY")
                  or file_config.get("google_key"))

    # Models: CLI > config file > defaults
    extract_model = (args.extract_model
                     or file_config.get("extract_model")
                     or DEFAULT_EXTRACT_MODEL)
    merge_model = (args.merge_model
                   or file_config.get("merge_model")
                   or DEFAULT_MERGE_MODEL)

    # Validate at least one key is provided
    if not any([anthropic_key, openai_key, google_key]):
        parser.error("At least one API key is required. Use --anthropic-key, --openai-key, or --google-key")

    # Set global config
    _config["extract_model"] = extract_model
    _config["merge_model"] = merge_model
    _config["keys"] = {
        k: v for k, v in {
            "anthropic": anthropic_key,
            "openai": openai_key,
            "google": google_key,
        }.items() if v
    }

    # Validate that the chosen models have API keys
    for role, model_name in [("extract", extract_model), ("merge", merge_model)]:
        resolved = _resolve_model(model_name)
        if resolved["provider"] not in _config["keys"]:
            parser.error(
                f"{role} model '{model_name}' requires a {resolved['provider']} API key. "
                f"Set --{resolved['provider']}-key or {resolved['provider'].upper()}_API_KEY"
            )

    print(f"  Models: extract={extract_model}, merge={merge_model}")

    # ─── Backfill mode ───
    if args.backfill:
        print("Backfilling knowledge pages from existing thoughts...")
        all_thoughts = store.get_thoughts(skipped=False, order_desc=False)
        # Filter to thoughts with canonical_project
        all_thoughts = [t for t in all_thoughts if t.get("canonical_project")]

        by_project = defaultdict(list)
        for t in all_thoughts:
            by_project[t["canonical_project"]].append(t)

        print(f"Found {len(by_project)} projects to backfill")

        # Process week by week for iterative refinement
        weekly = defaultdict(lambda: defaultdict(list))
        for t in all_thoughts:
            dt_str = t.get("created_at", "")[:10]
            try:
                dt = datetime.fromisoformat(dt_str)
                week_start = dt - timedelta(days=dt.weekday())
                week_key = week_start.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                week_key = "unknown"
            weekly[week_key][t["canonical_project"]].append(t)

        for week_num, (week_key, projects) in enumerate(sorted(weekly.items()), 1):
            total = sum(len(v) for v in projects.values())
            print(f"\n  ── Week {week_num}: {week_key} ({total} thoughts, {len(projects)} projects) ──")
            merge_into_knowledge_pages(projects, store, anthropic_key)

        generate_status(store)
        print("\nBackfill complete.")
        return

    # ─── Normal ingestion ───
    state = store.load_state()

    all_sessions = (
        find_claude_code_sessions(state) +
        find_cowork_sessions(state) +
        find_antigravity_sessions(state) +
        find_codex_sessions(state) +
        find_cursor_sessions(state) +
        find_copilot_sessions(state) +
        find_cline_sessions(state) +
        find_continue_sessions(state) +
        find_aider_sessions(state) +
        find_opencode_sessions(state)
    )

    # Respect excluded_tools from config
    excluded_tools = file_config.get("excluded_tools", [])
    if excluded_tools:
        before = len(all_sessions)
        all_sessions = [s for s in all_sessions if s["type"] not in excluded_tools]
        excluded_count = before - len(all_sessions)
        if excluded_count:
            print(f"  Excluded {excluded_count} sessions from: {', '.join(excluded_tools)}")

    if not all_sessions:
        print("No new sessions to process.")
        if not args.dry_run:
            generate_status(store)
        return

    # Count sessions by type
    counts = defaultdict(int)
    for s in all_sessions:
        counts[s["type"]] += 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()) if v > 0)
    print(f"Found: {summary}")

    # ── Cost estimation ──
    n_sessions = len(all_sessions)
    # Estimate: ~4KB avg input per extraction, ~500 tokens output
    # Merge: ~8KB avg input per project, ~4K tokens output, ~n_sessions/10 projects
    est_projects = max(1, n_sessions // 10)
    ext_input_tok = n_sessions * 4000 / 1_000_000   # MTok
    ext_output_tok = n_sessions * 500 / 1_000_000
    merge_input_tok = est_projects * 8000 / 1_000_000
    merge_output_tok = est_projects * 4000 / 1_000_000

    ext_price = MODEL_PRICING.get(extract_model, (1, 5))
    merge_price = MODEL_PRICING.get(merge_model, (3, 15))

    ext_cost = ext_input_tok * ext_price[0] + ext_output_tok * ext_price[1]
    merge_cost = merge_input_tok * merge_price[0] + merge_output_tok * merge_price[1]
    total_est = ext_cost + merge_cost

    # Time estimate: ~5s per extraction call with parallelism, ~15s per merge
    max_workers = file_config.get("parallel_extractions", 4)
    ext_time_mins = (n_sessions / max_workers * 5) / 60
    merge_time_mins = (est_projects * 15) / 60
    total_time_mins = ext_time_mins + merge_time_mins

    print(f"  Cost estimate: ~${total_est:.2f} "
          f"({n_sessions} extractions @ {extract_model}, "
          f"~{est_projects} merges @ {merge_model})")
    print(f"  Time estimate: ~{total_time_mins:.0f} minutes "
          f"({max_workers} parallel workers)")

    # If large batch, offer live vs background
    if n_sessions > 20 and not args.dry_run and sys.stdin.isatty():
        print()
        print(f"  [1] Run now and watch progress")
        print(f"  [2] Run in background (come back later)")
        print(f"  [3] Cancel")
        try:
            choice = input(f"\n  Choice [1]: ").strip()
        except EOFError:
            choice = "1"

        if choice == "3":
            print("  Cancelled.")
            _release_lock(store.base_dir if hasattr(store, 'base_dir') else Path.home() / ".gyrus")
            return
        elif choice == "2":
            # Fork to background
            log_file = store.base_dir / "ingest.log" if hasattr(store, 'base_dir') else Path.home() / ".gyrus" / "ingest.log"
            # Re-run self in background
            import subprocess
            cmd = [sys.executable, __file__]
            # Pass through all original args
            for arg in sys.argv[1:]:
                cmd.append(arg)
            print(f"  Starting background ingestion...")
            print(f"  Progress: tail -f {log_file}")
            with open(log_file, "a") as lf:
                subprocess.Popen(cmd, stdout=lf, stderr=lf,
                                 start_new_session=True)
            print(f"  Knowledge pages will appear in: {store.base_dir / 'projects'}/")
            _release_lock(store.base_dir if hasattr(store, 'base_dir') else Path.home() / ".gyrus")
            return

    EXTRACTORS = {
        "claude-code": lambda s: extract_claude_code_conversation(s["path"]),
        "cowork": lambda s: extract_cowork_conversation(s["path"], s.get("output_dir")),
        "antigravity": lambda s: extract_antigravity_session(s["path"]),
        "codex": lambda s: extract_codex_conversation(s["path"]),
        "cursor": lambda s: extract_cursor_conversation(s["path"]),
        "copilot": lambda s: extract_copilot_conversation(s["path"]),
        "cline": lambda s: extract_cline_conversation(s["path"]),
        "continue": lambda s: extract_continue_conversation(s["path"]),
        "aider": lambda s: extract_aider_conversation(s["path"]),
        "opencode": lambda s: extract_opencode_conversation(s["path"]),
    }

    def extract_text(session):
        extractor = EXTRACTORS.get(session["type"])
        return extractor(session) if extractor else ""

    # ── Step 0: Read tool memory files for bonus context ──
    memory_files = find_tool_memory_files()
    memory_context = ""
    if memory_files:
        print(f"  Found {len(memory_files)} tool memory files for context")
        memory_context = "\n\n---\nBONUS CONTEXT (from AI tool memory/rules files — extract relevant insights only if they contain strategic decisions):\n"
        for name, content in memory_files:
            memory_context += f"\n--- {name} ---\n{content}\n"

    # ── Step 1: Extract & save thoughts ──
    batch_thoughts = []
    repo_groups = file_config.get("repo_groups")
    max_workers = file_config.get("parallel_extractions", 4)

    def _process_session(session):
        """Extract thoughts from a single session (thread-safe for LLM calls)."""
        source = session["type"]
        text = extract_text(session)
        # Append tool memory context to first session for richer extraction
        if memory_context and session is all_sessions[0]:
            text += memory_context
        if len(text) < 100:
            return session, []
        workspace = session.get("workspace", "")
        thoughts = call_claude(text, anthropic_key, workspace=workspace,
                               repo_groups=repo_groups)
        return session, thoughts

    total = len(all_sessions)
    _start_time = time.time()
    _completed = [0]  # mutable for closure

    def _progress_line(i, source, session_id, detail=""):
        elapsed = time.time() - _start_time
        if i > 0:
            eta_secs = (elapsed / i) * (total - i)
            eta = f" ETA {int(eta_secs//60)}m{int(eta_secs%60):02d}s" if eta_secs > 10 else ""
        else:
            eta = ""
        return f"  [{i}/{total}]{eta} {source}: {session_id[:20]}... {detail}"

    if max_workers > 1 and len(all_sessions) > 1:
        # Parallel extraction
        from concurrent.futures import ThreadPoolExecutor, as_completed
        print(f"  Extracting with {max_workers} parallel workers...")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_session, s): s for s in all_sessions}
            for future in as_completed(futures):
                session = futures[future]
                source = session["type"]
                _completed[0] += 1
                try:
                    _, thoughts = future.result()
                except Exception as e:
                    print(_progress_line(_completed[0], source, session["session_id"], f"Error: {e}"))
                    thoughts = []

                if not thoughts:
                    state["processed_sessions"][session["state_key"]] = session["mtime"]
                    continue

                print(_progress_line(_completed[0], source, session["session_id"],
                                     f"{len(thoughts)} thoughts"))

                if not args.dry_run:
                    session_date = datetime.fromtimestamp(
                        session["mtime"], tz=timezone.utc
                    ).isoformat()
                    store.save_thoughts(
                        thoughts, source, session["session_id"],
                        session_date=session_date, machine=_MACHINE,
                    )
                    workspace = session.get("workspace", "")
                    for t in thoughts:
                        t["source"] = source
                        t["created_at"] = session_date
                        t["machine"] = _MACHINE
                        t["workspace"] = workspace
                    batch_thoughts.extend(thoughts)
                else:
                    for t in thoughts:
                        print(f"    -> {t['content'][:100]}")

                state["processed_sessions"][session["state_key"]] = session["mtime"]
    else:
        # Sequential extraction (single worker)
        for idx, session in enumerate(all_sessions):
            source = session["type"]
            print(_progress_line(idx, source, session["session_id"]), end="", flush=True)

            text = extract_text(session)
            if memory_context and session == all_sessions[0]:
                text += memory_context
            if len(text) < 100:
                print(" skipped (too short)")
                state["processed_sessions"][session["state_key"]] = session["mtime"]
                continue

            workspace = session.get("workspace", "")
            thoughts = call_claude(text, anthropic_key, workspace=workspace,
                                   repo_groups=repo_groups)
            print(f" {len(thoughts)} thoughts")

            if thoughts and not args.dry_run:
                session_date = datetime.fromtimestamp(
                    session["mtime"], tz=timezone.utc
                ).isoformat()
                store.save_thoughts(
                    thoughts, source, session["session_id"],
                    session_date=session_date, machine=_MACHINE,
                )
                for t in thoughts:
                    t["source"] = source
                    t["created_at"] = session_date
                    t["machine"] = _MACHINE
                    t["workspace"] = workspace
                batch_thoughts.extend(thoughts)

            if args.dry_run:
                for t in thoughts:
                    print(f"    -> {t['content'][:100]}")
            else:
                state["processed_sessions"][session["state_key"]] = session["mtime"]

    if not args.dry_run:
        store.save_state(state)

    # ── Step 2: Knowledge pipeline ──
    if batch_thoughts and not args.dry_run:
        print(f"\n{'='*50}")
        print(f"Knowledge Pipeline: {len(batch_thoughts)} new thoughts")
        print(f"{'='*50}")

        # Phase 1: Normalize
        print("\nPhase 1: Normalizing...")
        batch_thoughts = resolve_aliases(batch_thoughts, store,
                                         repo_groups=file_config.get("repo_groups"))
        batch_thoughts = deduplicate_thoughts(batch_thoughts, store)
        batch_thoughts = persist_thought_metadata(batch_thoughts, store)

        # Classify thoughts into three buckets by kind
        active_thoughts = [t for t in batch_thoughts
                           if not t.get("skipped") and t.get("canonical_project")]
        idea_thoughts = [t for t in batch_thoughts
                         if not t.get("skipped") and not t.get("canonical_project")
                         and t.get("kind") == "idea"]
        meta_thoughts = [t for t in batch_thoughts
                         if not t.get("skipped") and not t.get("canonical_project")
                         and t.get("kind") != "idea"]

        if active_thoughts:
            # Phase 2a: Merge into project pages
            print(f"\nPhase 2a: Merging {len(active_thoughts)} thoughts into knowledge pages...")
            by_project = defaultdict(list)
            for t in active_thoughts:
                by_project[t["canonical_project"]].append(t)
            merge_into_knowledge_pages(by_project, store, anthropic_key)

        if idea_thoughts:
            # Phase 2b: Merge ideas into ideas.md
            print(f"\nPhase 2b: Merging {len(idea_thoughts)} ideas into ideas.md...")
            merge_into_ideas_page(idea_thoughts, store, anthropic_key)

        if meta_thoughts:
            # Phase 2c: Merge meta/personal thoughts into me.md
            print(f"\nPhase 2c: Merging {len(meta_thoughts)} meta thoughts into me.md...")
            merge_into_me_page(meta_thoughts, store, anthropic_key)

        if active_thoughts:

            # Phase 3: Cross-reference (daily or if enough thoughts)
            last_xref = state.get("last_cross_reference", 0)
            hours_since = (time.time() - last_xref) / 3600
            if hours_since >= 24 or len(active_thoughts) >= 5:
                print("\nPhase 3: Cross-reference scan...")
                run_cross_reference_scan(store, anthropic_key, active_thoughts)
                state["last_cross_reference"] = time.time()
                store.save_state(state)

    # ── Step 3: Update status files + tool context ──
    if not args.dry_run:
        print("\nUpdating status files...")
        generate_status(store)
        sync_tool_context(store)

    # ── Step 4: Daily digest ──
    if batch_thoughts and not args.dry_run:
        digest_config = file_config.get("digest", {})
        if digest_config.get("enabled", False):
            digest = generate_digest(batch_thoughts, store, all_sessions)
            if digest_config.get("email"):
                send_digest_email(digest, digest_config, store.base_dir if hasattr(store, "base_dir") else Path.home() / ".gyrus")
            # Always save to file
            digest_path = (store.base_dir if hasattr(store, "base_dir") else Path.home() / ".gyrus") / "latest-digest.md"
            digest_path.write_text(digest)
            print(f"  Digest: {digest_path}")

    # ── Summary + Run Log ──
    extract_model = _config["extract_model"]
    merge_model = _config["merge_model"]
    extract_cost = _usage["extract_calls"] * _COST_PER_CALL.get(extract_model, 0.01)
    merge_cost = _usage["merge_calls"] * _COST_PER_CALL.get(merge_model, 0.03)
    total_cost = extract_cost + merge_cost

    print(f"\nDone. Processed {len(all_sessions)} sessions, "
          f"{len(batch_thoughts)} thoughts extracted.")
    print(f"  LLM calls: {_usage['extract_calls']} extraction ({extract_model}), "
          f"{_usage['merge_calls']} merge ({merge_model})")
    if total_cost > 0:
        print(f"  Estimated cost this run: ~${total_cost:.3f}")

    # Save structured run log
    if not args.dry_run:
        _save_run_log(store, all_sessions, batch_thoughts, total_cost)

    # Offer status review on first run (interactive terminal only)
    if batch_thoughts and not args.dry_run and sys.stdin.isatty():
        try:
            do_review = input("\n  Review project statuses? [Y/n]: ").strip()
        except EOFError:
            do_review = "n"
        if not do_review or do_review.lower().startswith("y"):
            review_project_status(store)

    _release_lock(store.base_dir if hasattr(store, 'base_dir') else Path.home() / ".gyrus")

    # Auto-sync: push results to origin. Non-fatal, silent if no changes.
    if not args.no_autosync and not args.dry_run:
        _autosync_push(
            store.base_dir if hasattr(store, 'base_dir') else Path.home() / ".gyrus",
            f"gyrus ingest · {datetime.now():%Y-%m-%d %H:%M} · "
            f"{len(all_sessions)} sessions, {len(batch_thoughts)} thoughts",
        )


if __name__ == "__main__":
    main()
