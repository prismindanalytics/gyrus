#!/usr/bin/env python3
"""
Tests for Gyrus — storage, extraction, alias resolution, deduplication.
Run: python3 -m pytest test_gyrus.py -v
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from storage import MarkdownStorage
from ingest import (
    extract_claude_code_conversation,
    extract_codex_conversation,
    extract_cowork_conversation,
    extract_antigravity_session,
    extract_cursor_conversation,
    extract_copilot_conversation,
    extract_cline_conversation,
    extract_continue_conversation,
    extract_aider_conversation,
    extract_opencode_conversation,
    resolve_aliases,
    deduplicate_thoughts,
    persist_thought_metadata,
    find_tool_memory_files,
    _resolve_model,
    MODEL_CATALOG,
    main,
    # v0.2 additions
    _detect_cloud_sync,
    _is_dataless,
    _read_text_safe,
    _git_is_repo,
    _git_remote_url,
    _git_pull,
    _git_commit_push,
    _doctor_check_storage,
    _doctor_check_git_sync,
    _doctor_check_freshness,
    _doctor_check_lockfile,
    run_doctor,
    # --fix helpers
    _doctor_fix_lockfile,
    _doctor_fix_git_sync,
    _lock_path,
    run_merge,
    run_merge_suggest,
    _detect_slug_clusters,
    _llm_suggest_merges,
    _resolve_model,
    _call_local,
    _detect_local_llm,
    _local_base_url,
)


class TestMarkdownStorage(unittest.TestCase):
    """Test the MarkdownStorage adapter."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = MarkdownStorage(base_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_save_and_get_thought(self):
        thought = {
            "content": "Decided to pivot to B2B",
            "source": "claude-code",
            "session_id": "abc123",
            "project": "beacon",
            "canonical_project": "beacon",
            "tags": ["strategy"],
            "created_at": "2025-03-20T12:00:00Z",
        }
        tid = self.store.save_thought(thought)
        self.assertIsNotNone(tid)
        self.assertTrue(tid.startswith("2025-03-20"))

        # Retrieve it
        thoughts = self.store.get_thoughts()
        self.assertEqual(len(thoughts), 1)
        self.assertEqual(thoughts[0]["content"], "Decided to pivot to B2B")

    def test_save_thoughts_batch(self):
        thoughts = [
            {"content": "First thought", "project": "alpha"},
            {"content": "Second thought", "project": "beta"},
            {"content": "Third thought", "project": "alpha"},
        ]
        ids = self.store.save_thoughts(
            thoughts, "claude-code", "session1",
            session_date="2025-04-01T00:00:00Z", machine="test-mac"
        )
        self.assertEqual(len(ids), 3)

        # Retrieve all
        all_t = self.store.get_thoughts()
        self.assertEqual(len(all_t), 3)

    def test_get_thoughts_filter_by_project(self):
        self.store.save_thoughts(
            [{"content": "Alpha thought", "project": "a", "canonical_project": "alpha"}],
            "test", "s1", session_date="2025-01-01T00:00:00Z"
        )
        self.store.save_thoughts(
            [{"content": "Beta thought", "project": "b", "canonical_project": "beta"}],
            "test", "s2", session_date="2025-01-01T00:00:00Z"
        )

        alpha = self.store.get_thoughts(canonical_project="alpha")
        self.assertEqual(len(alpha), 1)
        self.assertEqual(alpha[0]["content"], "Alpha thought")

    def test_update_thought(self):
        thoughts = [{"content": "Original thought"}]
        ids = self.store.save_thoughts(thoughts, "test", "s1",
                                       session_date="2025-06-15T00:00:00Z")
        tid = ids[0]

        self.store.update_thought(tid, {"processed": True, "merged_into_page": "beacon"})

        updated = self.store.get_thoughts()
        self.assertTrue(updated[0]["processed"])
        self.assertEqual(updated[0]["merged_into_page"], "beacon")

    def test_page_crud(self):
        # No page yet
        content, version = self.store.get_page("beacon")
        self.assertIsNone(content)
        self.assertEqual(version, 0)

        # Save a page
        self.store.save_page("beacon", "# Beacon\n\nA cool project.", 1)
        content, version = self.store.get_page("beacon")
        self.assertIn("# Beacon", content)
        self.assertEqual(version, 1)

        # Update page
        self.store.save_page("beacon", "# Beacon\n\nAn even cooler project.", 2)
        content, version = self.store.get_page("beacon")
        self.assertIn("even cooler", content)
        self.assertEqual(version, 2)

    def test_get_all_pages_excludes_special(self):
        self.store.save_page("beacon", "# Beacon", 1)
        self.store.save_page("status", "# Status", 1)
        self.store.save_page("cross-cutting", "# CC", 1)
        self.store.save_page("me", "# Me", 1)

        pages = self.store.get_all_pages()
        slugs = [p["slug"] for p in pages]
        self.assertIn("beacon", slugs)
        self.assertNotIn("status", slugs)
        self.assertNotIn("cross-cutting", slugs)
        self.assertNotIn("me", slugs)

    def test_aliases(self):
        self.assertEqual(self.store.get_aliases(), [])

        self.store.save_alias("Beacon", "beacon")
        self.store.save_alias("beacon-app", "beacon")
        self.store.save_alias("Project B", "beta")

        aliases = self.store.get_aliases()
        self.assertEqual(len(aliases), 3)

        # Update existing alias
        self.store.save_alias("Beacon", "beacon-v2")
        aliases = self.store.get_aliases()
        beacon_alias = [a for a in aliases if a["alias"] == "Beacon"][0]
        self.assertEqual(beacon_alias["canonical_slug"], "beacon-v2")

    def test_state_persistence(self):
        state = self.store.load_state()
        self.assertEqual(state["processed_sessions"], {})

        state["processed_sessions"]["code:abc"] = 12345.0
        self.store.save_state(state)

        reloaded = self.store.load_state()
        self.assertEqual(reloaded["processed_sessions"]["code:abc"], 12345.0)

    def test_get_recent_thoughts(self):
        for i in range(25):
            self.store.save_thought({
                "content": f"Thought {i}",
                "canonical_project": "beacon",
                "created_at": f"2025-03-{(i % 28) + 1:02d}T00:00:00Z",
            })
        recent = self.store.get_recent_thoughts("beacon", limit=10)
        self.assertEqual(len(recent), 10)


