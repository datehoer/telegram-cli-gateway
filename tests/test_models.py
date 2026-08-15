from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from cli_telegram_gateway import models


class ModelCatalogTests(unittest.TestCase):
    def test_claude_aliases_are_static(self) -> None:
        self.assertEqual(models.list_models("claude", ("claude",)), ["opus", "sonnet", "haiku", "fable"])

    def test_codex_models_are_read_from_cache_and_hide_entries_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "models_cache.json"
            cache.write_text(
                json.dumps({
                    "models": [
                        {"slug": "gpt-5.6-sol", "visibility": "list"},
                        {"slug": "gpt-5.6-sol-wm", "visibility": "hide"},
                        {"slug": "codex-auto-review", "visibility": "hide"},
                        {"slug": "gpt-5.5", "visibility": "list"},
                    ]
                }),
                encoding="utf-8",
            )
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = temporary
            try:
                self.assertEqual(
                    models.list_models("codex", ("codex",)),
                    ["gpt-5.6-sol", "gpt-5.5"],
                )
            finally:
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous

    def test_codex_missing_cache_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = temporary
            try:
                self.assertEqual(models.list_models("codex", ("codex",)), [])
            finally:
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous

    def test_grok_models_are_parsed_from_cli_output(self) -> None:
        script = (
            "import sys; "
            "print('You are logged in with grok.com.'); "
            "print(); "
            "print('Default model: grok-4.6'); "
            "print(); "
            "print('Available models:'); "
            "print('  * grok-4.6 (default)'); "
            "print('  - grok-4.5'); "
        )
        self.assertEqual(
            models.list_models("grok", ("python3", "-c", script)),
            ["grok-4.6", "grok-4.5"],
        )

    def test_pi_models_keep_provider_prefix(self) -> None:
        script = (
            "print('provider         model              context  max-out  thinking  images'); "
            "print('hotday-deepseek  deepseek-v4-flash  1M       384K     yes       no'); "
            "print('hotday-deepseek  deepseek-v4-pro    1M       384K     yes       no'); "
        )
        self.assertEqual(
            models.list_models("pi", ("python3", "-c", script)),
            ["hotday-deepseek/deepseek-v4-flash", "hotday-deepseek/deepseek-v4-pro"],
        )

    def test_listing_failure_returns_empty(self) -> None:
        self.assertEqual(models.list_models("grok", ("/no/such/binary",)), [])

    def test_efforts_per_cli(self) -> None:
        self.assertEqual(models.list_efforts("claude"), ["low", "medium", "high", "xhigh", "max"])
        self.assertIn("ultra", models.list_efforts("codex"))
        self.assertEqual(models.list_efforts("grok")[0], "none")
        self.assertIn("off", models.list_efforts("pi"))
        self.assertEqual(models.list_efforts("unknown"), [])


if __name__ == "__main__":
    unittest.main()
