"""
Notion storage adapter for Gyrus.
Stores knowledge pages, thoughts, and aliases in Notion databases via the API.
Uses only urllib (no external dependencies).
"""

import hashlib
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from storage import _safe_read, _safe_write


NOTION_API = "https://api.notion.com/v1/"
NOTION_VERSION = "2022-06-28"
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0  # seconds, doubles each retry
REQUEST_TIMEOUT = 30  # seconds, applied to connect and socket reads
MAX_RETRY_DELAY = 30  # never let a server-supplied Retry-After hang a run

_THOUGHT_KINDS = ("project", "idea", "meta")


def _kind_select_schema():
    return {"select": {"options": [
        {"name": "project", "color": "blue"},
        {"name": "idea", "color": "yellow"},
        {"name": "meta", "color": "green"},
    ]}}


def _notion_request(method, endpoint, notion_key, data=None, timeout=REQUEST_TIMEOUT):
    """Make a Notion API request with rate-limit retry."""
    url = NOTION_API + endpoint.lstrip("/")
    headers = {
        "Authorization": f"Bearer {notion_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode("utf-8") if data else None

    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    retry_after = float(
                        e.headers.get("Retry-After", RETRY_BACKOFF * (2 ** attempt))
                    )
                except (TypeError, ValueError):
                    retry_after = RETRY_BACKOFF * (2 ** attempt)
                retry_after = max(0, min(retry_after, MAX_RETRY_DELAY))
                if attempt < MAX_RETRIES - 1:
                    time.sleep(retry_after)
                    continue
                break
            # Read error body for debugging
            try:
                err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            except Exception:
                err_body = ""
            raise RuntimeError(f"Notion API {method} {endpoint} → {e.code}: {err_body}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(RETRY_BACKOFF * (2 ** attempt), MAX_RETRY_DELAY))
                continue
            raise RuntimeError(
                f"Notion API {method} {endpoint} failed after {MAX_RETRIES} attempts: {e}"
            ) from e
    raise RuntimeError(f"Notion API rate-limited after {MAX_RETRIES} retries: {method} {endpoint}")


def _rich_text(text):
    """Create a Notion rich_text array from a plain string."""
    if not text:
        return []
    # Notion rich_text content max is 2000 chars per element
    chunks = []
    for i in range(0, len(text), 2000):
        chunks.append({"type": "text", "text": {"content": text[i:i + 2000]}})
    return chunks


def _plain_text(rich_text_array):
    """Extract plain text from a Notion rich_text array."""
    if not rich_text_array:
        return ""
    return "".join(rt.get("plain_text", "") for rt in rich_text_array)


def _paragraph_blocks(content):
    """Split content into paragraph blocks (max 2000 chars each)."""
    if not content:
        return []
    blocks = []
    # Split on double newlines for natural paragraphs, then chunk
    paragraphs = content.split("\n\n")
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # Notion block text limit is 2000 chars
        for i in range(0, len(para), 2000):
            chunk = para[i:i + 2000]
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                }
            })
    return blocks


def _read_blocks(notion_key, page_id):
    """Read all block children of a page and return concatenated text."""
    parts = []
    cursor = None
    while True:
        endpoint = f"blocks/{page_id}/children?page_size=100"
        if cursor:
            endpoint += f"&start_cursor={cursor}"
        resp = _notion_request("GET", endpoint, notion_key)
        for block in resp.get("results", []):
            btype = block.get("type", "")
            rich_text = block.get(btype, {}).get("rich_text", [])
            text = _plain_text(rich_text)
            if not text:
                continue
            prefixes = {
                "heading_1": "# ",
                "heading_2": "## ",
                "heading_3": "### ",
                "bulleted_list_item": "- ",
                "numbered_list_item": "1. ",
                "to_do": "- [ ] ",
                "quote": "> ",
            }
            parts.append(prefixes.get(btype, "") + text)
        if resp.get("has_more"):
            cursor = resp.get("next_cursor")
        else:
            break
    return "\n\n".join(parts)


