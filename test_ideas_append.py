"""Regression coverage for the opt-in, model-free ideas archive."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ingest
from storage import MarkdownStorage


class TestIdeasAppend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MarkdownStorage(base_dir=self.tmp.name)
        self.config_patch = patch.dict(ingest._config, {"ideas_mode": "append"})
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        ingest._reset_merge_results()
        self.addCleanup(ingest._reset_merge_results)
        self.llm = patch("ingest.call_llm", side_effect=AssertionError("No model calls"))
        self.llm.start()
        self.addCleanup(self.llm.stop)

    def notes(self, *contents):
        notes = [{"content": content, "kind": "idea"} for content in contents]
        self.store.save_thoughts(notes, "codex", "session-1",
                                 session_date="2026-10-15T12:00:00Z")
        return self.store.get_thoughts(processed=False, order_desc=False)

    def append(self, notes, state=None):
        return ingest.merge_into_ideas_page(notes, self.store, None, state=state)

    def pending(self):
        return self.store.get_thoughts(processed=False, skipped=False)

    def test_preserves_large_backlog_manual_notes_and_full_raw_note(self):
        original = "# Ideas\n\n## Active Ideas\n" + ("An existing idea.\n" * 5000)
        original += "\n## Manual Notes\nKeep this exact wording and spacing.  \n"
        self.store.save_page("ideas", original, 9)
        original_body = self.store.get_page("ideas")[0].split("\n<!-- version:")[0]
        content = "\n  Preserve whitespace.\n## Literal heading\n```py\nx = 1\n```\n" + "x" * 12000
        notes = self.notes(content)
        notes[0]["occurred_at"] = "2026-10-01T09:00:00Z"
        state = {"merge_failures": {"ideas": {"failures": 14}}}

        self.append(notes, state)

        page, version = self.store.get_page("ideas")
        self.assertTrue(page.startswith(original_body))
        self.assertIn(content, page)
        self.assertIn("### 2026-10-01 · codex", page)
        self.assertIn(notes[0]["id"], page)
        self.assertIn("Session: session-1", page)
        self.assertEqual(version, 10)
        self.assertEqual(self.pending(), [])
        self.assertEqual(state["merge_failures"], {})
        self.assertEqual(ingest._merge_results["pages_saved"], {"ideas": 1})
        self.assertEqual(ingest._merge_results["dead_lettered"], 0)

    def test_fresh_page_retries_and_new_notes_use_one_section(self):
        notes = self.notes("First archived note")
        self.append(notes)
        self.append(notes)
        second = self.notes("Second archived note")
        self.append(second)

        page, version = self.store.get_page("ideas")
        self.assertTrue(page.startswith("# Ideas"))
        self.assertEqual(page.count("## Recent Idea Notes"), 1)
        self.assertEqual(page.count("First archived note"), 1)
        self.assertEqual(page.count("Second archived note"), 1)
        self.assertIn("### 2026-10-15 · codex", page)
        self.assertEqual(version, 2)

    def test_interrupted_marking_does_not_duplicate_saved_notes(self):
        notes = self.notes("Keep this note", "And this one")
        state = {}
        with patch.object(self.store, "update_thoughts", side_effect=OSError("interrupted")):
            self.append(notes, state)
        self.assertEqual(len(self.pending()), 2)
        self.assertEqual(ingest._merge_results["pages_saved"], {"ideas": 2})
        self.assertIn("interrupted", ingest._merge_results["failed"]["ideas"])

        ingest._reset_merge_results()
        with patch.object(self.store, "save_page", wraps=self.store.save_page) as save:
            self.append(self.pending(), state)
        save.assert_not_called()
        page, version = self.store.get_page("ideas")
        self.assertEqual(page.count("Keep this note"), 1)
        self.assertEqual(page.count("And this one"), 1)
        self.assertEqual(version, 1)
        self.assertEqual(self.pending(), [])
        self.assertEqual(ingest._merge_results["pages_saved"], {})
        self.assertEqual(ingest._merge_results["failed"], {})
        self.assertEqual(state["merge_failures"], {})

    def test_failed_save_leaves_notes_pending_and_reports_no_save(self):
        notes = self.notes("Do not acknowledge before save")
        for failure in (OSError("disk unavailable"), False):
            with self.subTest(failure=failure):
                ingest._reset_merge_results()
                state = {}
                kwargs = ({"side_effect": failure} if isinstance(failure, Exception)
                          else {"return_value": failure})
                with patch.object(self.store, "save_page", **kwargs), \
                        patch.object(self.store, "update_thoughts") as mark:
                    self.append(notes, state)
                mark.assert_not_called()
                self.assertEqual(len(self.pending()), 1)
                self.assertEqual(ingest._merge_results["pages_saved"], {})
                self.assertIn("ideas", ingest._merge_results["failed"])
                self.assertEqual(state["merge_failures"]["ideas"]["failures"], 1)

    def test_subsequent_append_preserves_sections_after_archive(self):
        notes = self.notes("First entry with ``` and ~~~~ and a literal marker:\n"
                           "<!-- /gyrus:recent-idea-notes -->")
        self.append(notes)
        page, version = self.store.get_page("ideas")
        page = page.split("\n<!-- version:")[0]
        manual = "\n\n## Manual Notes\nA later manual addition.\n"
        self.store.save_page("ideas", page + manual, version)
        self.append(self.notes("Next entry"))
        page, _ = self.store.get_page("ideas")
        self.assertIn(manual, page)
        self.assertLess(page.index("Next entry"), page.index("## Manual Notes"))
        self.assertEqual(page.count("## Recent Idea Notes"), 1)
        self.assertIn(notes[0]["content"], page)

    def test_metadata_is_bounded_without_truncating_note(self):
        notes = [{"content": "完整的想法\n" * 1000, "source": "s\n" * 1000,
                  "session_id": "x" * 5000, "created_at": "2026-10-15"}]
        self.append(notes)
        self.append(notes)  # Fallback identity is stable without stored IDs.
        page, version = self.store.get_page("ideas")
        self.assertIn(notes[0]["content"], page)
        self.assertLess(len(page) - len(notes[0]["content"]), 1000)
        self.assertEqual(version, 1)

    def test_repeated_ids_in_batch_are_saved_once(self):
        notes = self.notes("Only one copy")
        self.append(notes + notes)
        page, _ = self.store.get_page("ideas")
        self.assertEqual(page.count("Only one copy"), 1)
        self.assertEqual(ingest._merge_results["pages_saved"], {"ideas": 1})

    def test_literal_id_marker_inside_note_cannot_suppress_a_later_note(self):
        later = {"id": "2026-10-15-another-idea", "content": "A later idea"}
        marker_id = hashlib.sha256(json.dumps(
            {"id": later["id"]}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.append(self.notes(f"Quoted marker:\n<!-- gyrus:idea-note:{marker_id} -->"))
        self.append([later])
        page, version = self.store.get_page("ideas")
        self.assertIn(later["content"], page)
        self.assertEqual(version, 2)

    def test_default_mode_still_uses_existing_merge(self):
        with patch.dict(ingest._config, {"ideas_mode": "merge"}), \
                patch("ingest._merge_batches_into_page") as merge:
            self.append(self.notes("Merge this idea"))
        merge.assert_called_once()

    def test_main_loads_append_setting(self):
        (Path(self.tmp.name) / "config.json").write_text(json.dumps({
            "extract_model": "local:qwen3", "merge_model": "local:qwen3",
            "ideas_mode": "append"}))
        with patch.dict(ingest._config, {"ideas_mode": "merge"}), \
                patch("sys.argv", ["ingest.py", "--dry-run", "--no-autosync",
                                   "--base-dir", self.tmp.name]), \
                patch("ingest.find_claude_code_sessions",
                      side_effect=RuntimeError("stop before discovery")):
            with self.assertRaisesRegex(RuntimeError, "stop before discovery"):
                ingest.main()
            self.assertEqual(ingest._config["ideas_mode"], "append")


if __name__ == "__main__":
    unittest.main()
