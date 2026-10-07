from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from cli_telegram_gateway.app import GatewayApp
from cli_telegram_gateway.config import Config


def write_extension(
    project: Path,
    directory: str,
    script: str,
    *,
    command: str = "demo",
    **manifest_overrides: Any,
) -> Path:
    extension = project / "extensions" / directory
    extension.mkdir(parents=True, exist_ok=True)
    (extension / "main.py").write_text(script, encoding="utf-8")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "id": command,
        "name": f"{command} extension",
        "command": command,
        "usage": f"/{command} <text>",
        "description": f"{command} description",
        "exec": ["python3", "main.py"],
        "cwd": str(project),
        "timeout_seconds": 60,
    }
    payload.update(manifest_overrides)
    (extension / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return extension


class DirectCommandAppTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)

    def make_app(self, **kwargs: Any) -> GatewayApp:
        config = Config(
            project_dir=self.project,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(self.project,),
            default_workdir=self.project,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
            stream_update_interval=0.01,
            **kwargs,
        )
        app = GatewayApp(config)
        self.sent: list[str] = []
        self.cards: list[tuple[int, str]] = []
        self.edits: list[tuple[int, str]] = []
        self._message_id = 100

        def fake_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal_holder = self
            nonlocal_holder._message_id += 1
            if method.startswith("edit"):
                text = payload.get("rich_message", {}).get("markdown") or payload.get("text", "")
                nonlocal_holder.edits.append((int(payload["message_id"]), str(text)))
            elif method.startswith(("sendRichMessage", "sendMessage")):
                text = payload.get("rich_message", {}).get("markdown") or payload.get("text", "")
                nonlocal_holder.cards.append((nonlocal_holder._message_id, str(text)))
            return {"message_id": nonlocal_holder._message_id}

        for telegram in app._telegrams.values():
            telegram._call = fake_call  # type: ignore[method-assign]
        app._send = lambda _chat_id, text, *_a, **_kw: self.sent.append(text)  # type: ignore[method-assign]
        return app

    def wait_for_status(self, run, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if run.status != "running":
                return run
            time.sleep(0.02)
        raise AssertionError("command did not finish in time")

    def pump(self, app: GatewayApp, iterations: int = 1) -> None:
        for _ in range(iterations):
            app._pump_direct_commands(time.monotonic())
            time.sleep(0.02)

    def test_dispatch_runs_the_command_and_publishes_progress_then_result(self) -> None:
        write_extension(
            self.project,
            "demo",
            "import time\n"
            "print('@@PROGRESS step one', flush=True)\n"
            "time.sleep(0.2)\n"
            "print('final output')\n",
        )
        app = self.make_app()
        app._handle_command(1, "demo", "hello world")

        run = next(iter(app.direct_commands.runs()))
        # Direct commands skip shell tokenizing: the whole line is one argument and the extension splits it.
        self.assertEqual(run.args, ["hello world"])
        self.assertEqual(run.raw_args, "hello world")
        self.assertEqual(len(self.cards), 1, "the first card must be sent immediately")
        self.assertIn("Running", self.cards[0][1])
        self.assertIn("/demo · demo extension", self.cards[0][1])

        self.wait_for_status(run)
        self.pump(app)
        self.assertEqual(run.status, "completed")
        self.assertIn("final output", self.edits[-1][1])
        self.assertNotIn("@@PROGRESS", self.edits[-1][1])
        # The run is retired once its terminal card was published.
        self.assertEqual(app.direct_commands.runs(), [])

    def test_running_card_shows_the_progress_line_in_place(self) -> None:
        write_extension(
            self.project,
            "demo",
            "import time\nprint('@@PROGRESS 42 downloading', flush=True)\ntime.sleep(5)\n",
        )
        app = self.make_app()
        app._handle_command(1, "demo", "")
        run = next(iter(app.direct_commands.runs()))
        time.sleep(0.3)
        self.pump(app)
        self.assertTrue(any("42 downloading" in text for _id, text in self.edits))
        app.direct_commands.interrupt(run.turn_id)

    def test_artifact_paths_become_send_buttons(self) -> None:
        write_extension(
            self.project,
            "demo",
            "import os\n"
            "from pathlib import Path\n"
            "target = Path(os.environ['TG_WORKDIR']) / 'result.txt'\n"
            "target.write_text('done', encoding='utf-8')\n"
            "print(f'wrote {target}')\n",
        )
        app = self.make_app()
        app._handle_command(1, "demo", "")
        run = next(iter(app.direct_commands.runs()))
        self.wait_for_status(run)
        self.pump(app)
        markup = app._direct_artifact_markup(run)
        self.assertIsNotNone(markup)
        button = markup["inline_keyboard"][0][0]  # type: ignore[index]
        self.assertIn("Send file: result.txt", button["text"])
        self.assertTrue(button["callback_data"].startswith("file:"))
        self.assertEqual(run.auto_sent, [], "AUTO_SEND_ARTIFACTS defaults to off")

    def test_unknown_command_is_reported_and_valid_command_is_listed(self) -> None:
        write_extension(self.project, "demo", "print('ok')\n")
        app = self.make_app()
        app._handle_command(1, "nope", "")
        self.assertTrue(any("Unknown command" in text for text in self.sent))
        view = app._commands_view()
        self.assertIn("/demo", view)
        self.assertIn("/demo <text>", view)

    def test_command_list_entries_are_registered_for_the_telegram_menu(self) -> None:
        write_extension(self.project, "demo", "print('ok')\n", description="demo description")
        app = self.make_app()
        entries = dict(app._command_menu_entries())
        self.assertEqual(entries["demo"], "demo description")

    def test_reply_to_a_command_card_does_not_reach_the_cli_session(self) -> None:
        write_extension(self.project, "demo", "print('ok')\n")
        app = self.make_app()
        app._handle_command(1, "demo", "")
        run = next(iter(app.direct_commands.runs()))
        card_id = run.message_id
        self.assertIsNotNone(card_id)

        session = app.sessions.create_headless("pi", self.project, 1)
        forwarded: list[str] = []
        app._send_to_session = (  # type: ignore[method-assign]
            lambda _chat, _session, text, *_a, **_kw: forwarded.append(text)
        )
        app.handle_update(
            {
                "message": {
                    "message_id": 900,
                    "from": {"id": 1},
                    "chat": {"id": 1, "type": "private"},
                    "text": "please continue",
                    "reply_to_message": {"message_id": card_id},
                }
            }
        )
        self.assertEqual(forwarded, [])
        self.assertEqual(app.sessions.current(1).session_id, session.session_id)

    def test_interrupt_falls_back_to_the_running_command_when_the_session_is_idle(self) -> None:
        write_extension(self.project, "demo", "import time\ntime.sleep(30)\n")
        app = self.make_app()
        session = app.sessions.create_headless("pi", self.project, 1)
        app._handle_command(1, "demo", "")
        run = next(iter(app.direct_commands.runs()))
        time.sleep(0.2)

        # With the session idle, /interrupt must not just answer "no task is running".
        self.assertFalse(app._interrupt_session_turn(session))
        app._interrupt_session(session)
        self.wait_for_status(run)
        self.assertEqual(run.status, "interrupted")
        self.pump(app)
        self.assertIn("Interrupted", self.edits[-1][1])

    def test_a_session_turn_still_wins_over_the_command_fallback(self) -> None:
        write_extension(self.project, "demo", "import time\ntime.sleep(30)\n")
        app = self.make_app()
        session = app.sessions.create_headless("pi", self.project, 1)
        calls: list[str] = []
        app.headless.interrupt = (  # type: ignore[method-assign]
            lambda session_id: calls.append(session_id) or False
        )
        app.sessions.set_in_flight(session.session_id, 1, "turn-1")
        app._handle_command(1, "demo", "")
        run = next(iter(app.direct_commands.runs()))
        try:
            # With an in-flight record, /interrupt targets only that turn and must not kill the command.
            app._interrupt_session(session)
            self.assertEqual(calls, [session.session_id])
            self.assertEqual(run.status, "running")
        finally:
            app.direct_commands.interrupt(run.turn_id)

    def test_a_failing_manifest_is_skipped_and_does_not_block_the_gateway(self) -> None:
        (self.project / "extensions" / "broken").mkdir(parents=True, exist_ok=True)
        (self.project / "extensions" / "broken" / "manifest.json").write_text(
            "{not json", encoding="utf-8"
        )
        write_extension(self.project, "demo", "print('ok')\n")
        app = self.make_app()
        self.assertEqual(sorted(app.commands), ["demo"])

    def test_collision_with_a_builtin_command_is_rejected(self) -> None:
        write_extension(self.project, "sneaky", "print('hijacked')\n", command="new")
        app = self.make_app()
        self.assertEqual(app.commands, {})
        app._handle_command(1, "new", "pi")
        self.assertFalse(any("hijacked" in text for text in self.sent))

    def test_two_commands_in_one_chat_can_run_concurrently(self) -> None:
        write_extension(self.project, "demo", "import time\ntime.sleep(5)\n", command="demo")
        write_extension(self.project, "other", "import time\ntime.sleep(5)\n", command="other")
        app = self.make_app()
        app._handle_command(1, "demo", "")
        app._handle_command(1, "other", "")
        runs = app.direct_commands.runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(app.direct_commands.running_for_chat(1)), 2)
        # Only the newest run owns the bottom card; the other one is background.
        self.assertFalse(app._direct_run_is_foreground(runs[0]))
        self.assertTrue(app._direct_run_is_foreground(runs[1]))
        for run in runs:
            app.direct_commands.interrupt(run.turn_id)

    def test_duplicate_run_of_the_same_command_is_refused(self) -> None:
        write_extension(self.project, "demo", "import time\ntime.sleep(5)\n")
        app = self.make_app()
        app._handle_command(1, "demo", "")
        app._handle_command(1, "demo", "")
        self.assertTrue(any("is already running" in text for text in self.sent))
        for run in app.direct_commands.runs():
            app.direct_commands.interrupt(run.turn_id)

    def test_shutdown_interrupts_running_commands(self) -> None:
        write_extension(self.project, "demo", "import time\ntime.sleep(30)\n")
        app = self.make_app()
        app._handle_command(1, "demo", "")
        run = next(iter(app.direct_commands.runs()))
        app.stop()
        self.wait_for_status(run)
        self.assertEqual(run.status, "interrupted")

    def test_help_text_lists_installed_commands(self) -> None:
        write_extension(self.project, "demo", "print('ok')\n")
        app = self.make_app()
        app._handle_command(1, "help", "")
        self.assertTrue(any("Direct command extensions" in text for text in self.sent))
        self.assertTrue(any("/demo <text>" in text for text in self.sent))

    def test_help_text_without_commands_says_so(self) -> None:
        app = self.make_app()
        app._handle_command(1, "help", "")
        self.assertTrue(any("No direct command extensions are installed" in text for text in self.sent))

    def test_command_dir_from_config_is_also_scanned(self) -> None:
        external = self.project / "external-commands"
        target = external / "demo"
        target.mkdir(parents=True)
        (target / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (target / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "demo",
                    "command": "demo",
                    "exec": ["python3", "main.py"],
                    "cwd": str(external),
                }
            ),
            encoding="utf-8",
        )
        app = self.make_app(command_dir=external)
        self.assertEqual(sorted(app.commands), ["demo"])