# ─── Database Setup ───

def setup_notion_databases(notion_key):
    """
    Create the Gyrus Knowledge Base and Gyrus Aliases databases
    in the user's Notion workspace. Returns (kb_database_id, aliases_database_id).

    The databases are created as top-level pages (parent is the workspace).
    """
    # Search for an existing page to use as parent — use search to find workspace
    # Actually, Notion API allows creating databases with parent type "page_id"
    # but for workspace-level, we need to create a page first or use search.
    # The simplest approach: create databases at workspace level (parent: {type: "workspace"})
    # Note: workspace parent is only available for integrations with workspace-level access.

    # Create Knowledge Base database
    kb_schema = {
        "parent": {"type": "workspace", "workspace": True},
        "is_inline": False,
        "title": [{"type": "text", "text": {"content": "Gyrus Knowledge Base"}}],
        "properties": {
            "Title": {"title": {}},
            "Type": {"select": {"options": [
                {"name": "project", "color": "blue"},
                {"name": "thought", "color": "yellow"},
                {"name": "me", "color": "green"},
                {"name": "ideas", "color": "yellow"},
                {"name": "status", "color": "gray"},
            ]}},
            "Version": {"number": {"format": "number"}},
            "Source": {"select": {"options": [
                {"name": "claude-code", "color": "blue"},
                {"name": "cowork", "color": "purple"},
                {"name": "codex", "color": "orange"},
            ]}},
            "Project": {"select": {"options": []}},
            "Kind": _kind_select_schema(),
            "Tags": {"multi_select": {"options": []}},
            "Machine": {"rich_text": {}},
            "Session ID": {"rich_text": {}},
            "Processed": {"checkbox": {}},
            "Merged Into": {"rich_text": {}},
            "Skipped": {"checkbox": {}},
            "Skip Reason": {"rich_text": {}},
            "Created": {"date": {}},
            "Occurred": {"date": {}},
        }
    }
    kb_resp = _notion_request("POST", "databases", notion_key, kb_schema)
    kb_id = kb_resp["id"]

    # Create Aliases database
    aliases_schema = {
        "parent": {"type": "workspace", "workspace": True},
        "is_inline": False,
        "title": [{"type": "text", "text": {"content": "Gyrus Aliases"}}],
        "properties": {
            "Alias": {"title": {}},
            "Canonical Slug": {"rich_text": {}},
        }
    }
    aliases_resp = _notion_request("POST", "databases", notion_key, aliases_schema)
    aliases_id = aliases_resp["id"]

    return kb_id, aliases_id


# ─── NotionStorage Class ───