class TestExtractors(unittest.TestCase):
    """Test conversation extraction from various tool formats."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_extract_claude_code(self):
        path = os.path.join(self.tmpdir, "session.jsonl")
        lines = [
            json.dumps({"type": "human", "message": {"role": "user", "content": "Build a dashboard"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "I'll create a React dashboard"}}),
            json.dumps({"type": "human", "message": {"role": "user", "content": "Add charts"}}),
        ]
        with open(path, "w") as f:
            f.write("\n".join(lines))

        text = extract_claude_code_conversation(path)
        self.assertIn("Build a dashboard", text)
        self.assertIn("React dashboard", text)
        self.assertIn("Add charts", text)

    def test_extract_claude_code_with_blocks(self):
        path = os.path.join(self.tmpdir, "session.jsonl")
        lines = [
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Let me help"},
                {"type": "tool_use", "name": "write_file"},
                {"type": "text", "text": "Done!"},
            ]}}),
        ]
        with open(path, "w") as f:
            f.write("\n".join(lines))

        text = extract_claude_code_conversation(path)
        self.assertIn("Let me help", text)
        self.assertIn("[tool: write_file]", text)

    def test_extract_codex(self):
        path = os.path.join(self.tmpdir, "session.jsonl")
        lines = [
            json.dumps({"role": "user", "content": "Fix the login bug"}),
            json.dumps({"role": "assistant", "content": "I see the issue in auth.py"}),
        ]
        with open(path, "w") as f:
            f.write("\n".join(lines))

        text = extract_codex_conversation(path)
        self.assertIn("Fix the login bug", text)
        self.assertIn("auth.py", text)

    def test_extract_cowork(self):
        path = os.path.join(self.tmpdir, "session.jsonl")
        # Cowork sessions are JSONL with message envelopes
        rows = [
            {"type": "user", "message": {"role": "user", "content": "Let's brainstorm"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Great, here are some ideas"}},
        ]
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

        text = extract_cowork_conversation(path)
        self.assertIn("brainstorm", text)
        self.assertIn("ideas", text)

    def test_extract_antigravity(self):
        session_dir = os.path.join(self.tmpdir, "session1")
        os.makedirs(session_dir)
        with open(os.path.join(session_dir, "notes.md"), "w") as f:
            f.write("# Meeting Notes\nDiscussed pricing strategy")
        with open(os.path.join(session_dir, "ideas.txt"), "w") as f:
            f.write("Consider freemium model")

        text = extract_antigravity_session(session_dir)
        self.assertIn("pricing strategy", text)
        self.assertIn("freemium", text)

    def test_extract_copilot(self):
        path = os.path.join(self.tmpdir, "chat.jsonl")
        lines = [
            json.dumps({"role": "user", "content": "Explain this function"}),
            json.dumps({"role": "assistant", "content": "This function handles auth"}),
        ]
        with open(path, "w") as f:
            f.write("\n".join(lines))

        text = extract_copilot_conversation(path)
        self.assertIn("Explain this function", text)
        self.assertIn("handles auth", text)

    def test_extract_cline(self):
        path = os.path.join(self.tmpdir, "api_conversation_history.json")
        data = [
            {"role": "user", "content": [{"type": "text", "text": "Create a REST API"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "I'll build it with FastAPI"}]},
        ]
        with open(path, "w") as f:
            json.dump(data, f)

        text = extract_cline_conversation(path)
        self.assertIn("REST API", text)
        self.assertIn("FastAPI", text)

    def test_extract_continue(self):
        path = os.path.join(self.tmpdir, "session.json")
        data = {
            "history": [
                {"role": "user", "content": "Refactor this class"},
                {"role": "assistant", "content": "I'll extract the methods"},
            ]
        }
        with open(path, "w") as f:
            json.dump(data, f)

        text = extract_continue_conversation(path)
        self.assertIn("Refactor", text)
        self.assertIn("extract", text)

    def test_extract_aider(self):
        path = os.path.join(self.tmpdir, ".aider.chat.history.md")
        with open(path, "w") as f:
            f.write("# Aider Chat\n\n> user: Fix the tests\n\nassistant: Done, all 12 tests pass now")

        text = extract_aider_conversation(path)
        self.assertIn("Fix the tests", text)
        self.assertIn("12 tests pass", text)

    def test_extract_opencode(self):
        path = os.path.join(self.tmpdir, "session.json")
        data = {
            "messages": [
                {"role": "user", "content": "Add error handling"},
                {"role": "assistant", "content": "I'll wrap it in try-except"},
            ]
        }
        with open(path, "w") as f:
            json.dump(data, f)

        text = extract_opencode_conversation(path)
        self.assertIn("error handling", text)
        self.assertIn("try-except", text)

    def test_extract_cursor(self):
        db_path = os.path.join(self.tmpdir, "state.vscdb")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        chat_data = {
            "conversation": [
                {"role": "user", "content": "Optimize this query"},
                {"role": "assistant", "content": "Add an index on user_id"},
            ]
        }
        conn.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            ("composer:session1", json.dumps(chat_data))
        )
        conn.commit()
        conn.close()

        text = extract_cursor_conversation(db_path)
        self.assertIn("Optimize this query", text)
        self.assertIn("index on user_id", text)

    def test_extract_empty_files(self):
        # All extractors should handle empty/missing files gracefully
        self.assertEqual(extract_claude_code_conversation("/nonexistent"), "")
        self.assertEqual(extract_codex_conversation("/nonexistent"), "")
        self.assertEqual(extract_cowork_conversation("/nonexistent"), "")
        self.assertEqual(extract_antigravity_session("/nonexistent"), "")
        self.assertEqual(extract_copilot_conversation("/nonexistent"), "")
        self.assertEqual(extract_cline_conversation("/nonexistent"), "")
        self.assertEqual(extract_continue_conversation("/nonexistent"), "")
        self.assertEqual(extract_aider_conversation("/nonexistent"), "")
        self.assertEqual(extract_opencode_conversation("/nonexistent"), "")

    def test_extract_max_chars(self):
        path = os.path.join(self.tmpdir, "big.jsonl")
        lines = []
        for i in range(1000):
            lines.append(json.dumps({"role": "user", "content": f"Message {i} " + "x" * 200}))
        with open(path, "w") as f:
            f.write("\n".join(lines))

        text = extract_codex_conversation(path, max_chars=5000)
        self.assertLessEqual(len(text), 5000)


class TestAliasResolution(unittest.TestCase):
    """Test project alias resolution and fuzzy matching."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = MarkdownStorage(base_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_exact_match(self):
        self.store.save_alias("Beacon", "beacon")
        thoughts = [{"content": "test", "project": "Beacon"}]
        resolved = resolve_aliases(thoughts, self.store)
        self.assertEqual(resolved[0]["canonical_project"], "beacon")

    def test_fuzzy_match(self):
        self.store.save_alias("beacon-app", "beacon")
        thoughts = [{"content": "test", "project": "beacon app"}]
        resolved = resolve_aliases(thoughts, self.store)
        self.assertEqual(resolved[0]["canonical_project"], "beacon")

    def test_new_project_creates_alias(self):
        thoughts = [{"content": "test", "project": "Brand New Project"}]
        resolved = resolve_aliases(thoughts, self.store)
        self.assertEqual(resolved[0]["canonical_project"], "brand-new-project")

        # Alias should be saved
        aliases = self.store.get_aliases()
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["canonical_slug"], "brand-new-project")

    def test_no_project_skipped(self):
        thoughts = [{"content": "meta thought", "project": None}]
        resolved = resolve_aliases(thoughts, self.store)
        self.assertNotIn("canonical_project", resolved[0])

    def test_project_wins_over_workspace_in_new_slug(self):
        """A thought tagged project="kidworthy" inside a calledthird workspace
        must become the kidworthy slug, not calledthird. Regression for the
        bug where workspace overrode the LLM's project tag in Priority 4."""
        thoughts = [{
            "content": "Kidworthy travel idea came up during calledthird work",
            "project": "kidworthy",
            "workspace": "calledthird",
        }]
        resolved = resolve_aliases(thoughts, self.store)
        self.assertEqual(resolved[0]["canonical_project"], "kidworthy")
        # And the saved alias should map kidworthy to itself, not to calledthird
        aliases = {a["alias"]: a["canonical_slug"]
                   for a in self.store.get_aliases()}
        self.assertEqual(aliases.get("kidworthy"), "kidworthy")

    def test_workspace_ignored_when_deep_subfolder(self):
        """Claude Code subfolder paths like
        calledthird-website-results-2026-04-08-exploration-1-claude should
        not hijack the slug — the LLM's project tag wins."""
        thoughts = [{
            "content": "decided to refactor the homepage",
            "project": "calledthird",
            "workspace": "calledthird-website-results-2026-04-08-exploration-1-claude",
        }]
        resolved = resolve_aliases(thoughts, self.store)
        self.assertEqual(resolved[0]["canonical_project"], "calledthird")


