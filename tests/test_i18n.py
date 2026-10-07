from __future__ import annotations

import ast
import json
import os
import string
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_telegram_gateway import i18n
from cli_telegram_gateway.commands import DirectCommandRunner, parse_manifest
from cli_telegram_gateway.config import Config, ConfigError
from cli_telegram_gateway.usage import context_lines

SOURCE = Path(__file__).resolve().parents[1] / "src" / "cli_telegram_gateway"


def translatable_literals() -> tuple[set[str], list[str]]:
    """Return every string passed to tr() or N_(), and any f-string passed instead."""
    found: set[str] = set()
    formatted: list[str] = []
    for path in sorted(SOURCE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in {"tr", "N_"} or not node.args:
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                found.add(argument.value)
            elif isinstance(argument, ast.JoinedStr):
                formatted.append(f"{path.name}:{node.lineno}")
    return found, formatted


def fields(template: str) -> list[tuple[str, str]]:
    return sorted(
        (name, spec) for _text, name, spec, _conversion in string.Formatter().parse(template)
        if name is not None
    )


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, "en")

    def test_every_translatable_string_has_chinese_and_no_entry_is_stale(self) -> None:
        literals, formatted = translatable_literals()
        self.assertEqual(formatted, [], "tr()/N_() need a literal template, not an f-string")
        self.assertEqual(sorted(literals - set(i18n.ZH)), [], "missing Chinese translations")
        self.assertEqual(sorted(set(i18n.ZH) - literals), [], "unused Chinese translations")

    def test_translations_keep_the_same_placeholders(self) -> None:
        mismatched = [text for text, chinese in i18n.ZH.items() if fields(text) != fields(chinese)]
        self.assertEqual(mismatched, [])

    def test_english_is_the_default_and_chinese_is_selectable(self) -> None:
        self.assertEqual(i18n.current_language(), "en")
        self.assertEqual(i18n.tr("Run log · part {page}", page=2), "Run log · part 2")
        i18n.set_language("zh")
        self.assertEqual(i18n.tr("Run log · part {page}", page=2), "运行记录 · 第 2 段")
        self.assertEqual(i18n.join_items(["claude", "pi"]), "claude、pi")
        with self.assertRaises(ValueError):
            i18n.set_language("fr")

    def test_legacy_chinese_context_basis_renders_in_the_current_language(self) -> None:
        usage = {"context_tokens": 10, "context_window": 100, "context_basis": "最近请求"}
        self.assertEqual(context_lines(usage)[0], "Context (latest request): 10 / 100 tokens (10.0%)")
        i18n.set_language("zh")
        self.assertEqual(context_lines(usage)[0], "上下文（最近请求）：10 / 100 tokens（10.0%）")


class LanguageSettingTests(unittest.TestCase):
    def test_gateway_language_defaults_to_english_and_rejects_unknown_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            values = {
                "TELEGRAM_BOT_TOKEN": "123:test",
                "TELEGRAM_ALLOWED_USERS": "100",
                "ALLOWED_WORKDIRS": str(project),
                "DEFAULT_WORKDIR": str(project),
            }
            with patch.dict(os.environ, values, clear=True):
                self.assertEqual(Config.load(project).language, "en")
            with patch.dict(os.environ, {**values, "GATEWAY_LANGUAGE": " ZH "}, clear=True):
                self.assertEqual(Config.load(project).language, "zh")
            invalid = {**values, "GATEWAY_LANGUAGE": "fr"}
            with patch.dict(os.environ, invalid, clear=True), self.assertRaisesRegex(ConfigError, "GATEWAY_LANGUAGE"):
                Config.load(project)


class ExtensionLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(i18n.set_language, "en")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.extension = self.project / "extensions" / "demo"
        self.extension.mkdir(parents=True)
        self.config = Config(
            project_dir=self.project,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(self.project,),
            default_workdir=self.project,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
        )

    def write_manifest(self, **extra: object) -> Path:
        (self.extension / "main.py").write_text(
            "import os\nprint('language=' + os.environ['TG_LANGUAGE'])\n", encoding="utf-8"
        )
        payload = {
            "schema_version": 1, "id": "demo", "command": "demo", "name": "Demo",
            "usage": "/demo <text>", "description": "Echo the text",
            "exec": ["python3", "main.py"], **extra,
        }
        path = self.extension / "manifest.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_manifest_display_text_follows_the_gateway_language(self) -> None:
        command = parse_manifest(self.write_manifest(i18n={"zh": {"name": "演示", "description": "回显文本"}}), self.config)
        self.assertEqual((command.display_name(), command.display_description()), ("Demo", "Echo the text"))
        i18n.set_language("zh")
        self.assertEqual((command.display_name(), command.display_description()), ("演示", "回显文本"))
        # A field without a translation keeps the manifest's own text.
        self.assertEqual(command.usage_text(), "/demo <text>")

    def test_manifest_rejects_unknown_languages_and_fields(self) -> None:
        from cli_telegram_gateway.commands import CommandManifestError

        for value in ({"fr": {"name": "Démo"}}, {"zh": {"exec": "x"}}, ["zh"]):
            with self.subTest(i18n=value), self.assertRaises(CommandManifestError):
                parse_manifest(self.write_manifest(i18n=value), self.config)

    def test_extensions_receive_the_gateway_language(self) -> None:
        command = parse_manifest(self.write_manifest(), self.config)
        i18n.set_language("zh")
        runner = DirectCommandRunner(self.config, {command.command: command})
        self.addCleanup(runner.shutdown)
        run = runner.start(1, "demo", [])
        deadline = time.monotonic() + 10
        while runner.get(run.turn_id).status == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("language=zh", runner.get(run.turn_id).output_text())


if __name__ == "__main__":
    unittest.main()
