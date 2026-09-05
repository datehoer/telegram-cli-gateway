from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_telegram_gateway.config import Config, ConfigError, load_env_file


class ConfigTests(unittest.TestCase):
    def test_load_env_file_supports_quotes_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text('A="hello world"\nB=value # comment\n', encoding="utf-8")
            self.assertEqual(load_env_file(env_file), {"A": "hello world", "B": "value"})

    def test_config_loads_and_restricts_workdirs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            allowed = project / "projects"
            allowed.mkdir()
            child = allowed / "child"
            child.mkdir()
            (project / ".env").write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=123:test",
                        "TELEGRAM_ALLOWED_USERS=100,200",
                        f"DEFAULT_WORKDIR={allowed}",
                        f"ALLOWED_WORKDIRS={allowed}",
                        "CLI_CLAUDE=/bin/sh",
                        "CLI_CODEX=/bin/sh",
                        "CLI_GROK=/bin/sh",
                        "CLI_PI=/bin/sh",
                        "DEFAULT_CODEX_MODEL=gpt-6-astra",
                        "DEFAULT_CODEX_EFFORT=HIGH",
                        "ENABLED_CLIS=codex, claude codex",
                    ]
                ),
                encoding="utf-8",
            )
            keys = {
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_ALLOWED_USERS",
                "DEFAULT_WORKDIR",
                "ALLOWED_WORKDIRS",
                "CLI_CLAUDE",
                "CLI_CODEX",
                "CLI_GROK",
                "CLI_PI",
                "DEFAULT_CODEX_MODEL",
                "DEFAULT_CODEX_EFFORT",
                "ENABLED_CLIS",
                "STREAM_UPDATE_INTERVAL",
            }
            clean_environment = {key: value for key, value in os.environ.items() if key not in keys}
            with patch.dict(os.environ, clean_environment, clear=True):
                config = Config.load(project)
            self.assertEqual(config.allowed_user_ids, frozenset({100, 200}))
            self.assertEqual(config.enabled_clis, ("codex", "claude"))
            self.assertEqual(config.stream_update_interval, 10.0)
            self.assertEqual(config.default_model_for("codex"), "gpt-6-astra")
            self.assertEqual(config.default_effort_for("codex"), "high")
            self.assertIsNone(config.default_model_for("claude"))
            self.assertEqual(config.resolve_workdir(str(child)), child.resolve())
            with self.assertRaises(ConfigError):
                config.resolve_workdir("/etc")

    def test_auto_send_artifacts_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            allowed = project / "projects"
            allowed.mkdir()
            base_env = {
                "TELEGRAM_BOT_TOKEN": "123:test",
                "TELEGRAM_ALLOWED_USERS": "100",
                "DEFAULT_WORKDIR": str(allowed),
                "ALLOWED_WORKDIRS": str(allowed),
                "CLI_CLAUDE": "/bin/sh",
                "CLI_CODEX": "/bin/sh",
                "CLI_GROK": "/bin/sh",
                "CLI_PI": "/bin/sh",
            }
            for mode, expected in (("off", "off"), ("images", "images"), ("all", "all"), ("", "images")):
                values = dict(base_env)
                if mode:
                    values["AUTO_SEND_ARTIFACTS"] = mode
                clean_environment = {key: value for key, value in os.environ.items() if key not in values}
                with patch.dict(os.environ, {**clean_environment, **values}, clear=True):
                    config = Config.load(project)
                self.assertEqual(config.auto_send_artifacts, expected)
            values = {**base_env, "AUTO_SEND_ARTIFACTS": "sometimes"}
            clean_environment = {key: value for key, value in os.environ.items() if key not in values}
            with patch.dict(os.environ, {**clean_environment, **values}, clear=True):
                with self.assertRaises(ConfigError):
                    Config.load(project)

    def test_numbered_or_named_bot_tokens_are_discovered_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            allowed = project / "projects"
            allowed.mkdir()
            (project / ".env").write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=primary:test",
                        "TELEGRAM_BOT_TOKEN_2=worker:test",
                        "TELEGRAM_BOT_TOKEN_RESEARCH=research:test",
                        "TELEGRAM_ALLOWED_USERS=100",
                        f"DEFAULT_WORKDIR={allowed}",
                        f"ALLOWED_WORKDIRS={allowed}",
                    ]
                ),
                encoding="utf-8",
            )
            clean_environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("TELEGRAM_BOT_TOKEN")
            }
            with patch.dict(os.environ, clean_environment, clear=True):
                config = Config.load(project)
            self.assertEqual(
                config.telegram_bots,
                (
                    ("default", "primary:test"),
                    ("2", "worker:test"),
                    ("research", "research:test"),
                ),
            )

    def test_duplicate_bot_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            allowed = project / "projects"
            allowed.mkdir()
            values = {
                "TELEGRAM_BOT_TOKEN": "same:test",
                "TELEGRAM_BOT_TOKEN_2": "same:test",
                "TELEGRAM_ALLOWED_USERS": "100",
                "DEFAULT_WORKDIR": str(allowed),
                "ALLOWED_WORKDIRS": str(allowed),
            }
            with patch.dict(os.environ, values, clear=True):
                with self.assertRaisesRegex(ConfigError, "more than once"):
                    Config.load(project)

    def test_enabled_clis_must_be_known_and_nonempty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            allowed = project / "projects"
            allowed.mkdir()
            base_env = {
                "TELEGRAM_BOT_TOKEN": "123:test",
                "TELEGRAM_ALLOWED_USERS": "100",
                "DEFAULT_WORKDIR": str(allowed),
                "ALLOWED_WORKDIRS": str(allowed),
            }
            for raw in ("", "codex,unknown"):
                values = {**base_env, "ENABLED_CLIS": raw}
                with patch.dict(os.environ, values, clear=True):
                    with self.assertRaises(ConfigError):
                        Config.load(project)


if __name__ == "__main__":
    unittest.main()