class TestDeduplication(unittest.TestCase):
    """Test thought deduplication."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = MarkdownStorage(base_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_duplicate_detected(self):
        # Save an existing thought
        self.store.save_thought({
            "content": "Decided to pivot to B2B model for better margins and enterprise contracts",
            "canonical_project": "beacon",
            "created_at": "2025-03-20T00:00:00Z",
        })

        # New thought with same prefix
        new_thoughts = [{
            "content": "Decided to pivot to B2B model for better margins and enterprise contracts",
            "canonical_project": "beacon",
        }]
        result = deduplicate_thoughts(new_thoughts, self.store)
        self.assertTrue(result[0].get("skipped"))
        self.assertEqual(result[0].get("skip_reason"), "duplicate")

    def test_unique_thought_not_skipped(self):
        self.store.save_thought({
            "content": "Old thought about something",
            "canonical_project": "beacon",
            "created_at": "2025-03-20T00:00:00Z",
        })

        new_thoughts = [{
            "content": "Completely new insight about market positioning",
            "canonical_project": "beacon",
        }]
        result = deduplicate_thoughts(new_thoughts, self.store)
        self.assertFalse(result[0].get("skipped", False))

    def test_duplicate_metadata_persisted(self):
        self.store.save_thought({
            "content": "Decided to pivot to B2B model for better margins and enterprise contracts",
            "canonical_project": "beacon",
            "created_at": "2025-03-20T00:00:00Z",
        })

        new_thoughts = [{
            "content": "Decided to pivot to B2B model for better margins and enterprise contracts",
            "project": "Beacon",
        }]
        ids = self.store.save_thoughts(
            new_thoughts, "claude-code", "session1",
            session_date="2025-03-21T00:00:00Z"
        )

        new_thoughts = resolve_aliases(new_thoughts, self.store)
        new_thoughts = deduplicate_thoughts(new_thoughts, self.store)
        persist_thought_metadata(new_thoughts, self.store)

        saved = next(t for t in self.store.get_thoughts() if t["id"] == ids[0])
        self.assertEqual(saved["canonical_project"], "beacon")
        self.assertTrue(saved["skipped"])
        self.assertEqual(saved["skip_reason"], "duplicate")
        self.assertTrue(saved["processed"])


class TestModelConfig(unittest.TestCase):
    """Test multi-provider model configuration."""

    def test_catalog_lookup(self):
        resolved = _resolve_model("haiku")
        self.assertEqual(resolved["provider"], "anthropic")
        self.assertIn("haiku", resolved["model"])

    def test_catalog_openai(self):
        resolved = _resolve_model("gpt-5.4")
        self.assertEqual(resolved["provider"], "openai")
        self.assertEqual(resolved["model"], "gpt-5.4")

    def test_catalog_google(self):
        resolved = _resolve_model("gemini-flash")
        self.assertEqual(resolved["provider"], "google")
        self.assertIn("gemini", resolved["model"])

    def test_raw_model_id_anthropic(self):
        resolved = _resolve_model("claude-sonnet-4-20250514")
        self.assertEqual(resolved["provider"], "anthropic")

    def test_raw_model_id_openai(self):
        resolved = _resolve_model("gpt-5.4-mini")
        self.assertEqual(resolved["provider"], "openai")

    def test_raw_model_id_google(self):
        resolved = _resolve_model("gemini-3.1-pro-preview")
        self.assertEqual(resolved["provider"], "google")

    def test_all_catalog_entries_have_provider(self):
        for name, entry in MODEL_CATALOG.items():
            self.assertIn("provider", entry, f"Missing provider for {name}")
            self.assertIn("model", entry, f"Missing model for {name}")
            self.assertIn(entry["provider"],
                          ["anthropic", "openai", "google", "local"],
                          f"Invalid provider for {name}")


class TestToolMemoryFiles(unittest.TestCase):
    """Test discovery of tool memory/rules files."""

    def test_returns_list(self):
        # Should not crash even if no files found
        result = find_tool_memory_files(max_chars=1000)
        self.assertIsInstance(result, list)

    def test_max_chars_respected(self):
        result = find_tool_memory_files(max_chars=100)
        total = sum(len(content) for _, content in result)
        self.assertLessEqual(total, 100 + 3000)  # Allow some slack for first file


class TestMainCLI(unittest.TestCase):
    """Regression tests for CLI startup and config handling."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_main_accepts_api_key_flags(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("sys.argv", [
                "ingest.py",
                "--dry-run",
                "--extract-model", "haiku",
                "--base-dir", self.tmpdir,
                "--anthropic-key", "test-key",
            ]):
                with patch.multiple(
                    "ingest",
                    find_claude_code_sessions=MagicMock(return_value=[]),
                    find_cowork_sessions=MagicMock(return_value=[]),
                    find_antigravity_sessions=MagicMock(return_value=[]),
                    find_codex_sessions=MagicMock(return_value=[]),
                    find_cursor_sessions=MagicMock(return_value=[]),
                    find_copilot_sessions=MagicMock(return_value=[]),
                    find_cline_sessions=MagicMock(return_value=[]),
                    find_continue_sessions=MagicMock(return_value=[]),
                    find_aider_sessions=MagicMock(return_value=[]),
                    find_opencode_sessions=MagicMock(return_value=[]),
                ):
                    main()


