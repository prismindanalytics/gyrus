"""
Notion storage adapter for Gyrus.
Stores knowledge pages, thoughts, and aliases in Notion databases via the API.
Uses only urllib (no external dependencies).
"""

import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


NOTION_API = "https://api.notion.com/v1/"
NOTION_VERSION = "2022-06-28"
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0  # seconds, doubles each retry


def _notion_request(method, endpoint, notion_key, data=None):
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
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = float(e.headers.get("Retry-After", RETRY_BACKOFF * (2 ** attempt)))
                time.sleep(retry_after)
                continue
            # Read error body for debugging
            try:
                err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            except Exception:
                err_body = ""
            raise RuntimeError(f"Notion API {method} {endpoint} → {e.code}: {err_body}") from e
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
            if btype == "paragraph":
                text = _plain_text(block.get("paragraph", {}).get("rich_text", []))
                parts.append(text)
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
                {"name": "status", "color": "gray"},
            ]}},
            "Version": {"number": {"format": "number"}},
            "Source": {"select": {"options": [
                {"name": "claude-code", "color": "blue"},
                {"name": "cowork", "color": "purple"},
                {"name": "codex", "color": "orange"},
            ]}},
            "Project": {"select": {"options": []}},
            "Tags": {"multi_select": {"options": []}},
            "Machine": {"rich_text": {}},
            "Session ID": {"rich_text": {}},
            "Processed": {"checkbox": {}},
            "Merged Into": {"rich_text": {}},
            "Skipped": {"checkbox": {}},
            "Skip Reason": {"rich_text": {}},
            "Created": {"date": {}},
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

        # Local state directory
        self.state_dir = Path(os.path.expanduser("~/.gyrus"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / ".notion-state.json"

        # Local thought cache for dedup (avoids slow Notion queries)
        self._thought_cache_file = self.state_dir / ".notion-thought-cache.json"
        self._thought_cache = self._load_thought_cache()

    # ─── Internal helpers ───

    def _req(self, method, endpoint, data=None):
        return _notion_request(method, endpoint, self.notion_key, data)

    def _load_thought_cache(self):
        if self._thought_cache_file.exists():
            try:
                with open(self._thought_cache_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return []

    def _save_thought_cache(self):
        with open(self._thought_cache_file, "w") as f:
            json.dump(self._thought_cache, f)

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
        has_more = True
        start_cursor = None
        while has_more:
            url = f"blocks/{page_id}/children?page_size=100"
            if start_cursor:
                url += f"&start_cursor={start_cursor}"
            resp = self._req("GET", url)
            for block in resp.get("results", []):
                try:
                    self._req("DELETE", f"blocks/{block['id']}")
                except RuntimeError:
                    pass  # block may already be gone
            has_more = resp.get("has_more", False)
            start_cursor = resp.get("next_cursor")

        # Then append new blocks
        blocks = _paragraph_blocks(content)
        for i in range(0, len(blocks), 100):
            batch = blocks[i:i + 100]
            self._req("PATCH", f"blocks/{page_id}/children", {"children": batch})

    def _page_to_thought(self, page):
        """Convert a Notion page to a thought dict."""
        props = page.get("properties", {})
        return {
            "id": page["id"],
            "content": _plain_text(props.get("Title", {}).get("title", [])),
            "source": (props.get("Source", {}).get("select") or {}).get("name", ""),
            "session_id": _plain_text(props.get("Session ID", {}).get("rich_text", [])),
            "project": (props.get("Project", {}).get("select") or {}).get("name", ""),
            "canonical_project": (props.get("Project", {}).get("select") or {}).get("name", ""),
            "tags": [t["name"] for t in props.get("Tags", {}).get("multi_select", [])],
            "processed": props.get("Processed", {}).get("checkbox", False),
            "merged_into_page": _plain_text(props.get("Merged Into", {}).get("rich_text", [])) or None,
            "skipped": props.get("Skipped", {}).get("checkbox", False),
            "skip_reason": _plain_text(props.get("Skip Reason", {}).get("rich_text", [])) or None,
            "machine": _plain_text(props.get("Machine", {}).get("rich_text", [])),
            "created_at": (props.get("Created", {}).get("date") or {}).get("start", ""),
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
        return props

    # ─── Thoughts ───

    def save_thought(self, thought):
        """Save a single thought as a Notion page."""
        if "id" not in thought:
            dt = thought.get("created_at", datetime.now(timezone.utc).isoformat())
            date_str = dt[:10] if isinstance(dt, str) else dt.strftime("%Y-%m-%d")
            thought["id"] = f"{date_str}-{hash(thought.get('content', '')[:80]) & 0xFFFFFFFF:08x}"

        props = self._thought_properties(thought)
        content = thought.get("content", "")

        # Full content goes in the page body
        children = _paragraph_blocks(content) if content else []
        resp = self._create_page(props, children)
        notion_id = resp["id"]

        # Update local cache
        cached = dict(thought)
        cached["id"] = notion_id
        self._thought_cache.append(cached)
        self._save_thought_cache()

        return notion_id

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
            # Also check for "me" type
            filter_obj["and"][1] = {"property": "Type", "select": {"equals": "me"}}
            resp = self._query_database(filter_obj, page_size=1)
            results = resp.get("results", [])

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
                ]},
            ]
        }
        resp = self._query_database(filter_obj, page_size=1)
        results = resp.get("results", [])

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
                with open(self.state_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {"processed_sessions": {}, "last_cross_reference": 0}

    def save_state(self, state):
        """Save ingestion state to local JSON."""
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)
