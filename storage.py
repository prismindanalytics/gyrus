"""
Storage adapters for Gyrus.
Default: MarkdownStorage — pure local files on a plain filesystem.
Cross-machine sync is handled by git (see `gyrus init`).
Optional: NotionStorage — Notion API (storage_notion.py).
"""

import json
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SPECIAL_PAGE_SLUGS = frozenset({"me", "ideas"})
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _validate_slug(slug):
    """Return a safe, portable page slug or raise ``ValueError``.

    Slugs can originate in model output or synced alias/config files, so they
    must not be allowed to select an arbitrary filesystem path.
    """
    if not isinstance(slug, str) or not slug or len(slug) > 128:
        raise ValueError("project slug must be a non-empty string of at most 128 characters")
    if not slug[0].isalnum() or not slug[-1].isalnum():
        raise ValueError(f"unsafe project slug: {slug!r}")
    if any(not (char.isalnum() or char in "-_") for char in slug):
        raise ValueError(f"unsafe project slug: {slug!r}")
    if slug.casefold() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"reserved project slug: {slug!r}")
    return slug


def _assert_contained_path(path, root):
    """Reject path traversal and symlinks below a trusted storage root."""
    path = Path(path)
    root = Path(root).resolve(strict=True)
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise ValueError(f"storage path escapes base directory: {path}") from exc

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"refusing symlink in managed storage path: {current}")

    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError(f"storage path escapes base directory: {path}") from exc
    return path


def _safe_write(path, content, root=None):
    """Atomically write UTF-8 text without following a destination symlink."""
    path = Path(path)
    if root is not None:
        _assert_contained_path(path, root)
    path.parent.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    if root is not None:
        _assert_contained_path(path.parent, root)
        os.chmod(path.parent, _PRIVATE_DIR_MODE)

    fd = None
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, _PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # os.replace replaces a destination symlink itself rather than writing
        # through it, closing the check/write race present in plain open(...).
        os.replace(tmp_path, path)
        tmp_path = None
        os.chmod(path, _PRIVATE_FILE_MODE)
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _safe_read(path, root=None):
    """Read UTF-8 text without following a managed-file symlink."""
    path = Path(path)
    if root is not None:
        _assert_contained_path(path, root)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"managed storage path is not a private regular file: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = None
            return handle.read()
    finally:
        if fd is not None:
            os.close(fd)