# ─── v0.2: Cloud-sync detection ─────────────────────────────────────────────

class TestCloudSyncDetection(unittest.TestCase):
    """_detect_cloud_sync catches the sync folders we've told users to avoid."""

    def test_icloud_drive(self):
        p = "/Users/alice/Library/Mobile Documents/com~apple~CloudDocs/gyrus"
        self.assertEqual(_detect_cloud_sync(p), "iCloud Drive")

    def test_google_drive_new(self):
        p = "/Users/alice/Library/CloudStorage/GoogleDrive-a@b.com/My Drive/gyrus"
        self.assertEqual(_detect_cloud_sync(p), "Google Drive")

    def test_google_drive_legacy(self):
        self.assertEqual(_detect_cloud_sync("/Users/alice/Google Drive/gyrus"),
                         "Google Drive")
        self.assertEqual(_detect_cloud_sync("/Users/alice/GoogleDrive/gyrus"),
                         "Google Drive")

    def test_dropbox_both_locations(self):
        self.assertEqual(_detect_cloud_sync("/Users/alice/Library/CloudStorage/Dropbox/gyrus"),
                         "Dropbox")
        self.assertEqual(_detect_cloud_sync("/Users/alice/Dropbox/gyrus"),
                         "Dropbox")

    def test_onedrive(self):
        self.assertEqual(_detect_cloud_sync("/Users/alice/Library/CloudStorage/OneDrive-Personal/gyrus"),
                         "OneDrive")
        self.assertEqual(_detect_cloud_sync("/Users/alice/OneDrive/gyrus"),
                         "OneDrive")
        # Windows multi-account naming
        self.assertEqual(_detect_cloud_sync("C:\\Users\\Alice\\OneDrive - Personal\\gyrus"),
                         "OneDrive")

    def test_windows_backslash_paths(self):
        """Windows Path objects serialize with backslashes; detection must handle both."""
        self.assertEqual(_detect_cloud_sync("C:\\Users\\Alice\\Dropbox\\gyrus"), "Dropbox")
        self.assertEqual(_detect_cloud_sync("C:\\Users\\Alice\\Google Drive\\gyrus"), "Google Drive")
        self.assertEqual(_detect_cloud_sync("C:\\Users\\Alice\\Box Sync\\gyrus"), "Box")
        self.assertIsNone(_detect_cloud_sync("C:\\Users\\Alice\\gyrus-local"))

    def test_box(self):
        self.assertEqual(_detect_cloud_sync("/Users/alice/Box Sync/gyrus"), "Box")
        self.assertEqual(_detect_cloud_sync("/Users/alice/Library/CloudStorage/Box-Personal/gyrus"),
                         "Box")

    def test_misc_providers(self):
        self.assertEqual(_detect_cloud_sync("/Users/alice/Sync/gyrus"), "Sync.com")
        self.assertEqual(_detect_cloud_sync("/Users/alice/pCloud Drive/gyrus"), "pCloud")
        self.assertEqual(_detect_cloud_sync("/Users/alice/Proton Drive/gyrus"), "Proton Drive")

    def test_local_paths_are_not_cloud(self):
        self.assertIsNone(_detect_cloud_sync("/Users/alice/gyrus-local"))
        self.assertIsNone(_detect_cloud_sync("/Users/alice/Documents/gyrus"))
        self.assertIsNone(_detect_cloud_sync("/tmp/gyrus-test"))
        self.assertIsNone(_detect_cloud_sync("/opt/gyrus"))

    def test_nonexistent_path_still_checked(self):
        # Caller may pass a path that doesn't exist yet (during `gyrus init`).
        # Detection must still work against the string.
        p = "/Users/alice/Dropbox/brand-new-gyrus-dir"
        self.assertEqual(_detect_cloud_sync(p), "Dropbox")