class NotionStorage:
    """
    Notion-backed storage for Gyrus. Implements the same interface as MarkdownStorage
    but persists everything in Notion databases via the API.

    Constructor:
        NotionStorage(notion_key, database_id, aliases_database_id=None)

    - notion_key: Notion integration token
    - database_id: ID of the "Gyrus Knowledge Base" database
    - aliases_database_id: ID of the "Gyrus Aliases" database (optional)
    """

    def __init__(self, notion_key, database_id, aliases_database_id=None):
        self.notion_key = notion_key
        self.database_id = database_id
        self.aliases_database_id = aliases_database_id
        self._kind_property_ready = None

        # Local state directory
        configured_state_dir = Path(os.path.expanduser("~/.gyrus"))
        configured_state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._state_root = configured_state_dir.resolve(strict=True)
        # Use the canonical path for safe-file helpers; the installer commonly
        # makes ~/.gyrus a symlink to a local storage directory.
        self.state_dir = self._state_root
        os.chmod(self._state_root, 0o700)
        self.state_file = self.state_dir / ".notion-state.json"

        # Keep the filesystem-shaped attributes exposed by MarkdownStorage so
        # shared ingest paths (including merge quarantine/debug output) work
        # with either backend.
        self.base_dir = self.state_dir
        self.projects_dir = self.state_dir / "projects"
        self.projects_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.projects_dir, 0o700)
        self.aliases_file = self.state_dir / "aliases.json"

        # Local thought cache for dedup (avoids slow Notion queries)
        self._thought_cache_file = self.state_dir / ".notion-thought-cache.json"
        self._thought_cache = self._load_thought_cache()

    # ─── Internal helpers ───

    def _req(self, method, endpoint, data=None):
        return _notion_request(method, endpoint, self.notion_key, data)

    def _ensure_kind_property(self):
        """Add Kind and Occurred properties to older Gyrus databases.

        Schema migration is best-effort: if an integration can write pages but
        cannot alter the database schema, kind still survives in the local
        cache instead of making all thought writes fail.
        """
        if self._kind_property_ready is not None:
            return self._kind_property_ready
        try:
            database = self._req("GET", f"databases/{self.database_id}")
            existing = database.get("properties", {})
            additions = {}
            if "Kind" not in existing:
                additions["Kind"] = _kind_select_schema()
            if "Occurred" not in existing:
                additions["Occurred"] = {"date": {}}
            if additions:
                self._req("PATCH", f"databases/{self.database_id}", {
                    "properties": additions,
                })
            self._kind_property_ready = True
        except RuntimeError:
            self._kind_property_ready = False
        return self._kind_property_ready

    def _load_thought_cache(self):
        if self._thought_cache_file.exists():
            try:
                value = json.loads(_safe_read(
                    self._thought_cache_file, root=self._state_root
                ))
                return value if isinstance(value, list) else []
            except (json.JSONDecodeError, IOError, OSError, ValueError):
                pass
        return []

    def _save_thought_cache(self):
        _safe_write(
            self._thought_cache_file,
            json.dumps(self._thought_cache, ensure_ascii=False) + "\n",
            root=self._state_root,
        )

    @staticmethod
    def _thought_fingerprint(thought):
        identity = json.dumps({
            "content": thought.get("content", ""),
            "source": thought.get("source", ""),
            "session_id": thought.get("session_id", ""),
            "project": thought.get("project"),
            "kind": thought.get("kind", "project"),
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

    def _query_database(self, filter_obj=None, sorts=None, page_size=100, start_cursor=None):
        """Query the main knowledge base database."""
        body = {"page_size": page_size}
        if filter_obj:
            body["filter"] = filter_obj
        if sorts:
            body["sorts"] = sorts
        if start_cursor:
            body["start_cursor"] = start_cursor
        return self._req("POST", f"databases/{self.database_id}/query", body)

    def _query_all(self, filter_obj=None, sorts=None, limit=None):
        """Query with pagination, returns all matching pages."""
        results = []
        cursor = None
        while True:
            resp = self._query_database(filter_obj, sorts, page_size=100, start_cursor=cursor)
            results.extend(resp.get("results", []))
            if limit and len(results) >= limit:
                return results[:limit]
            if resp.get("has_more"):
                cursor = resp.get("next_cursor")
            else:
                break
        return results

    def _create_page(self, properties, children=None):
        """Create a page in the main database."""
        body = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }
        if children:
            # Notion limits children to 100 blocks per create
            body["children"] = children[:100]
        resp = self._req("POST", "pages", body)
        page_id = resp["id"]
        # Append remaining blocks if > 100
        if children and len(children) > 100:
            for i in range(100, len(children), 100):
                batch = children[i:i + 100]
                self._req("PATCH", f"blocks/{page_id}/children", {"children": batch})
        return resp

    def _update_page(self, page_id, properties):
        """Update page properties."""
        return self._req("PATCH", f"pages/{page_id}", {"properties": properties})

    def _replace_page_content(self, page_id, content):
        """Replace all block children of a page with new content."""
        # Delete ALL existing blocks (paginate past 100-block limit)
        block_ids = []
        has_more = True
        start_cursor = None
        while has_more:
            url = f"blocks/{page_id}/children?page_size=100"
            if start_cursor:
                url += f"&start_cursor={start_cursor}"
            resp = self._req("GET", url)
            for block in resp.get("results", []):
                if block.get("id"):
                    block_ids.append(block["id"])
            has_more = resp.get("has_more", False)
            start_cursor = resp.get("next_cursor")

        for block_id in block_ids:
            try:
                self._req("DELETE", f"blocks/{block_id}")
            except RuntimeError:
                pass  # block may already be gone

        # Then append new blocks
        blocks = _paragraph_blocks(content)
        for i in range(0, len(blocks), 100):
            batch = blocks[i:i + 100]
            self._req("PATCH", f"blocks/{page_id}/children", {"children": batch})

    def _page_to_thought(self, page):
        """Convert a Notion page to a thought dict."""
        props = page.get("properties", {})
        kind = (props.get("Kind", {}).get("select") or {}).get("name")
        return {
            "id": page["id"],
            "content": _plain_text(props.get("Title", {}).get("title", [])),
            "source": (props.get("Source", {}).get("select") or {}).get("name", ""),
            "session_id": _plain_text(props.get("Session ID", {}).get("rich_text", [])),
            "project": (props.get("Project", {}).get("select") or {}).get("name", ""),
            "canonical_project": (props.get("Project", {}).get("select") or {}).get("name", ""),
            "kind": kind if kind in _THOUGHT_KINDS else None,
            "tags": [t["name"] for t in props.get("Tags", {}).get("multi_select", [])],
            "processed": props.get("Processed", {}).get("checkbox", False),
            "merged_into_page": _plain_text(props.get("Merged Into", {}).get("rich_text", [])) or None,
            "skipped": props.get("Skipped", {}).get("checkbox", False),
            "skip_reason": _plain_text(props.get("Skip Reason", {}).get("rich_text", [])) or None,
            "machine": _plain_text(props.get("Machine", {}).get("rich_text", [])),
            "created_at": (props.get("Created", {}).get("date") or {}).get("start", ""),
            "occurred_at": (props.get("Occurred", {}).get("date") or {}).get("start"),
        }

    def _thought_properties(self, thought):
        """Build Notion properties dict from a thought dict."""
        title_text = (thought.get("content") or "")[:100]
        props = {
            "Title": {"title": _rich_text(title_text)},
            "Type": {"select": {"name": "thought"}},
            "Source": {"select": {"name": thought.get("source", "claude-code")}},
            "Session ID": {"rich_text": _rich_text(thought.get("session_id", ""))},
            "Processed": {"checkbox": thought.get("processed", False)},
            "Skipped": {"checkbox": thought.get("skipped", False)},
        }
        kind = thought.get("kind", "project")
        if kind not in _THOUGHT_KINDS:
            kind = "project"
        props["Kind"] = {"select": {"name": kind}}
        if thought.get("canonical_project") or thought.get("project"):
            proj = thought.get("canonical_project") or thought.get("project")
            if proj:
                props["Project"] = {"select": {"name": proj}}
        if thought.get("tags"):
            props["Tags"] = {"multi_select": [{"name": t} for t in thought["tags"]]}
        if thought.get("machine"):
            props["Machine"] = {"rich_text": _rich_text(thought["machine"])}
        if thought.get("merged_into_page"):
            props["Merged Into"] = {"rich_text": _rich_text(thought["merged_into_page"])}
        if thought.get("skip_reason"):
            props["Skip Reason"] = {"rich_text": _rich_text(thought["skip_reason"])}
        if thought.get("created_at"):
            created = thought["created_at"]
            if isinstance(created, str) and len(created) >= 10:
                props["Created"] = {"date": {"start": created}}
        if thought.get("occurred_at"):
            occurred = thought["occurred_at"]
            if isinstance(occurred, str) and len(occurred) >= 10:
                props["Occurred"] = {"date": {"start": occurred}}
        return props

    # ─── Thoughts ───

    def save_thought(self, thought):
        """Save a single thought as a Notion page."""
        fingerprint = self._thought_fingerprint(thought)
        for cached in self._thought_cache:
            cached_fingerprint = cached.get("_fingerprint")
            if not cached_fingerprint:
                cached_fingerprint = self._thought_fingerprint(cached)
            if cached_fingerprint == fingerprint and cached.get("id"):
                thought["id"] = cached["id"]
                return cached["id"]
        if "id" not in thought:
            dt = thought.get("created_at", datetime.now(timezone.utc).isoformat())
            date_str = dt[:10] if isinstance(dt, str) else dt.strftime("%Y-%m-%d")
            thought["id"] = f"{date_str}-{fingerprint}"

        props = self._thought_properties(thought)
        if not self._ensure_kind_property():
            props.pop("Kind", None)
            props.pop("Occurred", None)
        content = thought.get("content", "")

        # Full content goes in the page body
        children = _paragraph_blocks(content) if content else []
        resp = self._create_page(props, children)
        notion_id = resp["id"]

        # Update local cache
        cached = dict(thought)
        cached["id"] = notion_id
        cached["_fingerprint"] = fingerprint
        self._thought_cache.append(cached)
        self._save_thought_cache()

        return notion_id

    def save_thoughts(self, thoughts, source, session_id, session_date=None, machine=None):
        """Save a batch of extracted thoughts."""
        ids = []
        for thought in thoughts:
            kind = thought.get("kind", "project")
            if kind not in _THOUGHT_KINDS:
                kind = "project"
            row = {
                "content": thought["content"],
                "source": source,
                "session_id": session_id,
                "project": thought.get("project"),
                "canonical_project": thought.get("canonical_project"),
                "kind": kind,
                "tags": thought.get("tags", []),
                "processed": False,
                "merged_into_page": None,
                "skipped": thought.get("skipped", False),
                "skip_reason": thought.get("skip_reason"),
                "machine": machine,
                "created_at": session_date or datetime.now(timezone.utc).isoformat(),
                "occurred_at": thought.get("occurred_at"),
            }
            tid = self.save_thought(row)
            thought["id"] = tid
            ids.append(tid)
        return ids

    def get_thoughts(self, canonical_project=None, merged=None, processed=None,
                     skipped=None, limit=None, order_desc=True):
        """Query thoughts from Notion with optional filters."""
        filters = [{"property": "Type", "select": {"equals": "thought"}}]

        if canonical_project is not None:
            filters.append({"property": "Project", "select": {"equals": canonical_project}})
        if processed is not None:
            filters.append({"property": "Processed", "checkbox": {"equals": processed}})
        if skipped is not None:
            filters.append({"property": "Skipped", "checkbox": {"equals": skipped}})
        if merged is True:
            filters.append({"property": "Merged Into", "rich_text": {"is_not_empty": True}})
        elif merged is False:
            filters.append({"property": "Merged Into", "rich_text": {"is_empty": True}})

        filter_obj = {"and": filters} if len(filters) > 1 else filters[0]
        sorts = [{"property": "Created", "direction": "descending" if order_desc else "ascending"}]

        pages = self._query_all(filter_obj, sorts, limit=limit)
        thoughts = []
        for page in pages:
            t = self._page_to_thought(page)
            if not t.get("kind"):
                cached = next(
                    (row for row in self._thought_cache
                     if row.get("id") == t.get("id")),
                    None,
                )
                t["kind"] = (cached or {}).get("kind", "project")
            # Fetch full content from body if title was truncated
            body_content = _read_blocks(self.notion_key, page["id"])
            if body_content:
                t["content"] = body_content
            thoughts.append(t)
        return thoughts

    def get_recent_thoughts(self, canonical_project, limit=20):
        """Get recent thoughts for a project (for deduplication). Uses local cache first."""
        # Try local cache for speed
        cached = [
            t for t in self._thought_cache
            if t.get("canonical_project") == canonical_project
        ]
        cached.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        if cached:
            return cached[:limit]
        # Fallback to Notion query
        return self.get_thoughts(canonical_project=canonical_project, limit=limit, order_desc=True)

    def update_thought(self, thought_id, updates):
        """Update a thought's properties by its Notion page ID."""
        props = {}
        if "processed" in updates:
            props["Processed"] = {"checkbox": updates["processed"]}
        if "merged_into_page" in updates:
            val = updates["merged_into_page"] or ""
            props["Merged Into"] = {"rich_text": _rich_text(val)}
        if "skipped" in updates:
            props["Skipped"] = {"checkbox": updates["skipped"]}
        if "skip_reason" in updates:
            val = updates["skip_reason"] or ""
            props["Skip Reason"] = {"rich_text": _rich_text(val)}
        if "canonical_project" in updates:
            val = updates["canonical_project"]
            if val:
                props["Project"] = {"select": {"name": val}}
        if "tags" in updates:
            props["Tags"] = {"multi_select": [{"name": t} for t in updates["tags"]]}
        if "kind" in updates:
            kind = updates["kind"]
            if kind not in _THOUGHT_KINDS:
                raise ValueError(f"unsupported thought kind: {kind!r}")
            if self._ensure_kind_property():
                props["Kind"] = {"select": {"name": kind}}

        if props:
            self._update_page(thought_id, props)

        # Update local cache
        for t in self._thought_cache:
            if t.get("id") == thought_id:
                t.update(updates)
                break
        self._save_thought_cache()

    # ─── Knowledge Pages ───

    def get_page(self, slug):
        """Read a knowledge page by slug. Returns (content, version) or (None, 0)."""
        filter_obj = {
            "and": [
                {"property": "Title", "title": {"equals": slug}},
                {"property": "Type", "select": {"equals": "project"}},
            ]
        }
        resp = self._query_database(filter_obj, page_size=1)
        results = resp.get("results", [])
        if not results:
            # Also check for special pages created by newer and older releases.
            for page_type in ("me", "ideas"):
                filter_obj["and"][1] = {
                    "property": "Type", "select": {"equals": page_type}
                }
                resp = self._query_database(filter_obj, page_size=1)
                results = resp.get("results", [])
                if results:
                    break

        if not results:
            return None, 0

        page = results[0]
        props = page.get("properties", {})
        version = props.get("Version", {}).get("number") or 1
        content = _read_blocks(self.notion_key, page["id"])
        return content, version

    def save_page(self, slug, content, version):
        """Write a knowledge page. Creates or updates."""
        # Check if page exists
        filter_obj = {
            "and": [
                {"property": "Title", "title": {"equals": slug}},
                {"or": [
                    {"property": "Type", "select": {"equals": "project"}},
                    {"property": "Type", "select": {"equals": "me"}},
                    {"property": "Type", "select": {"equals": "ideas"}},
                ]},
            ]
        }
        resp = self._query_database(filter_obj, page_size=1)
        results = resp.get("results", [])

        # Keep ideas pages compatible with databases created before the
        # dedicated ``ideas`` select option existed; the title already
        # distinguishes the special page.
        page_type = "me" if slug == "me" else "project"

        if results:
            # Update existing page
            page_id = results[0]["id"]
            self._update_page(page_id, {
                "Version": {"number": version},
            })
            self._replace_page_content(page_id, content)
        else:
            # Create new page
            props = {
                "Title": {"title": _rich_text(slug)},
                "Type": {"select": {"name": page_type}},
                "Version": {"number": version},
            }
            children = _paragraph_blocks(content)
            self._create_page(props, children)

        return True

    def get_all_pages(self):
        """Read all knowledge pages. Returns list of {slug, content, version}."""
        filter_obj = {
            "or": [
                {"property": "Type", "select": {"equals": "project"}},
            ]
        }
        pages_raw = self._query_all(filter_obj)

        pages = []
        for page in pages_raw:
            props = page.get("properties", {})
            slug = _plain_text(props.get("Title", {}).get("title", []))
            if slug in ("status", "cross-cutting", "me", "ideas"):
                continue
            version = props.get("Version", {}).get("number") or 1
            content = _read_blocks(self.notion_key, page["id"])
            pages.append({"slug": slug, "content": content, "version": version})

        return pages

    # ─── Aliases ───

    def get_aliases(self):
        """Read all project aliases from the aliases database."""
        if not self.aliases_database_id:
            return []

        results = []
        cursor = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            resp = self._req("POST", f"databases/{self.aliases_database_id}/query", body)
            for page in resp.get("results", []):
                props = page.get("properties", {})
                alias = _plain_text(props.get("Alias", {}).get("title", []))
                canonical = _plain_text(props.get("Canonical Slug", {}).get("rich_text", []))
                if alias:
                    results.append({"alias": alias, "canonical_slug": canonical})
            if resp.get("has_more"):
                cursor = resp.get("next_cursor")
            else:
                break
        return results

    def save_alias(self, alias, canonical_slug):
        """Add or update a project alias in the aliases database."""
        if not self.aliases_database_id:
            return

        # Check if alias exists
        filter_obj = {"property": "Alias", "title": {"equals": alias}}
        body = {"page_size": 1, "filter": filter_obj}
        resp = self._req("POST", f"databases/{self.aliases_database_id}/query", body)
        results = resp.get("results", [])

        if results:
            # Update existing
            page_id = results[0]["id"]
            self._req("PATCH", f"pages/{page_id}", {
                "properties": {
                    "Canonical Slug": {"rich_text": _rich_text(canonical_slug)},
                }
            })
        else:
            # Create new
            self._req("POST", "pages", {
                "parent": {"database_id": self.aliases_database_id},
                "properties": {
                    "Alias": {"title": _rich_text(alias)},
                    "Canonical Slug": {"rich_text": _rich_text(canonical_slug)},
                }
            })

    # ─── Status & Sync ───

    def write_status(self, content):
        """Write the status page (Type=status, Title=status)."""
        filter_obj = {
            "and": [
                {"property": "Title", "title": {"equals": "status"}},
                {"property": "Type", "select": {"equals": "status"}},
            ]
        }
        resp = self._query_database(filter_obj, page_size=1)
        results = resp.get("results", [])

        if results:
            page_id = results[0]["id"]
            self._replace_page_content(page_id, content)
        else:
            props = {
                "Title": {"title": _rich_text("status")},
                "Type": {"select": {"name": "status"}},
            }
            children = _paragraph_blocks(content)
            self._create_page(props, children)

    def write_cross_cutting(self, content):
        """Write the cross-cutting page (Type=status, Title=cross-cutting)."""
        filter_obj = {
            "and": [
                {"property": "Title", "title": {"equals": "cross-cutting"}},
                {"property": "Type", "select": {"equals": "status"}},
            ]
        }
        resp = self._query_database(filter_obj, page_size=1)
        results = resp.get("results", [])

        if results:
            page_id = results[0]["id"]
            self._replace_page_content(page_id, content)
        else:
            props = {
                "Title": {"title": _rich_text("cross-cutting")},
                "Type": {"select": {"name": "status"}},
            }
            children = _paragraph_blocks(content)
            self._create_page(props, children)

    # ─── State Management ───

    def load_state(self):
        """Load ingestion state from local JSON (not Notion — too slow for frequent reads)."""
        if self.state_file.exists():
            try:
                state = json.loads(_safe_read(self.state_file, root=self._state_root))
                if isinstance(state, dict):
                    return state
            except (json.JSONDecodeError, IOError, OSError, ValueError):
                pass
        return {"processed_sessions": {}, "last_cross_reference": 0}

    def save_state(self, state):
        """Save ingestion state to local JSON."""
        _safe_write(
            self.state_file,
            json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            root=self._state_root,
        )
