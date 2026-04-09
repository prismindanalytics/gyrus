"""
Storage adapters for Gyrus.
Default: MarkdownStorage — pure local files, zero signup.
Optional: NotionStorage — Notion API (add later).
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from difflib import SequenceMatcher


def _ensure_downloaded(path):
    """Force iCloud to download a file if it's evicted (placeholder only).
    On macOS, iCloud can evict files to save space. This forces a download."""
    import subprocess, platform
    p = Path(path)
    if platform.system() != "Darwin" or not p.exists():
        return
    # Check if file is evicted (iCloud placeholder) — has .icloud prefix in parent
    icloud_placeholder = p.parent / f".{p.name}.icloud"
    if icloud_placeholder.exists():
        try:
            subprocess.run(["brctl", "download", str(p)], timeout=30,
                          capture_output=True)
            # Wait for download
            for _ in range(60):  # up to 30 seconds
                if not icloud_placeholder.exists():
                    break
                time.sleep(0.5)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # brctl not available or timed out


def _safe_write(path, content, retries=3):
    """Write to a file with retry for cloud sync lock conflicts (iCloud/Dropbox)."""
    for attempt in range(retries):
        try:
            Path(path).write_text(content)
            return
        except OSError as e:
            if e.errno == 11 and attempt < retries - 1:  # Resource deadlock avoided
                time.sleep(0.5 * (attempt + 1))
                continue
            raise


def _safe_read(path, retries=3):
    """Read a file with retry for cloud sync lock conflicts."""
    _ensure_downloaded(path)
    for attempt in range(retries):
        try:
            return Path(path).read_text()
        except OSError as e:
            if e.errno == 11 and attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise


class MarkdownStorage:
    """
    Local filesystem storage. Knowledge pages are markdown files,
    thoughts are JSONL files, aliases are a JSON file.

    Structure:
      ~/.gyrus/
        config.json
        thoughts/
          2026-03-20.jsonl
          2026-03-21.jsonl
        projects/
          chartlite.md
          everyplace.md
        aliases.json
        status.md
        cross-cutting.md
    """

    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or os.path.expanduser("~/.gyrus"))
        self.thoughts_dir = self.base_dir / "thoughts"
        self.projects_dir = self.base_dir / "projects"
        self.aliases_file = self.base_dir / "aliases.json"
        self.state_file = self.base_dir / ".ingest-state.json"

        # Ensure directories exist
        self.thoughts_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    # ─── Thoughts ───

    def save_thought(self, thought):
        """Append a thought to the daily JSONL file."""
        dt = thought.get("created_at", datetime.now(timezone.utc).isoformat())
        # Parse the date for the filename
        if isinstance(dt, str):
            date_str = dt[:10]  # YYYY-MM-DD
        else:
            date_str = dt.strftime("%Y-%m-%d")

        # Assign an ID if not present — use full content hash + random suffix for uniqueness
        if "id" not in thought:
            import hashlib, uuid
            content_hash = hashlib.sha256(thought["content"].encode()).hexdigest()[:12]
            thought["id"] = f"{date_str}-{content_hash}-{uuid.uuid4().hex[:6]}"

        filepath = self.thoughts_dir / f"{date_str}.jsonl"
        for _attempt in range(3):
            try:
                with open(filepath, "a") as f:
                    f.write(json.dumps(thought, default=str) + "\n")
                break
            except OSError as e:
                if e.errno == 11 and _attempt < 2:
                    time.sleep(0.5 * (_attempt + 1))
                else:
                    raise

        return thought["id"]

    def save_thoughts(self, thoughts, source, session_id, session_date=None, machine=None):
        """Save a batch of extracted thoughts."""
        ids = []
        for thought in thoughts:
            row = {
                "content": thought["content"],
                "source": source,
                "session_id": session_id,
                "project": thought.get("project"),
                "canonical_project": thought.get("canonical_project"),
                "tags": thought.get("tags", []),
                "processed": False,
                "merged_into_page": None,
                "skipped": thought.get("skipped", False),
                "skip_reason": thought.get("skip_reason"),
                "machine": machine,
                "created_at": session_date or datetime.now(timezone.utc).isoformat(),
            }
            tid = self.save_thought(row)
            thought["id"] = tid
            ids.append(tid)
        return ids

    def get_thoughts(self, canonical_project=None, merged=None, processed=None,
                     skipped=None, limit=None, order_desc=True):
        """Read thoughts from JSONL files with optional filters."""
        all_thoughts = []
        jsonl_files = sorted(self.thoughts_dir.glob("*.jsonl"),
                             reverse=order_desc)

        for filepath in jsonl_files:
            with open(filepath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Apply filters
                    if canonical_project is not None:
                        if t.get("canonical_project") != canonical_project:
                            continue
                    if merged is not None:
                        if merged and not t.get("merged_into_page"):
                            continue
                        if not merged and t.get("merged_into_page"):
                            continue
                    if processed is not None:
                        if t.get("processed") != processed:
                            continue
                    if skipped is not None:
                        if t.get("skipped") != skipped:
                            continue

                    all_thoughts.append(t)

                    if limit and len(all_thoughts) >= limit:
                        return all_thoughts

        return all_thoughts

    def get_recent_thoughts(self, canonical_project, limit=20):
        """Get recent thoughts for a project (for deduplication)."""
        return self.get_thoughts(
            canonical_project=canonical_project, limit=limit, order_desc=True
        )

    def update_thought(self, thought_id, updates):
        """Update a thought in its JSONL file by ID."""
        # Extract date from ID to find the right file
        date_str = thought_id[:10] if len(thought_id) >= 10 else None
        if not date_str:
            return

        filepath = self.thoughts_dir / f"{date_str}.jsonl"
        if not filepath.exists():
            # Search all files
            for fp in self.thoughts_dir.glob("*.jsonl"):
                if self._update_in_file(fp, thought_id, updates):
                    return
            return

        self._update_in_file(filepath, thought_id, updates)

    def _update_in_file(self, filepath, thought_id, updates):
        """Update a thought within a specific JSONL file."""
        lines = []
        found = False
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                    if t.get("id") == thought_id:
                        t.update(updates)
                        found = True
                    lines.append(json.dumps(t, default=str))
                except json.JSONDecodeError:
                    lines.append(line)

        if found:
            with open(filepath, "w") as f:
                f.write("\n".join(lines) + "\n")
        return found

    # ─── Knowledge Pages ───

    def get_page(self, slug):
        """Read a knowledge page. Returns (content, version) or (None, 0)."""
        filepath = self.projects_dir / f"{slug}.md"
        if filepath.exists():
            content = _safe_read(filepath)
            # Extract version from a hidden comment at the end
            version = 1
            version_match = re.search(r'<!-- version: (\d+) -->', content)
            if version_match:
                version = int(version_match.group(1))
            return content, version
        return None, 0

    def save_page(self, slug, content, version):
        """Write a knowledge page. Appends version as hidden comment."""
        filepath = self.projects_dir / f"{slug}.md"

        # Strip old version comment if present
        content = re.sub(r'\n<!-- version: \d+ -->\s*$', '', content)
        # Add new version comment
        content = content.rstrip() + f"\n<!-- version: {version} -->\n"

        _safe_write(filepath, content)
        return True

    def get_all_pages(self):
        """Read all knowledge pages. Returns list of {slug, content, version}."""
        pages = []
        for filepath in sorted(self.projects_dir.glob("*.md")):
            slug = filepath.stem
            if slug in ("status", "cross-cutting", "me", "ideas"):
                continue
            content = _safe_read(filepath)
            version = 1
            version_match = re.search(r'<!-- version: (\d+) -->', content)
            if version_match:
                version = int(version_match.group(1))
            pages.append({"slug": slug, "content": content, "version": version})
        return pages

    # ─── Aliases ───

    def get_aliases(self):
        """Read all project aliases. Returns list of {alias, canonical_slug}."""
        if self.aliases_file.exists():
            with open(self.aliases_file) as f:
                return json.load(f)
        return []

    def save_alias(self, alias, canonical_slug):
        """Add or update a project alias."""
        aliases = self.get_aliases()
        # Check if alias already exists
        for a in aliases:
            if a["alias"].lower() == alias.lower():
                a["canonical_slug"] = canonical_slug
                break
        else:
            aliases.append({"alias": alias, "canonical_slug": canonical_slug})

        with open(self.aliases_file, "w") as f:
            json.dump(aliases, f, indent=2)

    # ─── Status & Sync ───

    def write_status(self, content):
        """Write the status.md overview file."""
        (self.base_dir / "status.md").write_text(content)

    def write_cross_cutting(self, content):
        """Write the cross-cutting.md file."""
        (self.base_dir / "cross-cutting.md").write_text(content)

    # ─── State Management ───

    def load_state(self):
        """Load ingestion state (processed sessions, etc)."""
        if self.state_file.exists():
            with open(self.state_file) as f:
                return json.load(f)
        return {"processed_sessions": {}, "last_cross_reference": 0}

    def save_state(self, state):
        """Save ingestion state."""
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)