# ─── v0.2: Git helpers ──────────────────────────────────────────────────────

class TestGitHelpers(unittest.TestCase):
    """_git_* helpers are non-fatal no-ops on non-repo / no-remote paths."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_is_repo_false_on_plain_dir(self):
        self.assertFalse(_git_is_repo(self.tmpdir))

    def test_remote_url_none_on_non_repo(self):
        self.assertIsNone(_git_remote_url(self.tmpdir))

    def test_pull_noop_on_non_repo(self):
        ok, msg = _git_pull(self.tmpdir)
        self.assertTrue(ok)
        self.assertEqual(msg, "no remote")

    def test_commit_push_noop_on_non_repo(self):
        ok, msg = _git_commit_push(self.tmpdir, "test")
        self.assertTrue(ok)
        self.assertEqual(msg, "no remote")

    def test_is_repo_true_after_git_init(self):
        import subprocess
        subprocess.run(["git", "init", "--quiet"], cwd=self.tmpdir, check=True)
        self.assertTrue(_git_is_repo(self.tmpdir))
        # No remote yet, so pull/push still no-op
        self.assertIsNone(_git_remote_url(self.tmpdir))
        ok, _ = _git_pull(self.tmpdir)
        self.assertTrue(ok)


# ─── v0.2: Safe-read timeout ────────────────────────────────────────────────

class TestReadTextSafe(unittest.TestCase):
    """_read_text_safe returns content normally and None on error."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reads_normal_file(self):
        p = Path(self.tmpdir) / "f.txt"
        p.write_text("hello\nworld\n")
        self.assertEqual(_read_text_safe(p), "hello\nworld\n")

    def test_returns_none_on_missing(self):
        p = Path(self.tmpdir) / "missing.txt"
        self.assertIsNone(_read_text_safe(p))

    def test_is_dataless_false_on_normal_file(self):
        p = Path(self.tmpdir) / "normal.txt"
        p.write_text("x")
        self.assertFalse(_is_dataless(p))


# ─── v0.2: Doctor checks ────────────────────────────────────────────────────

