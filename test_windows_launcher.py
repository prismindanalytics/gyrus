"""Windows launcher regressions, with no installation or model calls."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ingest


def installed_windows_launcher():
    """Read the exact batch script emitted by the PowerShell installer."""
    installer = Path(__file__).with_name("install.ps1").read_text(encoding="utf-8")
    match = re.search(r'@"\n(@echo off\n.*?)\n"@ \| Set-Content \$GyrusBin\b',
                      installer, re.DOTALL)
    if not match:
        raise AssertionError("Windows launcher here-string not found")
    return match.group(1) + "\n"


class TestWindowsLauncher(unittest.TestCase):
    def test_launcher_forwards_once_and_preserves_working_directory(self):
        launcher = installed_windows_launcher()
        # The installer writes this file with Set-Content -Encoding ASCII.
        # Check on every platform so incompatible text does not hide behind
        # the Windows-only integration test.
        launcher.encode("ascii")
        commands = [line.strip() for line in launcher.splitlines()
                    if line.strip() and not line.lstrip().lower().startswith("rem ")]
        self.assertEqual(sum(line.count("%*") for line in commands), 1)
        self.assertFalse(any(re.match(r"(?i)(?:cd|chdir|pushd|shift)\b", line)
                             for line in commands))
        self.assertIn('"%GYRUS_HOME%\\ingest.py" %*', launcher)
        self.assertIn("--no-project", launcher)
        self.assertEqual(commands[-1], "exit /b %errorlevel%")

    def test_forwarded_subcommands_reach_runtime_handlers(self):
        # Exercise main's actual argument parser, not just its alias table.
        # Every external boundary is mocked before the runtime can reach it.
        cases = [
            (["init", "--clone", "https://github.com/example/knowledge"],
             "run_init", 0),
            (["doctor", "--fix"], "run_doctor", 0),
            (["models", "--yes"], "run_models", 0),
            (["merge", "old-project", "new-project"], "run_merge", 0),
            (["context", "--cwd", "project with spaces", "--tool", "codex"],
             "show_project_context", 0),
            (["update"], "self_update", True),
        ]
        with tempfile.TemporaryDirectory(prefix="gyrus launcher ") as tmp:
            base = Path(tmp)
            for arguments, handler_name, result in cases:
                with self.subTest(command=arguments[0]):
                    with (
                        patch.dict(os.environ, {}, clear=True),
                        patch.object(Path, "home", return_value=base / "home"),
                        patch.object(ingest, "_load_env_file"),
                        patch.object(ingest, "_print_heartbeat"),
                        patch.object(ingest, "_autosync_pull", side_effect=AssertionError("Unexpected sync")),
                        patch.object(ingest, "call_llm", side_effect=AssertionError("Unexpected model call")),
                        patch.object(ingest, handler_name, return_value=result) as handler,
                        patch.object(sys, "argv", ["ingest.py", *arguments,
                                                  "--base-dir", tmp, "--no-autosync"]),
                    ):
                        with self.assertRaises(SystemExit) as exit_result:
                            ingest.main()
                    self.assertEqual(exit_result.exception.code, 0)
                    handler.assert_called_once()
                    if handler_name == "run_init":
                        handler.assert_called_with(
                            clone_url="https://github.com/example/knowledge", location=None)
                    elif handler_name == "run_doctor":
                        handler.assert_called_with(base, fix=True)
                    elif handler_name == "run_models":
                        handler.assert_called_with(base, yes=True)
                    elif handler_name == "run_merge":
                        self.assertEqual(handler.call_args.args[1], ["old-project", "new-project"])
                        self.assertEqual(handler.call_args.args[0].base_dir, base)
                    elif handler_name == "show_project_context":
                        self.assertEqual(handler.call_args.kwargs["cwd"], "project with spaces")
                        self.assertEqual(handler.call_args.kwargs["tool"], "codex")
                        self.assertIsNone(handler.call_args.kwargs["project"])
                    else:
                        handler.assert_called_with(base)

    @unittest.skipUnless(os.name == "nt", "Requires the actual Windows command processor")
    def test_cmd_preserves_arguments_cwd_and_exit_status(self):
        # The fake uv records arguments instead of starting ingestion. This
        # catches CMD's SHIFT/%* behavior, quoting and cwd changes on Windows CI.
        with tempfile.TemporaryDirectory(prefix="gyrus launcher ") as tmp:
            root = Path(tmp)
            caller = root / "working project"
            caller.mkdir()
            knowledge = root / "knowledge base"
            knowledge.mkdir()
            launcher = root / "gyrus.cmd"
            launcher.write_text(installed_windows_launcher(), encoding="ascii")
            capture = root / "capture.py"
            capture.write_text(
                "import json, os, pathlib, sys\n"
                "pathlib.Path(os.environ['GYRUS_TEST_CAPTURE']).write_text(\n"
                "    json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}))\n"
                "sys.exit(int(os.environ['GYRUS_TEST_EXIT']))\n",
                encoding="utf-8",
            )
            fake_uv = root / "fake uv.cmd"
            fake_uv.write_text(
                f'@echo off\n"{sys.executable}" "{capture}" %*\nexit /b %errorlevel%\n',
                encoding="utf-8",
            )
            output = root / "captured.json"
            env = dict(os.environ, GYRUS_HOME=str(knowledge), UV_BIN=str(fake_uv),
                       GYRUS_TEST_CAPTURE=str(output), GYRUS_TEST_EXIT="23")
            for arguments in (["init"], ["doctor", "--fix"], ["models"],
                              ["merge", "old-project", "new-project"],
                              ["context", "--cwd", str(caller), "--tool", "codex"],
                              ["context"], ["update"]):
                with self.subTest(command=arguments):
                    command = subprocess.list2cmdline([str(launcher), *arguments])
                    result = subprocess.run(command, shell=True, cwd=caller, env=env,
                                            capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 23, result.stderr)
                    actual = json.loads(output.read_text())
                    self.assertEqual(actual["args"], [
                        "run", "--no-project", "--python", "3.12",
                        str(knowledge / "ingest.py"), *arguments,
                    ])
                    self.assertEqual(Path(actual["cwd"]), caller)


if __name__ == "__main__":
    unittest.main()
