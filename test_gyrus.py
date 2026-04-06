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
            self.assertIn(entry["provider"], ["anthropic", "openai", "google"],
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


if __name__ == "__main__":
    unittest.main()