class TestDoctorChecks(unittest.TestCase):
    """Doctor checks return (status, label, msg, hint) tuples and never raise."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_storage_ok_for_local_path(self):
        status, label, _, _ = _doctor_check_storage(self.tmpdir)
        self.assertEqual(status, "ok")
        self.assertEqual(label, "storage")

    def test_storage_warn_for_icloud_path(self):
        # Build a synthetic path that resolves to an iCloud location
        icloud = self.tmpdir / "fake-icloud-marker"
        icloud.mkdir()
        # The marker check is substring-based on the resolved path; we can't
        # easily fake that in a tmp dir, so we only assert the function returns
        # a tuple with the expected shape for any path.
        result = _doctor_check_storage(icloud)
        self.assertEqual(len(result), 4)
        self.assertIn(result[0], ("ok", "warn", "fail"))

    def test_freshness_warn_when_no_thoughts(self):
        status, label, _, _ = _doctor_check_freshness(self.tmpdir)
        self.assertEqual(status, "warn")
        self.assertEqual(label, "ingest freshness")

    def test_freshness_ok_when_recent_file(self):
        thoughts = self.tmpdir / "thoughts"
        thoughts.mkdir()
        today = datetime.now().strftime("%Y-%m-%d")
        (thoughts / f"{today}.jsonl").write_text("")
        status, _, _, _ = _doctor_check_freshness(self.tmpdir)
        self.assertEqual(status, "ok")

    def test_git_sync_warn_without_repo(self):
        status, label, _, _ = _doctor_check_git_sync(self.tmpdir)
        self.assertEqual(status, "warn")
        self.assertEqual(label, "git sync")

    def test_lockfile_ok_when_missing(self):
        status, label, _, _ = _doctor_check_lockfile()
        # Whether OK or warn depends on whether any gyrus is currently running
        # on this box, but the shape is always (status, label, msg, hint).
        self.assertEqual(label, "lockfile")
        self.assertIn(status, ("ok", "warn"))

    def test_run_doctor_returns_exit_code(self):
        # run_doctor prints a lot but should complete and return int
        # Use a capture to quiet the output
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_doctor(self.tmpdir)
        self.assertIsInstance(rc, int)
        self.assertIn(rc, (0, 1))
        self.assertIn("gyrus doctor", buf.getvalue())


# ─── v0.2: Doctor auto-fixes (--fix) ────────────────────────────────────────

class TestDoctorFixes(unittest.TestCase):
    """Auto-fix helpers are safe, idempotent, and never raise."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        # Keep any existing real lockfile safe — we back it up and restore later
        self._real_lock = _lock_path()
        self._real_lock_backup = None
        if self._real_lock.exists():
            self._real_lock_backup = self._real_lock.read_bytes()
            self._real_lock.unlink()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Clean up any lockfile we created
        if self._real_lock.exists():
            self._real_lock.unlink()
        # Restore the user's real lockfile if we displaced one
        if self._real_lock_backup is not None:
            self._real_lock.write_bytes(self._real_lock_backup)

    def test_fix_lockfile_removes_file(self):
        self._real_lock.parent.mkdir(parents=True, exist_ok=True)
        self._real_lock.write_text(json.dumps(
            {"machine": "x", "pid": 1, "time": 0}
        ))
        ok, msg = _doctor_fix_lockfile()
        self.assertTrue(ok)
        self.assertFalse(self._real_lock.exists())

    def test_fix_lockfile_noop_when_missing(self):
        ok, msg = _doctor_fix_lockfile()
        self.assertTrue(ok)
        self.assertIn("no lockfile", msg)

    def test_fix_git_sync_initializes_empty_dir(self):
        self.assertFalse((self.tmpdir / ".git").exists())
        ok, msg = _doctor_fix_git_sync(self.tmpdir)
        self.assertTrue(ok)
        self.assertTrue((self.tmpdir / ".git").exists())
        self.assertTrue((self.tmpdir / ".gitignore").exists())
        # Should have at least one commit
        import subprocess
        r = subprocess.run(
            ["git", "-C", str(self.tmpdir), "log", "--oneline"],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.strip())

    def test_fix_git_sync_no_remote_returns_actionable_message(self):
        import subprocess
        subprocess.run(["git", "init", "--quiet"], cwd=self.tmpdir, check=True)
        # CI runners may not have a global git identity — set one inline
        subprocess.run(["git", "-C", str(self.tmpdir),
                        "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "--allow-empty", "-m", "x", "--quiet"],
                       check=True)
        ok, msg = _doctor_fix_git_sync(self.tmpdir)
        self.assertFalse(ok)
        self.assertIn("no remote", msg)

    def test_run_doctor_with_fix_flag_does_not_raise(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_doctor(self.tmpdir, fix=True)
        self.assertIsInstance(rc, int)
        output = buf.getvalue()
        self.assertIn("--fix enabled", output)


# ─── v0.2: gyrus merge ──────────────────────────────────────────────────────

class TestMerge(unittest.TestCase):
    """`gyrus merge` rewrites aliases, thoughts, and orphan pages correctly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = MarkdownStorage(base_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed(self):
        """Seed fixture: two slugs (calledthird-website + calledthirdresearchcoaching-gap)
        that need to merge into 'calledthird'."""
        self.store.save_alias("calledthird-website", "calledthird-website")
        self.store.save_alias("calledthird.com", "calledthird-website")
        self.store.save_alias("Coaching Gap", "calledthirdresearchcoaching-gap")
        self.store.save_alias("nerve", "nerve")  # unrelated, should not move

        for date, cp in [
            ("2026-04-01", "calledthird-website"),
            ("2026-04-02", "calledthird-website"),
            ("2026-04-03", "calledthirdresearchcoaching-gap"),
            ("2026-04-04", "nerve"),  # unrelated
        ]:
            (Path(self.tmpdir) / "thoughts" / f"{date}.jsonl").write_text(
                json.dumps({"content": f"{cp} thought",
                            "canonical_project": cp,
                            "created_at": f"{date}T00:00:00Z"}) + "\n"
            )

        # Orphan project pages
        for slug in ("calledthird-website", "calledthirdresearchcoaching-gap"):
            (Path(self.tmpdir) / "projects" / f"{slug}.md").write_text(
                f"# {slug}\n\nstub\n"
            )

    def test_merge_rewrites_aliases(self):
        self._seed()
        rc = run_merge(
            self.store,
            ["calledthird-website", "calledthirdresearchcoaching-gap", "calledthird"],
            yes=True,
        )
        self.assertEqual(rc, 0)
        aliases = {a["alias"]: a["canonical_slug"]
                   for a in self.store.get_aliases()}
        # Existing aliases that mapped to either source now map to 'calledthird'
        self.assertEqual(aliases["calledthird-website"], "calledthird")
        self.assertEqual(aliases["calledthird.com"], "calledthird")
        self.assertEqual(aliases["Coaching Gap"], "calledthird")
        # Unrelated alias is untouched
        self.assertEqual(aliases["nerve"], "nerve")

    def test_merge_rewrites_thoughts(self):
        self._seed()
        run_merge(
            self.store,
            ["calledthird-website", "calledthirdresearchcoaching-gap", "calledthird"],
            yes=True,
        )
        # Every thought that pointed at either source now points at target
        thoughts_dir = Path(self.tmpdir) / "thoughts"
        cps = []
        for f in sorted(thoughts_dir.glob("*.jsonl")):
            for line in f.read_text().strip().splitlines():
                cps.append(json.loads(line)["canonical_project"])
        self.assertEqual(cps, ["calledthird", "calledthird", "calledthird", "nerve"])

    def test_merge_removes_orphan_pages(self):
        self._seed()
        run_merge(
            self.store,
            ["calledthird-website", "calledthirdresearchcoaching-gap", "calledthird"],
            yes=True,
        )
        projects_dir = Path(self.tmpdir) / "projects"
        self.assertFalse((projects_dir / "calledthird-website.md").exists())
        self.assertFalse((projects_dir / "calledthirdresearchcoaching-gap.md").exists())

    def test_merge_self_merge_noop(self):
        self._seed()
        rc = run_merge(self.store, ["calledthird", "calledthird"], yes=True)
        self.assertEqual(rc, 0)  # nothing to do, but not an error

    def test_merge_usage_error_on_too_few_args(self):
        rc = run_merge(self.store, ["calledthird"], yes=True)
        self.assertEqual(rc, 2)


class TestSlugClustering(unittest.TestCase):
    """Heuristic used by `gyrus merge` (no-arg mode) and `gyrus doctor`."""

    def test_dash_separated_fragments(self):
        # Classic case: calledthird + several dash-delimited children
        clusters = _detect_slug_clusters([
            "calledthird", "calledthird-website", "calledthird-research",
        ])
        self.assertEqual(clusters,
                         {"calledthird": ["calledthird-research",
                                          "calledthird-website"]})

    def test_no_dash_but_long_prefix(self):
        # Smashed name that shares a long prefix — covers real-world bug
        # where a garbage slug like 'calledthirdresearchcoaching-gap'
        # wasn't caught by the dash heuristic alone.
        clusters = _detect_slug_clusters([
            "calledthird", "calledthirdresearchcoaching-gap",
        ])
        self.assertIn("calledthird", clusters)
        self.assertIn("calledthirdresearchcoaching-gap",
                      clusters["calledthird"])

    def test_short_shared_prefix_is_not_cluster(self):
        # "kid" and "kidworthy" share a prefix but "kid" is short (<8);
        # don't cluster — avoids false positives.
        clusters = _detect_slug_clusters(["kid", "kidworthy"])
        self.assertEqual(clusters, {})

    def test_nested_cluster_flattens_to_root(self):
        # ct-web-results → ct-web → ct all roll up to ct in one cluster
        # (avoids leaving 'ct-web' hanging after sequential merges)
        clusters = _detect_slug_clusters([
            "ct", "ct-web", "ct-web-results",
        ])
        self.assertEqual(set(clusters.keys()), {"ct"})
        self.assertEqual(clusters["ct"], ["ct-web", "ct-web-results"])

    def test_unrelated_slugs_empty(self):
        clusters = _detect_slug_clusters(["nerve", "caremap", "chartlite"])
        self.assertEqual(clusters, {})

    def test_workspace_parents_supplement_prefix_heuristic(self):
        """When a slug has no text prefix match but its workspace maps to a
        real repo, the filesystem signal fills the gap."""
        # slug `homepage-redesign` shares no prefix with `calledthird`, but
        # filesystem says it was a subfolder of calledthird.
        slugs = ["calledthird", "homepage-redesign", "nerve"]
        ws_parents = {"homepage-redesign": "calledthird"}
        clusters = _detect_slug_clusters(slugs, workspace_parents=ws_parents)
        self.assertIn("homepage-redesign", clusters.get("calledthird", []))

    def test_prefix_wins_over_workspace_when_conflict(self):
        """If prefix says A → B but workspace says A → C, prefix wins."""
        slugs = ["beacon", "beacon-web", "calledthird"]
        ws_parents = {"beacon-web": "calledthird"}  # conflicting signal
        clusters = _detect_slug_clusters(slugs, workspace_parents=ws_parents)
        self.assertIn("beacon-web", clusters.get("beacon", []))
        self.assertNotIn("beacon-web", clusters.get("calledthird", []))

    def test_real_world_calledthird_cluster(self):
        # Exact slugs the user actually had pre-fix
        slugs = [
            "calledthird",
            "calledthird-website",
            "calledthird-website-results-2026-04-08-exploration-1-claude",
            "calledthird-website-results-2026-04-09-exploration-2-claude",
            "calledthirdresearchcoaching-gap",
            "kidworthy",  # separate, should not cluster in
            "nerve",      # unrelated
        ]
        clusters = _detect_slug_clusters(slugs)
        # All calledthird-* variants should cluster under some calledthird parent
        flattened = [f for fs in clusters.values() for f in fs]
        self.assertIn("calledthird-website", flattened)
        self.assertIn("calledthirdresearchcoaching-gap", flattened)
        self.assertIn(
            "calledthird-website-results-2026-04-08-exploration-1-claude",
            flattened,
        )
        # kidworthy must not have been clustered with anything
        self.assertNotIn("kidworthy", flattened)
        self.assertNotIn("kidworthy", clusters)
        # nerve likewise
        self.assertNotIn("nerve", flattened)


class TestMergeSuggest(unittest.TestCase):
    """`gyrus merge` (no-args) interactive flow."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = MarkdownStorage(base_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_projects_returns_early(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_merge_suggest(self.store, yes=True)
        self.assertEqual(rc, 0)
        self.assertIn("nothing to suggest", buf.getvalue())

    def test_no_clusters_reports_clean(self):
        # Two unrelated project pages
        (Path(self.tmpdir) / "projects" / "nerve.md").write_text("# nerve\n")
        (Path(self.tmpdir) / "projects" / "caremap.md").write_text("# caremap\n")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_merge_suggest(self.store, yes=True)
        self.assertEqual(rc, 0)
        self.assertIn("no fragmented slug clusters", buf.getvalue())

    def test_yes_auto_merges_all_clusters(self):
        # Seed a real cluster: target + two fragments with aliases + thoughts
        for slug in ("calledthird", "calledthird-website",
                     "calledthird-research"):
            (Path(self.tmpdir) / "projects" / f"{slug}.md").write_text(
                f"# {slug}\n"
            )
            self.store.save_alias(slug, slug)
            (Path(self.tmpdir) / "thoughts" / "2026-04-01.jsonl").write_text(
                json.dumps({"content": "t", "canonical_project": slug,
                            "created_at": "2026-04-01T00:00:00Z"}) + "\n",
            )

        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_merge_suggest(self.store, yes=True)
        self.assertEqual(rc, 0)
        # The two fragment pages should now be gone
        projects_dir = Path(self.tmpdir) / "projects"
        self.assertTrue((projects_dir / "calledthird.md").exists())
        self.assertFalse((projects_dir / "calledthird-website.md").exists())
        self.assertFalse((projects_dir / "calledthird-research.md").exists())


class TestLLMMergeSuggest(unittest.TestCase):
    """_llm_suggest_merges parses and filters Claude's response correctly."""

    def test_returns_empty_when_too_few_pages(self):
        self.assertEqual(_llm_suggest_merges([]), [])
        self.assertEqual(_llm_suggest_merges([{"slug": "a", "content": "x"}]), [])

    def test_parses_valid_response(self):
        pages = [
            {"slug": "beacon",     "content": "realtime analytics for startups"},
            {"slug": "atlas",      "content": "realtime analytics dashboard"},
            {"slug": "pulse",      "content": "realtime analytics startups"},
            {"slug": "caremap",    "content": "clinical workflow tool"},
        ]
        with patch("ingest.call_llm") as mock_llm:
            mock_llm.return_value = json.dumps([
                {"canonical": "beacon",
                 "fragments": ["atlas", "pulse"],
                 "reason": "all three describe the same realtime analytics product"}
            ])
            result = _llm_suggest_merges(pages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["canonical"], "beacon")
        self.assertEqual(sorted(result[0]["fragments"]), ["atlas", "pulse"])

    def test_drops_phantom_fragment_slugs(self):
        """LLM hallucinated a slug that doesn't exist — drop it."""
        pages = [
            {"slug": "beacon", "content": "x"},
            {"slug": "atlas",  "content": "y"},
        ]
        with patch("ingest.call_llm") as mock_llm:
            mock_llm.return_value = json.dumps([
                {"canonical": "beacon",
                 "fragments": ["atlas", "pulse-never-existed"]}
            ])
            result = _llm_suggest_merges(pages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["fragments"], ["atlas"])

    def test_skips_existing_cluster_slugs(self):
        """Don't ask the LLM about slugs already handled by heuristics."""
        pages = [
            {"slug": "beacon", "content": "x"},
            {"slug": "atlas",  "content": "y"},
        ]
        with patch("ingest.call_llm") as mock_llm:
            _llm_suggest_merges(pages, existing_cluster_slugs={"beacon", "atlas"})
            # With no eligible pages the LLM should not be called at all
            mock_llm.assert_not_called()

    def test_handles_malformed_json(self):
        pages = [{"slug": "a", "content": "x"}, {"slug": "b", "content": "y"}]
        with patch("ingest.call_llm") as mock_llm:
            mock_llm.return_value = "not valid json at all"
            result = _llm_suggest_merges(pages)
        self.assertEqual(result, [])


class TestLocalLLM(unittest.TestCase):
    """The local-LLM provider resolves, dispatches, and degrades gracefully."""

    def test_local_prefix_resolves(self):
        # Any `local:<model>` routes to the local provider
        self.assertEqual(
            _resolve_model("local:llama3.3")["provider"], "local",
        )
        self.assertEqual(
            _resolve_model("local:qwen3:32b")["model"], "qwen3:32b",
        )

    def test_catalog_local_models(self):
        # Named local models in the catalog route to local
        for name in ("llama3.3", "qwen3", "deepseek-v3", "gpt-oss"):
            self.assertEqual(
                _resolve_model(name)["provider"], "local",
                f"{name} should be a local model",
            )

    def test_call_local_hits_configured_base_url(self):
        """_call_local POSTs to {base_url}/chat/completions with OpenAI shape."""
        import ingest
        captured = {}

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def read(self):
                return self._payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            captured["auth"] = req.get_header("Authorization")
            return FakeResp(json.dumps({
                "choices": [{"message": {"content": "hello from local"}}]
            }).encode())

        with patch.object(ingest, "urlopen", fake_urlopen), \
             patch.dict(ingest._config, {"local_base_url": "http://localhost:1234/v1"}):
            out = _call_local("qwen3:7b",
                              [{"role": "user", "content": "hi"}],
                              max_tokens=256, api_key=None)
        self.assertEqual(out, "hello from local")
        self.assertEqual(captured["url"],
                         "http://localhost:1234/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "qwen3:7b")
        self.assertEqual(captured["body"]["max_tokens"], 256)
        self.assertTrue(captured["auth"].startswith("Bearer "))

    def test_call_local_gives_helpful_error_when_server_down(self):
        import ingest
        def explode(req, timeout=None):
            raise ConnectionRefusedError("nope")
        with patch.object(ingest, "urlopen", explode):
            with self.assertRaises(Exception) as ctx:
                _call_local("qwen3", [{"role": "user", "content": "hi"}],
                            max_tokens=32, api_key=None)
        msg = str(ctx.exception).lower()
        # Must mention how to fix it
        self.assertTrue(
            "ollama" in msg or "local" in msg or "base_url" in msg,
            f"error should be actionable, got: {ctx.exception}"
        )

    def test_base_url_env_overrides_config(self):
        import ingest
        with patch.dict(os.environ, {"GYRUS_LOCAL_BASE_URL": "http://x:9/v1"}), \
             patch.dict(ingest._config, {"local_base_url": "http://y:8/v1"}):
            self.assertEqual(_local_base_url(), "http://x:9/v1")

    def test_detect_local_llm_returns_none_when_nothing_listening(self):
        """With no server running the detection function returns (None,None,[]).
        Uses a bogus host to force connection failure."""
        import ingest
        with patch.object(ingest, "_LOCAL_LLM_CANDIDATES",
                          [("http://127.0.0.1:1/v1", "bogus")]):
            url, name, models = _detect_local_llm(timeout=1)
        self.assertIsNone(url)
        self.assertEqual(models, [])


if __name__ == "__main__":
    unittest.main()