def _safe_append(path, content, root=None):
    """Append one UTF-8 record with O_APPEND and private permissions."""
    path = Path(path)
    if root is not None:
        _assert_contained_path(path, root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, _PRIVATE_FILE_MODE)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"managed storage path is not a private regular file: {path}")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, _PRIVATE_FILE_MODE)
        data = content.encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError("short write while appending storage record")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _set_private_file_mode(path, root):
    """Harden an existing regular file while refusing managed symlinks."""
    path = _assert_contained_path(path, root)
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"managed storage path is not a private regular file: {path}")
        os.chmod(path, _PRIVATE_FILE_MODE)


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
        self.base_dir = Path(base_dir or os.path.expanduser("~/.gyrus")).expanduser()
        if self.base_dir.exists() and not (self.base_dir.is_dir() or self.base_dir.is_symlink()):
            raise ValueError(f"storage base is not a directory: {self.base_dir}")
        self.base_dir.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)

        # Resolve the one supported symlink boundary (`~/.gyrus` itself) once,
        # then keep every managed child anchored to that canonical directory.
        self._root_dir = self.base_dir.resolve(strict=True)
        if self._root_dir == Path(self._root_dir.anchor):
            raise ValueError("refusing to use a filesystem root as Gyrus storage")
        # ``Path.home()`` can raise on deliberately minimal Windows service
        # environments where HOME/USERPROFILE is unset. An explicit
        # ``base_dir`` is still safe to use there; only apply the home-dir
        # guard when the platform can resolve a home directory.
        try:
            home_dir = Path.home().resolve()
        except RuntimeError:
            home_dir = None
        if home_dir is not None and self._root_dir == home_dir:
            raise ValueError("refusing to use the home directory itself as Gyrus storage")
        os.chmod(self._root_dir, _PRIVATE_DIR_MODE)

        self.thoughts_dir = self._root_dir / "thoughts"
        self.projects_dir = self._root_dir / "projects"
        self.aliases_file = self._root_dir / "aliases.json"
        self.state_file = self._root_dir / ".ingest-state.json"

        # Managed subdirectories must be real directories, never redirects.
        for directory in (self.thoughts_dir, self.projects_dir):
            _assert_contained_path(directory, self._root_dir)
            directory.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(f"managed storage path is not a real directory: {directory}")
            os.chmod(directory, _PRIVATE_DIR_MODE)

        # Migrate permissions for existing sensitive data. Deliberately limit
        # this to known Gyrus data files rather than touching code or `.git`.
        for filename in (
            ".env", "config.json", "aliases.json", ".ingest-state.json",
            "status.md", "cross-cutting.md", "me.md", "ideas.md",
            "runs.jsonl", "latest-digest.md", "ingest.log",
            ".notion-state.json", ".notion-thought-cache.json",
        ):
            _set_private_file_mode(self._root_dir / filename, self._root_dir)
        for directory, pattern in (
            (self.thoughts_dir, "*.jsonl"),
            (self.projects_dir, "*.md"),
        ):
            for path in directory.glob(pattern):
                _set_private_file_mode(path, self._root_dir)

    def _page_path(self, slug, *, legacy=False):
        slug = _validate_slug(slug)
        if slug in _SPECIAL_PAGE_SLUGS and not legacy:
            path = self._root_dir / f"{slug}.md"
        else:
            path = self.projects_dir / f"{slug}.md"
        return _assert_contained_path(path, self._root_dir)

    def _thought_path(self, date_str):
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid thought date: {date_str!r}") from exc
        return _assert_contained_path(
            self.thoughts_dir / f"{date_str}.jsonl", self._root_dir
        )

    # ─── Thoughts ───

    def save_thought(self, thought):
        """Append a thought to the daily JSONL file."""
        dt = thought.get("created_at", datetime.now(timezone.utc).isoformat())
        # Parse the date for the filename
        if isinstance(dt, str):
            date_str = dt[:10]  # YYYY-MM-DD
        else:
            date_str = dt.strftime("%Y-%m-%d")

        # Stable event IDs make crash recovery and cross-machine ingestion
        # idempotent. Identical content from a different tool/session remains a
        # distinct event because source and session_id participate in the hash.
        if "id" not in thought:
            import hashlib
            identity = json.dumps({
                "content": thought.get("content", ""),
                "source": thought.get("source", ""),
                "session_id": thought.get("session_id", ""),
                "project": thought.get("project"),
                "kind": thought.get("kind", "project"),
            }, sort_keys=True, ensure_ascii=False)
            content_hash = hashlib.sha256(identity.encode()).hexdigest()[:20]
            thought["id"] = f"{date_str}-{content_hash}"

        filepath = self._thought_path(date_str)
        if filepath.exists():
            existing_text = _safe_read(filepath, root=self._root_dir)
            for line in existing_text.splitlines():
                try:
                    if json.loads(line).get("id") == thought["id"]:
                        return thought["id"]
                except json.JSONDecodeError:
                    continue
        for _attempt in range(3):
            try:
                _safe_append(
                    filepath,
                    json.dumps(thought, default=str) + "\n",
                    root=self._root_dir,
                )
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
            kind = thought.get("kind", "project")
            if kind not in ("project", "idea", "meta"):
                kind = "project"
            row = {
                "content": thought["content"],
                "source": source,
                "session_id": session_id,
                "project": thought.get("project"),
                "canonical_project": thought.get("canonical_project"),
                "tags": thought.get("tags", []),
                "kind": kind,
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
        """Read thoughts from JSONL files with optional filters."""
        all_thoughts = []
        jsonl_files = sorted(self.thoughts_dir.glob("*.jsonl"),
                             reverse=order_desc)

        for filepath in jsonl_files:
            text = _safe_read(filepath, root=self._root_dir)
            for line in text.splitlines():
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
        date_str = thought_id[:10] if isinstance(thought_id, str) and len(thought_id) >= 10 else None
        try:
            filepath = self._thought_path(date_str) if date_str else None
        except ValueError:
            filepath = None
        if filepath is None or not filepath.exists():
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
        text = _safe_read(filepath, root=self._root_dir)
        for line in text.splitlines():
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
            _safe_write(
                filepath, "\n".join(lines) + "\n", root=self._root_dir
            )
        return found

    # ─── Knowledge Pages ───

    def get_page(self, slug):
        """Read a knowledge page. Returns (content, version) or (None, 0).

        ``me.md`` and ``ideas.md`` live at the documented storage root. For
        installations created by older releases, a legacy copy under
        ``projects/`` remains readable until the next save migrates it.
        """
        filepath = self._page_path(slug)
        if slug in _SPECIAL_PAGE_SLUGS and not filepath.exists():
            legacy_path = self._page_path(slug, legacy=True)
            if legacy_path.exists():
                filepath = legacy_path
        if filepath.exists():
            content = _safe_read(filepath, root=self._root_dir)
            # Extract version from a hidden comment at the end
            version = 1
            version_match = re.search(r'<!-- version: (\d+) -->', content)
            if version_match:
                version = int(version_match.group(1))
            return content, version
        return None, 0

    def save_page(self, slug, content, version):
        """Write a knowledge page. Appends version as hidden comment."""
        filepath = self._page_path(slug)

        # Strip old version comment if present
        content = re.sub(r'\n<!-- version: \d+ -->\s*$', '', content)
        # Add new version comment
        content = content.rstrip() + f"\n<!-- version: {version} -->\n"

        _safe_write(filepath, content, root=self._root_dir)
        return True

    def get_all_pages(self):
        """Read all knowledge pages. Returns list of {slug, content, version}."""
        pages = []
        for filepath in sorted(self.projects_dir.glob("*.md")):
            slug = filepath.stem
            if slug in ("status", "cross-cutting", "me", "ideas"):
                continue
            _validate_slug(slug)
            content = _safe_read(filepath, root=self._root_dir)
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
            try:
                aliases = json.loads(
                    _safe_read(self.aliases_file, root=self._root_dir)
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                # An interrupted edit or an older manually-created file should
                # not prevent ingestion. The next save will replace it with a
                # valid atomic JSON document.
                return []
            if not isinstance(aliases, list):
                return []
            valid_aliases = []
            for row in aliases:
                if not isinstance(row, dict) or not isinstance(row.get("alias"), str):
                    continue
                try:
                    _validate_slug(row.get("canonical_slug"))
                except ValueError:
                    continue
                valid_aliases.append(row)
            return valid_aliases
        return []

    def save_alias(self, alias, canonical_slug):
        """Add or update a project alias."""
        if not isinstance(alias, str) or not alias:
            raise ValueError("alias must be a non-empty string")
        canonical_slug = _validate_slug(canonical_slug)
        aliases = self.get_aliases()
        # Check if alias already exists
        for a in aliases:
            if a["alias"].lower() == alias.lower():
                a["canonical_slug"] = canonical_slug
                break
        else:
            aliases.append({"alias": alias, "canonical_slug": canonical_slug})

        _safe_write(
            self.aliases_file,
            json.dumps(aliases, indent=2) + "\n",
            root=self._root_dir,
        )

    # ─── Status & Sync ───

    def write_status(self, content):
        """Write the status.md overview file."""
        _safe_write(
            self._root_dir / "status.md", content, root=self._root_dir
        )

    def write_cross_cutting(self, content):
        """Write the cross-cutting.md file."""
        _safe_write(
            self._root_dir / "cross-cutting.md", content, root=self._root_dir
        )

    # ─── State Management ───

    def load_state(self):
        """Load ingestion state (processed sessions, etc)."""
        if self.state_file.exists():
            try:
                state = json.loads(_safe_read(self.state_file, root=self._root_dir))
                if isinstance(state, dict):
                    return state
            except (OSError, ValueError, json.JSONDecodeError):
                # A truncated state file should not permanently stop ingestion.
                # The next successful run will rewrite it atomically.
                pass
        return {"processed_sessions": {}, "last_cross_reference": 0}

    def save_state(self, state):
        """Save ingestion state."""
        _safe_write(
            self.state_file,
            json.dumps(state, indent=2) + "\n",
            root=self._root_dir,
        )