class OversizedArtifactTests(unittest.TestCase):
    """An artifact over the Bot API upload limit can be returned as a path but not sent as a file."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)

    def make_app(self, limit: int, local_api_url: str | None = None) -> GatewayApp:
        config = Config(
            project_dir=self.project,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(self.project,),
            default_workdir=self.project,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
            telegram_max_file_bytes=limit,
            telegram_local_api_url=local_api_url,
        )
        app = GatewayApp(config)
        for telegram in app._telegrams.values():
            telegram._call = lambda *_a, **_kw: {"message_id": 1}  # type: ignore[method-assign]
        return app

    def write(self, name: str, size: int) -> Path:
        target = self.project / name
        target.write_bytes(b"x" * size)
        return target

    def test_oversized_artifact_gets_a_path_button_instead_of_a_send_button(self) -> None:
        app = self.make_app(limit=1024)
        session = app.sessions.create_headless("pi", self.project, 1)
        big = self.write("movie.mp4", 4096)
        small = self.write("clip.mp4", 512)
        view = app._register_turn(session, "turn-oversize")
        view.artifacts = [big, small]

        markup = app._artifact_markup(view)
        buttons = markup["inline_keyboard"]  # type: ignore[index]
        self.assertEqual(buttons[0][0]["callback_data"].split(":")[0], "sendpath")
        self.assertIn("Send path", buttons[0][0]["text"])
        self.assertEqual(buttons[1][0]["callback_data"].split(":")[0], "video")

    def test_oversized_artifacts_are_never_auto_sent(self) -> None:
        app = self.make_app(limit=1024)
        session = app.sessions.create_headless("pi", self.project, 1)
        big = self.write("movie.mp4", 4096)
        view = app._register_turn(session, "turn-oversize-auto")
        view.artifacts = [big]
        view.published = True
        sent: list[Path] = []
        app.telegram.send_local_file = (  # type: ignore[method-assign]
            lambda _chat, path, **_kw: sent.append(path)
        )
        app.config = replace(app.config, auto_send_artifacts="all")
        app._auto_send_artifacts(view)
        self.assertEqual(sent, [])

    def test_sendpath_callback_reports_the_path_without_uploading(self) -> None:
        app = self.make_app(limit=1024)
        big = self.write("movie.mp4", 4096)
        token = app.sessions.register_artifact(1, "pi-abc", big, "default", only_path=True)
        sent: list[str] = []
        app._send = lambda _chat, text, *_a, **_kw: sent.append(text)  # type: ignore[method-assign]
        app.telegram.answer_callback_query = lambda *_a, **_kw: None  # type: ignore[method-assign]
        uploads: list[Path] = []
        app.telegram.send_local_file = (  # type: ignore[method-assign]
            lambda _chat, path, **_kw: uploads.append(path)
        )
        app.handle_update(
            {
                "callback_query": {
                    "id": "cb-1",
                    "from": {"id": 1},
                    "data": f"sendpath:{token}",
                    "message": {
                        "message_id": 5,
                        "chat": {"id": 1, "type": "private"},
                    },
                }
            }
        )
        self.assertEqual(uploads, [])
        self.assertEqual(len(sent), 1)
        self.assertIn(str(big), sent[0])
        self.assertIn("exceeds Telegram's send limit", sent[0])

    def test_smaller_default_limit_still_allows_45mb_uploads(self) -> None:
        app = self.make_app(limit=45 * 1024 * 1024)
        self.assertEqual(app.artifact_send_limit, 45 * 1024 * 1024)

    def test_local_one_gib_limit_offers_the_original_video_and_rejects_larger_files(self) -> None:
        app = self.make_app(limit=1024 ** 3, local_api_url="http://127.0.0.1:8081")
        session = app.sessions.create_headless("pi", self.project, 1)
        original = self.project / "original.mp4"
        too_large = self.project / "too-large.mp4"
        for path, size in ((original, 101151627), (too_large, 1024 ** 3 + 1)):
            with path.open("wb") as handle:
                handle.truncate(size)
        view = app._register_turn(session, "turn-local-large")
        view.artifacts = [original, too_large]
        markup = app._artifact_markup(view)
        buttons = markup["inline_keyboard"]  # type: ignore[index]
        self.assertEqual(buttons[0][0]["callback_data"].split(":")[0], "video")
        self.assertEqual(buttons[1][0]["callback_data"].split(":")[0], "sendpath")
        self.assertTrue(app.telegram._local_api)
