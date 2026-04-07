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

__version__ = "0.1.0"

import argparse
import atexit
import json
import os
import sys
import glob
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import platform
import socket


# ─── Lockfile (prevents conflicts on cloud-synced folders) ───

def _acquire_lock(base_dir):
    """Acquire a lockfile to prevent concurrent ingestion runs.
    Returns True if acquired, False if another instance is running."""
    lock_path = Path(base_dir) / ".gyrus.lock"
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
        except (json.JSONDecodeError, IOError):
            pass  # Corrupt lock file — override it

    lock_path.write_text(json.dumps({
        "machine": socket.gethostname(),
        "pid": os.getpid(),
        "time": time.time(),
    }))
    atexit.register(lambda: lock_path.unlink(missing_ok=True))
    return True


def _release_lock(base_dir):
    """Release the lockfile."""
    lock_path = Path(base_dir) / ".gyrus.lock"
    lock_path.unlink(missing_ok=True)

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

        # Priority 3: Fuzzy match
        best_match = None
        best_score = 0
        project_lower = project.lower().replace(" ", "").replace("-", "").replace("_", "")
        for alias, slug in aliases.items():
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
            # Priority 4: If workspace is set, use it as the canonical slug
            if workspace:
                slug = workspace.lower().replace(" ", "-")
                slug = "".join(c for c in slug if c.isalnum() or c == "-")
            else:
                slug = project.lower().replace(" ", "-")
                slug = "".join(c for c in slug if c.isalnum() or c == "-")
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


def _get_project_recency(store):
    """Get the most recent thought date per project."""
    recency = {}
    thoughts_dir = store.base_dir / "thoughts" if hasattr(store, "base_dir") else Path.home() / ".gyrus" / "thoughts"
    if not thoughts_dir.exists():
        return recency
    for jsonl_file in sorted(thoughts_dir.glob("*.jsonl"), reverse=True):
        try:
            for line in jsonl_file.read_text().splitlines():
                t = json.loads(line)
                cp = t.get("canonical_project") or t.get("merged_into_page")
                if cp and cp not in recency:
                    created = t.get("created_at", "")[:10]
                    if created:
                        recency[cp] = created
        except (json.JSONDecodeError, IOError):
            continue
    return recency


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
    parser.add_argument("--digest", action="store_true",
                        help="Generate a digest from the latest ingestion run")
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

    # Load .env file early (so Notion keys and other env vars are available)
    env_base = Path(args.base_dir) if args.base_dir else Path.home() / ".gyrus"
    env_file = env_base / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

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
        sys.exit(0)

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

    # Acquire lock (prevents conflicts on cloud-synced folders)
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

    # ── Step 3: Update status files ──
    if not args.dry_run:
        print("\nUpdating status files...")
        generate_status(store)

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

    # ── Summary ──
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

    # Offer status review on first run (interactive terminal only)
    if batch_thoughts and not args.dry_run and sys.stdin.isatty():
        try:
            do_review = input("\n  Review project statuses? [Y/n]: ").strip()
        except EOFError:
            do_review = "n"
        if not do_review or do_review.lower().startswith("y"):
            review_project_status(store)

    _release_lock(store.base_dir if hasattr(store, 'base_dir') else Path.home() / ".gyrus")


if __name__ == "__main__":
    main()
