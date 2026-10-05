from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cli_telegram_gateway.commands import parse_manifest
from cli_telegram_gateway.config import Config

REPO = Path(__file__).resolve().parents[1]
EXTENSION = REPO / "extensions" / "article"


class ArticleManifestTests(unittest.TestCase):
    def test_bundled_command_has_valid_exec_and_allowed_artifact_directory(self) -> None:
        config = Config(
            project_dir=REPO,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(REPO.parent,),
            default_workdir=REPO.parent,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
        )
        command = parse_manifest(EXTENSION / "manifest.json", config)
        self.assertEqual(command.command, "article")
        self.assertEqual(command.spawn_argv(), ("node", str(EXTENSION / "main.mjs")))
        self.assertEqual(command.cwd, REPO.parent / "artifacts")
        self.assertEqual(command.timeout_seconds, 180)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_gateway_first_argument_is_taken_from_environment_without_a_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {"PATH": os.environ.get("PATH", ""), "TG_ARGV0": "help", "TG_WORKDIR": temporary}
            result = subprocess.run(
                ["node", str(EXTENSION / "main.mjs")],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("/article <URL>", result.stdout)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_invalid_url_fails_before_creating_an_artifact_or_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {"PATH": os.environ.get("PATH", ""), "TG_RAW_ARGS": "file:///etc/passwd", "TG_WORKDIR": temporary}
            result = subprocess.run(
                ["node", str(EXTENSION / "main.mjs")],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("HTTP/HTTPS", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(list(Path(temporary).iterdir()), [])
