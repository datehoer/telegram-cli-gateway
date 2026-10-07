from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from cli_telegram_gateway.app import (
    BOT_COMMANDS,
    HEADLESS_STEER_CONFIRM_SECONDS,
    HELP_TEXT,
    MAX_PENDING_INPUTS_PER_SESSION,
    RESUME_PROMPT,
    STATE_FAILED,
    STATE_IDLE,
    STATE_INTERRUPTED,
    STATE_WORKING,
    GatewayApp,
    PendingInput,
)
from cli_telegram_gateway.config import Config
from cli_telegram_gateway.codex_backend import CodexBackendError
from cli_telegram_gateway.sessions import SessionError
from cli_telegram_gateway.telegram import TelegramError


class FakeCodex:
    def __init__(self) -> None:
        self.responses: list[tuple[str | int, dict[str, Any]]] = []

    def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))


def run_background_inline(app: GatewayApp) -> None:
    """Run off-dispatch actions synchronously; tests of the threads restore it."""
    app._run_in_background = (  # type: ignore[method-assign]
        lambda _name, target, *args: target(*args)
    )


def text_update(
    update_id: int, message_id: int, text: str, date: int = 1700, **extra: Any
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": 1},
            "chat": {"id": 1, "type": "private"},
            "date": date,
            "text": text,
            **extra,
        },
    }


class GatewayEventTests(unittest.TestCase):
    def make_app(
        self,
        project: Path,
        auto_resume: bool = False,
        enabled_clis: tuple[str, ...] = ("claude", "codex", "grok", "pi"),
        bot_tokens: tuple[tuple[str, str], ...] = (),
        cli_default_models: dict[str, str] | None = None,
        cli_default_efforts: dict[str, str] | None = None,
    ) -> GatewayApp:
        config = Config(
            project_dir=project,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(project,),
            default_workdir=project,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
            enabled_clis=enabled_clis,
            auto_resume=auto_resume,
            bot_tokens=bot_tokens,
            cli_default_models=cli_default_models or {},
            cli_default_efforts=cli_default_efforts or {},
        )
        app = GatewayApp(config)
        run_background_inline(app)
        for telegram in app._telegrams.values():
            # The typing indicator is best effort; keep it off the network in tests.
            telegram.send_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        return app

    def test_local_api_endpoint_is_used_for_every_bot_entrance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self.make_app(Path(temporary)).config
            app = GatewayApp(replace(
                config,
                bot_tokens=(("default", "primary:test"), ("worker", "worker:test")),
                telegram_local_api_url="http://127.0.0.1:8081",
                telegram_max_file_bytes=1024 ** 3,
            ))
            self.assertEqual(app._telegrams["default"]._base_url, "http://127.0.0.1:8081/botprimary:test/")
            self.assertEqual(app._telegrams["worker"]._base_url, "http://127.0.0.1:8081/botworker:test/")

    def test_turn_output_stays_with_the_bot_that_started_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project,
                bot_tokens=(("default", "primary:test"), ("worker", "worker:test")),
            )
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.switch(1, session.session_id, "worker")
            sent_by_primary: list[str] = []
            sent_by_worker: list[str] = []
            app._telegrams["default"].send_rich_markdown_parts = (  # type: ignore[method-assign]
                lambda _chat_id, text, reply_markup=None, **_kwargs: sent_by_primary.append(text)
                or [10]
            )
            app._telegrams["worker"].send_rich_markdown_parts = (  # type: ignore[method-assign]
                lambda _chat_id, text, reply_markup=None, **_kwargs: sent_by_worker.append(text)
                or [20]
            )

            view = app._register_turn(session, "turn-worker", "worker")
            view.parts.append("worker result")
            view.status = "completed"
            rendered, _answer = app._render_turn(view, view.started_at + 1)
            app._publish_final_turn(view, rendered, with_artifacts=False)

            self.assertEqual(sent_by_primary, [])
            self.assertEqual(len(sent_by_worker), 1)
            self.assertEqual(view.bot_key, "worker")
            self.assertEqual(
                app.sessions.session_for_message(1, 20, "worker").session_id,  # type: ignore[union-attr]
                session.session_id,
            )

    def test_other_bot_can_request_running_session_snapshot_without_taking_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project,
                bot_tokens=(("default", "primary:test"), ("worker", "worker:test")),
            )
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.switch(1, session.session_id, "worker")
            view = app._register_turn(session, "turn-primary", "default")
            view.parts.append("current progress")
            sent_by_worker: list[str] = []
            app._telegrams["worker"].send_rich_markdown = (  # type: ignore[method-assign]
                lambda _chat_id, text, reply_markup=None: sent_by_worker.append(text) or 30
            )

            with app._bot_scope("worker"):
                app._surface_switched_session(session)

            self.assertEqual(len(sent_by_worker), 1)
            self.assertIn("current progress", sent_by_worker[0])
            self.assertEqual(view.bot_key, "default")
            self.assertEqual(view.message_id, None)

    def test_local_file_commands_are_not_exposed_or_handled(self) -> None:
        command_names = {name for name, _description in BOT_COMMANDS}
        self.assertNotIn("file", command_names)
        self.assertNotIn("photo", command_names)
        self.assertIn("tgstats", command_names)
        self.assertNotIn("/file", HELP_TEXT)
        self.assertNotIn("/photo", HELP_TEXT)
        self.assertIn("/tgstats", HELP_TEXT)

        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            sent: list[str] = []
            app._send = lambda _chat_id, text: sent.append(text)  # type: ignore[method-assign]

            for command in ("/file report.txt", "/photo chart.png"):
                app.handle_update({"message": {
                    "from": {"id": 1},
                    "chat": {"id": 1, "type": "private"},
                    "text": command,
                }})

            self.assertEqual(sent, ["Unknown command. Use /help to see the available commands."] * 2)

    def test_tgstats_command_formats_daily_and_recent_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            app.telegram.metrics_snapshot = lambda: {  # type: ignore[method-assign]
                "utc_day": "2026-08-26",
                "today": {
                    "successful_requests": 12,
                    "failed_requests": 2,
                    "rate_limited": 1,
                    "local_deferrals": 3,
                    "new_messages": 4,
                    "message_edits": 6,
                    "other_writes": 2,
                    "methods": {
                        "editMessageText": {
                            "success": 6,
                            "failed": 1,
                            "rate_limited": 1,
                            "local_deferred": 2,
                        }
                    },
                },
                "last_7_days": {
                    "new_messages": 20,
                    "message_edits": 30,
                    "other_writes": 5,
                    "rate_limited": 2,
                },
                "flood_wait_seconds": 7,
            }
            sent: list[str] = []
            app._send = lambda _chat_id, text: sent.append(text)  # type: ignore[method-assign]

            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/tgstats",
            }})

            self.assertEqual(len(sent), 1)
            self.assertIn("UTC 2026-08-26", sent[0])
            self.assertIn("4 new messages · 6 edits · 2 other", sent[0])
            self.assertIn("1 rate-limited (429) · 3 deferred locally", sent[0])
            self.assertIn("last 7 days", sent[0])
            self.assertIn("flood wait: 7 s", sent[0])
            self.assertIn("editMessageText 10", sent[0])

    def test_run_continues_when_command_refresh_is_rate_limited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            app._acquire_singleton_lock = lambda: None  # type: ignore[method-assign]
            app._prepare_sessions = lambda: None  # type: ignore[method-assign]
            app._recover_interrupted_turns = lambda: None  # type: ignore[method-assign]
            started: list[bool] = []
            closed: list[bool] = []
            app.codex.start = lambda: started.append(True)  # type: ignore[method-assign]
            app.codex.close = lambda: closed.append(True)  # type: ignore[method-assign]
            app.telegram.set_commands = (  # type: ignore[method-assign]
                lambda _commands: (_ for _ in ()).throw(
                    TelegramError("rate limited", retry_after=60)
                )
            )
            app.stop_event.set()

            app.run()

            self.assertEqual(started, [True])
            self.assertEqual(closed, [True])

    def test_new_command_without_args_shows_enabled_cli_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project, enabled_clis=("codex", "pi"))
            sent: list[tuple[str, dict[str, Any]]] = []
            app.telegram.send_message = (  # type: ignore[method-assign]
                lambda _chat_id, text, reply_markup=None: sent.append((text, reply_markup))
            )

            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/new",
            }})

            self.assertEqual(len(sent), 1)
            self.assertIn(f"Default directory: {project}", sent[0][0])
            callbacks = {
                button["callback_data"]
                for row in sent[0][1]["inline_keyboard"]
                for button in row
            }
            self.assertEqual(callbacks, {"new:codex", "new:pi"})

    def test_new_callback_creates_selected_cli_and_closes_picker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            created: list[tuple[int, str, str | None]] = []
            answered: list[tuple[str, str]] = []
            edited: list[tuple[int, int, str]] = []
            app._new_session = (  # type: ignore[method-assign]
                lambda chat_id, cli, cwd: created.append((chat_id, cli, cwd))
            )
            app.telegram.answer_callback_query = (  # type: ignore[method-assign]
                lambda query_id, text="": answered.append((query_id, text))
            )
            app.telegram.edit_message = (  # type: ignore[method-assign]
                lambda chat_id, message_id, text, reply_markup=None: edited.append(
                    (chat_id, message_id, text)
                )
            )

            app.handle_update({"callback_query": {
                "id": "new-pi",
                "from": {"id": 1},
                "data": "new:pi",
                "message": {"message_id": 42, "chat": {"id": 1, "type": "private"}},
            }})

            self.assertEqual(created, [(1, "pi", None)])
            self.assertEqual(answered, [("new-pi", "Creating pi")])
            self.assertEqual(edited, [(1, 42, "Picked pi; creating…")])

    def test_new_codex_session_uses_gateway_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project,
                cli_default_models={"codex": "gpt-6-astra"},
                cli_default_efforts={"codex": "high"},
            )
            started: list[tuple[str, str | None]] = []
            app.codex.start_thread = (  # type: ignore[method-assign]
                lambda cwd, ephemeral=False, model=None: (
                    started.append((cwd, model)) or "thread-default"
                )
            )
            sent: list[str] = []
            app._send = lambda _chat_id, text: sent.append(text)  # type: ignore[method-assign]

            app._new_session(1, "codex", None)

            self.assertEqual(started, [(str(project), "gpt-6-astra")])
            self.assertIn("Model: gpt-6-astra (gateway default)", sent[0])
            self.assertIn("Reasoning effort: high (gateway default)", sent[0])

    def test_new_callback_rejects_disabled_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project, enabled_clis=("codex",))
            created: list[str] = []
            answered: list[str] = []
            app._new_session = (  # type: ignore[method-assign]
                lambda _chat_id, cli, _cwd: created.append(cli)
            )
            app.telegram.answer_callback_query = (  # type: ignore[method-assign]
                lambda _query_id, text="": answered.append(text)
            )

            app.handle_update({"callback_query": {
                "id": "stale-pi",
                "from": {"id": 1},
                "data": "new:pi",
                "message": {"message_id": 42, "chat": {"id": 1, "type": "private"}},
            }})

            self.assertEqual(created, [])
            self.assertEqual(answered, ["That CLI is no longer available; use /new again"])

    def test_new_callback_rejects_unapproved_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            created: list[str] = []
            answered: list[str] = []
            app._new_session = (  # type: ignore[method-assign]
                lambda _chat_id, cli, _cwd: created.append(cli)
            )
            app.telegram.answer_callback_query = (  # type: ignore[method-assign]
                lambda _query_id, text="": answered.append(text)
            )

            app.handle_update({"callback_query": {
                "id": "unauthorized-pi",
                "from": {"id": 2},
                "data": "new:pi",
                "message": {"message_id": 42, "chat": {"id": 1, "type": "private"}},
            }})

            self.assertEqual(created, [])
            self.assertEqual(answered, ["Not allowed"])

    def test_reload_command_without_args_shows_current_and_all_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            sent: list[tuple[str, dict[str, Any]]] = []
            app.telegram.send_message = (  # type: ignore[method-assign]
                lambda _chat_id, text, reply_markup=None: sent.append((text, reply_markup))
            )

            app._handle_command(1, "reload", "")

            self.assertEqual(len(sent), 1)
            callbacks = {
                button["callback_data"]
                for row in sent[0][1]["inline_keyboard"]
                for button in row
            }
            self.assertEqual(callbacks, {f"reload:{session.session_id}", "reload:all"})
            self.assertIn("Codex is a shared backend", sent[0][0])

    def test_reload_current_codex_restarts_shared_backend_and_resumes_all_threads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project, cli_default_models={"codex": "gpt-6-astra"}
            )
            first = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            second = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-2"
            )
            app.sessions.set_model(second.session_id, "gpt-test")
            app.sessions.create_headless("pi", project, 1)
            lifecycle: list[str] = []
            resumed: list[tuple[str, str | None]] = []
            app.codex.close = lambda: lifecycle.append("close")  # type: ignore[method-assign]
            app.codex.start = lambda: lifecycle.append("start")  # type: ignore[method-assign]
            app.codex.resume_thread = (  # type: ignore[method-assign]
                lambda thread_id, model=None: resumed.append((thread_id, model))
            )

            result = app._reload_cli_backends(1, first.session_id)

            self.assertEqual(lifecycle, ["close", "start"])
            self.assertEqual(
                resumed,
                [("thread-1", "gpt-6-astra"), ("thread-2", "gpt-test")],
            )
            self.assertEqual(result, "Codex backend restarted; resumed 2 sessions.")
            self.assertEqual(app.sessions.get(first.session_id).external_id, "thread-1")  # type: ignore[union-attr]

    def test_reload_current_headless_uses_new_binary_on_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.codex.close = (  # type: ignore[method-assign]
                lambda: self.fail("headless reload must not restart Codex")
            )

            result = app._reload_cli_backends(1, session.session_id)

            self.assertIn("pi: a new process starts for each task", result)
            self.assertIn("the next task uses the installed version", result)

    def test_reload_all_restarts_resident_backend_and_covers_headless_clis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            lifecycle: list[str] = []
            resumed: list[str] = []
            app.codex.close = lambda: lifecycle.append("close")  # type: ignore[method-assign]
            app.codex.start = lambda: lifecycle.append("start")  # type: ignore[method-assign]
            app.codex.resume_thread = (  # type: ignore[method-assign]
                lambda thread_id, model=None: resumed.append(thread_id)
            )

            result = app._reload_cli_backends(1, "all")

            self.assertEqual(lifecycle, ["close", "start"])
            self.assertEqual(resumed, ["thread-1"])
            self.assertIn("Codex backend restarted", result)
            self.assertIn("claude, grok, pi: a new process starts for each task", result)

    def test_reload_all_rejects_when_a_headless_turn_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            busy = app.sessions.create_headless("pi", project, 1)
            app.headless.is_active = (  # type: ignore[method-assign]
                lambda session_id: session_id == busy.session_id
            )
            app.codex.close = (  # type: ignore[method-assign]
                lambda: self.fail("reload all must be atomic while a CLI is busy")
            )

            with self.assertRaisesRegex(SessionError, busy.label):
                app._reload_cli_backends(1, "all")

    def test_reload_current_codex_rejects_when_any_codex_thread_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            idle = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-idle"
            )
            busy = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-busy"
            )
            app._codex_active_turns["thread-busy"] = "turn-1"
            app.codex.close = (  # type: ignore[method-assign]
                lambda: self.fail("busy backend must not be restarted")
            )

            with self.assertRaisesRegex(SessionError, busy.label):
                app._reload_cli_backends(1, idle.session_id)

    def test_reload_callback_uses_session_captured_by_picker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            selected = app.sessions.create_headless("pi", project, 1)
            app.sessions.create_headless("claude", project, 1)
            targets: list[tuple[int, str]] = []
            answers: list[tuple[str, str]] = []
            edits: list[tuple[int, int, str]] = []
            app._reload_cli_backends = (  # type: ignore[method-assign]
                lambda chat_id, target: targets.append((chat_id, target)) or "reloaded"
            )
            app.telegram.answer_callback_query = (  # type: ignore[method-assign]
                lambda query_id, text="": answers.append((query_id, text))
            )
            app.telegram.edit_message = (  # type: ignore[method-assign]
                lambda chat_id, message_id, text, reply_markup=None: edits.append(
                    (chat_id, message_id, text)
                )
            )

            app.handle_update({"callback_query": {
                "id": "reload-current",
                "from": {"id": 1},
                "data": f"reload:{selected.session_id}",
                "message": {"message_id": 42, "chat": {"id": 1, "type": "private"}},
            }})

            self.assertEqual(targets, [(1, selected.session_id)])
            self.assertEqual(answers, [("reload-current", "Reloading")])
            self.assertEqual(edits, [(1, 42, "reloaded")])

    def test_codex_deltas_accumulate_and_render_as_one_completed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_virtual("codex", project, 1, "codex-app-server", "thread-1")
            app._on_codex_notification(
                "turn/started", {"threadId": "thread-1", "turn": {"id": "turn-1"}}
            )
            for delta in ("hello ", "world"):
                app._on_codex_notification(
                    "item/agentMessage/delta",
                    {"threadId": "thread-1", "turnId": "turn-1", "delta": delta},
                )
            app._on_codex_notification(
                "turn/completed",
                {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
            )
            view = app._turns[(session.session_id, "turn-1")]
            rendered, answer = app._render_turn(view, view.started_at + 2)
            self.assertEqual(answer, "hello world")
            self.assertIn("Done 2s", rendered)
            self.assertNotIn("thread-1", app._codex_active_turns)

    def test_disabled_cli_cannot_create_or_switch_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            disabled = app.sessions.create_headless("claude", project, 1)
            enabled = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-enabled"
            )
            app.config = Config(
                project_dir=project,
                bot_token="test",
                allowed_user_ids=frozenset({1}),
                allow_groups=False,
                allowed_roots=(project,),
                default_workdir=project,
                cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
                poll_timeout=1,
                enabled_clis=("codex",),
            )
            sent: list[str] = []
            app._send = lambda _chat_id, text: sent.append(text)  # type: ignore[method-assign]
            app._new_session(1, "claude", None)
            self.assertIn("claude is not enabled", sent[-1])
            app._handle_command(1, "use", disabled.session_id)
            self.assertEqual(app.sessions.current(1).session_id, enabled.session_id)  # type: ignore[union-attr]
            app._send_to_session(1, disabled, "do not run")
            self.assertIn("this session cannot continue", sent[-1])

    def test_codex_approval_is_automatically_accepted_for_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            fake_codex = FakeCodex()
            app.codex = fake_codex  # type: ignore[assignment]
            app._on_codex_server_request(
                9, "item/commandExecution/requestApproval", {"command": "example-command"}
            )
            self.assertEqual(fake_codex.responses, [(9, {"decision": "acceptForSession"})])

    def test_stop_marks_codex_shutdown_before_stopping_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            self.assertFalse(app.codex._closing.is_set())
            app.stop()
            self.assertTrue(app.codex._closing.is_set())
            self.assertTrue(app.stop_event.is_set())

    def test_photo_update_downloads_and_forwards_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("claude", project, 1)
            downloaded: list[tuple[str, Path]] = []

            def fake_download(file_id: str, destination: Path, _max_bytes: int) -> int:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"jpg")
                downloaded.append((file_id, destination))
                return 3

            forwarded: list[tuple[int, str, tuple[Any, ...]]] = []
            app.telegram.download_file = fake_download  # type: ignore[method-assign]
            app._send_to_session = (  # type: ignore[method-assign]
                lambda chat_id, _session, text, attachments=(): forwarded.append(
                    (chat_id, text, attachments)
                )
            )
            app.handle_update(
                {
                    "message": {
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "caption": "look at this image",
                        "photo": [{"file_id": "small", "file_size": 1}, {"file_id": "large", "file_size": 9}],
                    }
                }
            )
            self.assertEqual(downloaded[0][0], "large")
            self.assertEqual(forwarded[0][1], "look at this image")
            self.assertTrue(forwarded[0][2][0].is_image)
            self.assertEqual(app.sessions.current(1).session_id, session.session_id)  # type: ignore[union-attr]

    def test_audio_video_and_voice_updates_are_forwarded_as_files(self) -> None:
        """WAV usually arrives as audio; video, voice, video notes and GIFs are files too and reach the CLI."""
        gif = {"file_id": "g", "file_name": "cat.gif.mp4", "mime_type": "video/mp4"}
        cases: list[tuple[dict[str, Any], str, str]] = [
            ({"audio": {"file_id": "a", "file_name": "take.wav", "mime_type": "audio/x-wav"}},
             "take.wav", "audio/x-wav"),
            ({"video": {"file_id": "v", "file_name": "clip.mov", "mime_type": "video/quicktime"}},
             "clip.mov", "video/quicktime"),
            ({"voice": {"file_id": "o", "file_unique_id": "u1", "mime_type": "audio/ogg"}},
             "voice-u1.", "audio/ogg"),
            ({"video_note": {"file_id": "n", "file_unique_id": "u2"}}, "video_note-u2.mp4", "video/mp4"),
            # The Bot API sends a GIF as both animation and document; it counts as one attachment.
            ({"animation": gif, "document": gif}, "cat.gif.mp4", "video/mp4"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            app.sessions.create_headless("claude", project, 1)

            def fake_download(_file_id: str, destination: Path, _max_bytes: int) -> int:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"data")
                return 4

            forwarded: list[tuple[int, str, tuple[Any, ...]]] = []
            sent: list[str] = []
            app.telegram.download_file = fake_download  # type: ignore[method-assign]
            app._send = lambda _chat_id, text: sent.append(text)  # type: ignore[method-assign]
            app._send_to_session = (  # type: ignore[method-assign]
                lambda chat_id, _session, text, attachments=(): forwarded.append(
                    (chat_id, text, attachments)
                )
            )
            for fields, name_prefix, mime_type in cases:
                with self.subTest(kind=next(iter(fields))):
                    forwarded.clear()
                    app.handle_update(
                        {"message": {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}, **fields}}
                    )
                    self.assertEqual(len(forwarded), 1)
                    self.assertEqual(forwarded[0][1], "Please review and handle this attachment.")
                    (attachment,) = forwarded[0][2]
                    self.assertTrue(attachment.name.startswith(name_prefix), attachment.name)
                    self.assertEqual(attachment.mime_type, mime_type)
                    self.assertFalse(attachment.is_image)
                    self.assertTrue(attachment.path.name.endswith(attachment.name))
            self.assertEqual(sent, [])

    def test_sticker_is_not_forwarded_to_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            app.sessions.create_headless("claude", project, 1)
            sent: list[str] = []
            forwarded: list[Any] = []
            app._send = lambda _chat_id, text: sent.append(text)  # type: ignore[method-assign]
            app._send_to_session = lambda *args: forwarded.append(args)  # type: ignore[method-assign]
            app.handle_update(
                {
                    "message": {
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "sticker": {"file_id": "s", "type": "regular"},
                    }
                }
            )
            self.assertEqual(forwarded, [])
            self.assertEqual(len(sent), 1)
            self.assertIn("stickers", sent[0])

    def test_media_group_is_coalesced_into_single_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            updates = [
                {
                    "update_id": 10,
                    "message": {
                        "message_id": 1,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "media_group_id": "mg-1",
                        "caption": "look at these images",
                        "photo": [{"file_id": "a"}],
                    },
                },
                {
                    "update_id": 11,
                    "message": {
                        "message_id": 2,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "media_group_id": "mg-1",
                        "photo": [{"file_id": "b"}],
                    },
                },
                {
                    "update_id": 12,
                    "message": {
                        "message_id": 3,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "media_group_id": "mg-1",
                        "document": {"file_id": "c", "file_name": "c.txt"},
                    },
                },
                {
                    "update_id": 13,
                    "message": {
                        "message_id": 4,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "text": "after album",
                    },
                },
            ]
            coalesced = app._coalesce_updates(updates)
            self.assertEqual(len(coalesced), 2)
            merged = coalesced[0]
            self.assertEqual(merged["update_id"], 12)
            message = merged["message"]
            self.assertEqual(message["media_group_id"], "mg-1")
            self.assertEqual(message["caption"], "look at these images")
            self.assertEqual(
                [item["photo"][0]["file_id"] if "photo" in item else item["document"]["file_id"]
                 for item in message["media"]],
                ["a", "b", "c"],
            )
            self.assertNotIn("photo", message)
            self.assertNotIn("document", message)
            self.assertEqual(coalesced[1], updates[3])

    def test_client_split_long_text_is_stitched_back_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            tail = "end of the long message"
            updates = [
                {
                    "update_id": 20,
                    "message": {
                        "message_id": 100,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "date": 1700,
                        "text": "a" * 4096,
                    },
                },
                {
                    "update_id": 21,
                    "message": {
                        "message_id": 101,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "date": 1701,
                        "text": tail,
                    },
                },
            ]
            coalesced = app._coalesce_text_fragments(updates)
            self.assertEqual(len(coalesced), 1)
            self.assertEqual(coalesced[0]["update_id"], 21)
            self.assertEqual(coalesced[0]["message"]["text"], "a" * 4096 + tail)
            # Single-part text is unaffected
            self.assertEqual(app._coalesce_text_fragments([updates[1]]), [updates[1]])

    def test_ordinary_consecutive_messages_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            updates = [
                {
                    "update_id": 30,
                    "message": {
                        "message_id": 200,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "date": 1700,
                        "text": "first normal message",
                    },
                },
                {
                    "update_id": 31,
                    "message": {
                        "message_id": 201,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "date": 1701,
                        "text": "second normal message",
                    },
                },
            ]
            self.assertEqual(app._coalesce_text_fragments(updates), updates)

    def test_reply_continuation_parts_are_not_stitched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            reply = {"message_id": 900}
            updates = [
                {
                    "update_id": 50,
                    "message": {
                        "message_id": 400,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "date": 1700,
                        "reply_to_message": reply,
                        "text": "y" * 4096,
                    },
                },
                {
                    "update_id": 51,
                    "message": {
                        "message_id": 401,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "date": 1701,
                        "reply_to_message": reply,
                        "text": "separate reply",
                    },
                },
            ]
            self.assertEqual(app._coalesce_text_fragments(updates), updates)

    def test_long_reply_keeps_its_reply_route_after_stitching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            reply = {"message_id": 900}
            updates = [
                {
                    "update_id": 60,
                    "message": {
                        "message_id": 500,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "date": 1700,
                        "reply_to_message": reply,
                        "text": "z" * 4096,
                    },
                },
                {
                    "update_id": 61,
                    "message": {
                        "message_id": 501,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "date": 1701,
                        "text": "rest of the reply",
                    },
                },
            ]
            coalesced = app._coalesce_text_fragments(updates)
            self.assertEqual(len(coalesced), 1)
            self.assertEqual(coalesced[0]["message"]["reply_to_message"], reply)
            self.assertEqual(
                coalesced[0]["message"]["text"], "z" * 4096 + "rest of the reply"
            )

    def test_split_long_text_queues_one_turn_instead_of_steering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            app.sessions.create_headless("pi", project, 1)
            turns: list[str] = []
            app._send_to_current = (  # type: ignore[method-assign]
                lambda _chat_id, text, _attachments=(): turns.append(text)
            )
            updates = [
                {
                    "update_id": 40,
                    "message": {
                        "message_id": 300,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "date": 1700,
                        "text": "x" * 4096,
                    },
                },
                {
                    "update_id": 41,
                    "message": {
                        "message_id": 301,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "date": 1701,
                        "text": "last part",
                    },
                },
            ]
            for update in app._coalesce_text_fragments(updates):
                app.handle_update(update)
            self.assertEqual(turns, ["x" * 4096 + "last part"])

    def test_desktop_split_parts_restore_the_trimmed_line_breaks(self) -> None:
        # Telegram Desktop breaks between 2048 and 4096 at paragraphs or newlines and the server
        # strips the whitespace there, so non-final parts are often well under 4000.
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            head = "\n".join(f"const value{n} = {n};" for n in range(120))
            css = ".card{color:red}" * 190
            parts = [head, css, "tail();"]
            self.assertTrue(all(2048 < len(part) < 4000 for part in parts[:2]))
            updates = [
                text_update(90 + index, 900 + index, part)
                for index, part in enumerate(parts)
            ]
            coalesced = app._coalesce_text_fragments(updates)
            self.assertEqual(len(coalesced), 1)
            self.assertEqual(coalesced[0]["update_id"], 92)
            self.assertEqual(coalesced[0]["message"]["text"], f"{head}\n{css}\ntail();")

    def test_single_line_split_parts_are_joined_with_a_space(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            prose = " ".join(["word"] * 600)
            updates = [
                text_update(100, 1000, prose, entities=[{"type": "bold", "offset": 0, "length": 4}]),
                text_update(101, 1001, "the end."),
            ]
            coalesced = app._coalesce_text_fragments(updates)
            self.assertEqual(len(coalesced), 1)
            self.assertEqual(coalesced[0]["message"]["text"], prose + " the end.")
            # Entity offsets are invalid after joining.
            self.assertNotIn("entities", coalesced[0]["message"])

    def test_hard_cut_is_measured_in_utf16_units(self) -> None:
        # 2048 emoji are exactly 4096 UTF-16 units: the client cuts hard here, so no whitespace is restored.
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            emoji = "😀" * 2048
            updates = [text_update(110, 1100, emoji), text_update(111, 1101, "tail")]
            coalesced = app._coalesce_text_fragments(updates)
            self.assertEqual(len(coalesced), 1)
            self.assertEqual(coalesced[0]["message"]["text"], emoji + "tail")

    def test_forwarded_long_messages_are_not_stitched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            origin = {"type": "hidden_user", "sender_user_name": "someone", "date": 1600}
            updates = [
                text_update(120, 1200, "f" * 3000, forward_origin=origin),
                text_update(121, 1201, "g" * 3000, forward_origin=origin),
            ]
            self.assertEqual(app._coalesce_text_fragments(updates), updates)

    def test_stitched_text_stays_below_the_argv_limit(self) -> None:
        # claude -p and pi take the prompt through argv; one argument cannot exceed 128 KiB.
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            updates = [
                text_update(130 + index, 1300 + index, "汉" * 4096) for index in range(10)
            ]
            coalesced = app._coalesce_text_fragments(updates)
            self.assertEqual(len(coalesced), 2)
            first = coalesced[0]["message"]["text"]
            self.assertEqual(first, "汉" * 4096 * 8)
            self.assertLess(len(first.encode()), 128 * 1024)
            self.assertEqual(coalesced[1]["message"]["text"], "汉" * 4096 * 2)

    def test_poll_waits_for_split_text_from_later_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            head = "\n".join(f"line {n};" for n in range(300))
            middle = "x" * 3000
            parts = [
                text_update(70, 600, head),
                text_update(71, 601, middle),
                text_update(72, 602, "done"),
            ]
            # The long poll first returns only the first part; later re-polls add the following parts.
            responses = [parts[:1], parts[:1], parts[:2], parts[:3]]
            calls: list[tuple[int | None, int]] = []

            def fake_get_updates(offset: int | None, timeout: int) -> list[dict[str, Any]]:
                calls.append((offset, timeout))
                if responses:
                    return responses.pop(0)
                app.stop_event.set()
                return []

            dispatched: list[dict[str, Any]] = []
            app.telegram.get_updates = fake_get_updates  # type: ignore[method-assign]
            app._dispatch_update = (  # type: ignore[method-assign]
                lambda _bot_key, update: dispatched.append(update)
            )
            with mock.patch("cli_telegram_gateway.app.TEXT_FRAGMENT_POLL_SECONDS", 0.001):
                app._poll_bot("default")

            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0]["update_id"], 72)
            self.assertEqual(dispatched[0]["message"]["text"], f"{head}\n{middle}\ndone")
            # While waiting, re-polls keep the original offset, so parts are not confirmed before dispatch.
            self.assertEqual(calls, [(None, 1), (None, 0), (None, 0), (None, 0), (73, 1)])
            self.assertEqual(app.sessions.get_telegram_offset("default"), 73)

    def test_poll_dispatches_a_lone_long_message_after_the_quiet_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            lone = text_update(80, 700, "y" * 2500)
            calls: list[tuple[int | None, int]] = []

            def fake_get_updates(offset: int | None, timeout: int) -> list[dict[str, Any]]:
                calls.append((offset, timeout))
                if offset == 81:
                    app.stop_event.set()
                    return []
                return [lone]

            dispatched: list[dict[str, Any]] = []
            app.telegram.get_updates = fake_get_updates  # type: ignore[method-assign]
            app._dispatch_update = (  # type: ignore[method-assign]
                lambda _bot_key, update: dispatched.append(update)
            )
            started = time.monotonic()
            with (
                mock.patch("cli_telegram_gateway.app.TEXT_FRAGMENT_POLL_SECONDS", 0.005),
                mock.patch("cli_telegram_gateway.app.TEXT_FRAGMENT_QUIET_SECONDS", 0.05),
            ):
                app._poll_bot("default")

            self.assertEqual(dispatched, [lone])
            self.assertGreaterEqual(time.monotonic() - started, 0.05)
            self.assertIn((None, 0), calls)
            self.assertEqual(calls[-1], (81, 1))

    def test_poll_does_not_wait_after_ordinary_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            short = text_update(85, 750, "hello")
            calls: list[tuple[int | None, int]] = []

            def fake_get_updates(offset: int | None, timeout: int) -> list[dict[str, Any]]:
                calls.append((offset, timeout))
                if offset == 86:
                    app.stop_event.set()
                    return []
                return [short]

            dispatched: list[dict[str, Any]] = []
            app.telegram.get_updates = fake_get_updates  # type: ignore[method-assign]
            app._dispatch_update = (  # type: ignore[method-assign]
                lambda _bot_key, update: dispatched.append(update)
            )
            app._poll_bot("default")

            self.assertEqual(dispatched, [short])
            self.assertEqual(calls, [(None, 1), (86, 1)])

    def test_media_group_forwards_all_attachments_in_one_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("claude", project, 1)
            downloaded: list[str] = []

            def fake_download(file_id: str, destination: Path, _max_bytes: int) -> int:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"data")
                downloaded.append(file_id)
                return 4

            forwarded: list[tuple[int, str, tuple[Any, ...]]] = []
            app.telegram.download_file = fake_download  # type: ignore[method-assign]
            app._send_to_session = (  # type: ignore[method-assign]
                lambda chat_id, _session, text, attachments=(): forwarded.append(
                    (chat_id, text, attachments)
                )
            )
            app.handle_update(
                {
                    "message": {
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "media_group_id": "mg-1",
                        "caption": "together",
                        "media": [
                            {"photo": [{"file_id": "p1", "file_size": 2}]},
                            {"document": {"file_id": "d1", "file_name": "a.pdf"}},
                        ],
                    }
                }
            )
            self.assertEqual(downloaded, ["p1", "d1"])
            self.assertEqual(len(forwarded), 1)
            self.assertEqual(forwarded[0][1], "together")
            self.assertEqual(len(forwarded[0][2]), 2)
            self.assertTrue(forwarded[0][2][0].is_image)
            self.assertFalse(forwarded[0][2][1].is_image)
            self.assertEqual(app.sessions.current(1).session_id, session.session_id)  # type: ignore[union-attr]

    def test_media_group_without_caption_uses_default_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            app.sessions.create_headless("claude", project, 1)

            def fake_download(file_id: str, destination: Path, _max_bytes: int) -> int:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"data")
                return 4

            forwarded: list[tuple[int, str, tuple[Any, ...]]] = []
            app.telegram.download_file = fake_download  # type: ignore[method-assign]
            app._send_to_session = (  # type: ignore[method-assign]
                lambda chat_id, _session, text, attachments=(): forwarded.append(
                    (chat_id, text, attachments)
                )
            )
            app.handle_update(
                {
                    "message": {
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "media": [{"document": {"file_id": "d2", "file_name": "b.txt"}}],
                    }
                }
            )
            self.assertEqual(forwarded[0][1], "Please review and handle these attachments.")

    def test_audio_album_is_coalesced_and_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            app.sessions.create_headless("claude", project, 1)
            downloaded: list[str] = []

            def fake_download(file_id: str, destination: Path, _max_bytes: int) -> int:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"RIFF")
                downloaded.append(file_id)
                return 4

            forwarded: list[tuple[int, str, tuple[Any, ...]]] = []
            app.telegram.download_file = fake_download  # type: ignore[method-assign]
            app._send_to_session = (  # type: ignore[method-assign]
                lambda chat_id, _session, text, attachments=(): forwarded.append(
                    (chat_id, text, attachments)
                )
            )
            updates = [
                {
                    "update_id": 20 + index,
                    "message": {
                        "message_id": 1 + index,
                        "from": {"id": 1},
                        "chat": {"id": 1, "type": "private"},
                        "media_group_id": "mg-audio",
                        **({"caption": "mixed"} if index == 0 else {}),
                        "audio": {"file_id": f"a{index}", "file_name": name, "mime_type": "audio/x-wav"},
                    },
                }
                for index, name in enumerate(["drums.wav", "bass.wav"])
            ]
            (merged,) = app._coalesce_updates(updates)
            self.assertEqual(merged["update_id"], 21)
            self.assertNotIn("audio", merged["message"])
            app.handle_update(merged)
            self.assertEqual(downloaded, ["a0", "a1"])
            self.assertEqual(len(forwarded), 1)
            self.assertEqual(forwarded[0][1], "mixed")
            self.assertEqual([item.name for item in forwarded[0][2]], ["drums.wav", "bass.wav"])

    def test_running_turn_render_includes_command_and_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-1")
            app._update_turn(session, "turn-1", "command", "ls -la")
            app._update_turn(session, "turn-1", "command_output", "file.txt")
            rendered, _answer = app._render_turn(view, view.started_at + 7)
            self.assertIn("Running 7s", rendered)
            self.assertIn("Current command:\n```text\nls -la\n```", rendered)
            self.assertIn("file.txt", rendered)

    def test_status_loop_does_not_edit_without_new_cli_events(self) -> None:
        """Without new CLI output the preview stays as it is, so the timer alone never keeps calling Telegram."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-waiting")
            view.published = True
            view.live = True
            view.dirty = False
            view.last_edit = -10.0
            published: list[str] = []
            app._publish_running_turn = (  # type: ignore[method-assign]
                lambda _view, rendered: published.append(rendered)
            )

            class StopAfterOneIteration:
                calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 1

            app.stop_event = StopAfterOneIteration()  # type: ignore[assignment]
            app._status_loop()

            self.assertEqual(published, [])

    def test_status_loop_publishes_dirty_running_turn_after_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-dirty")
            view.published = True
            view.live = True
            view.dirty = True
            view.last_edit = -10.0
            published: list[str] = []
            app._publish_running_turn = (  # type: ignore[method-assign]
                lambda _view, rendered: published.append(rendered)
            )

            class StopAfterOneIteration:
                calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 1

            app.stop_event = StopAfterOneIteration()  # type: ignore[assignment]
            app._status_loop()

            self.assertEqual(len(published), 1)
            self.assertIn("Running", published[0])

    def test_completion_during_running_publish_still_publishes_final_status(self) -> None:
        """When completed arrives during a running edit, the next pass must publish the final state instead of dropping the view."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-race")
            view.parts.append("complete answer")
            view.message_id = 88
            view.published = True
            view.live = True
            view.last_edit = -10.0
            app.sessions.set_in_flight(session.session_id, session.chat_id, view.turn_id)

            running_updates: list[str] = []
            final_updates: list[str] = []

            def publish_running(_view: object, rendered: str) -> None:
                running_updates.append(rendered)
                app._update_turn(session, view.turn_id, "completed")

            app._publish_running_turn = publish_running  # type: ignore[method-assign]
            app._publish_final_turn = (  # type: ignore[method-assign]
                lambda _view, rendered, with_artifacts=True, **_kwargs: final_updates.append(rendered)
            )

            class StopAfterTwoIterations:
                calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 2

            app.stop_event = StopAfterTwoIterations()  # type: ignore[assignment]
            app._status_loop()

            self.assertEqual(len(running_updates), 1)
            self.assertIn("Running", running_updates[0])
            self.assertEqual(len(final_updates), 1)
            self.assertIn("Done", final_updates[0])
            self.assertNotIn((session.session_id, view.turn_id), app._turns)
            self.assertEqual(app.sessions.stale_in_flight(), [])
            saved = app.sessions.get(session.session_id)
            self.assertEqual(saved.last_completed_message_id, 88)  # type: ignore[union-attr]

    def test_switching_away_backgrounds_running_turn(self) -> None:
        """After switching away, a running turn freezes as "running in background" and stops streaming."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            first = app.sessions.create_headless("pi", project, 1)
            app.sessions.create_headless("pi", project, 1)  # the second becomes current, switching away from the first
            view = app._register_turn(first, "turn-a")
            view.published = True
            view.live = True
            view.dirty = True
            view.last_edit = -10.0
            view.message_id = 88

            edited: list[tuple[int, str]] = []
            app.telegram.edit_rich_markdown = (  # type: ignore[method-assign]
                lambda chat_id, message_id, markdown, reply_markup=None: edited.append(
                    (message_id, markdown)
                )
            )
            published: list[str] = []
            app._publish_running_turn = (  # type: ignore[method-assign]
                lambda _view, rendered: published.append(rendered)
            )

            class StopAfterOneIteration:
                calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 1

            app.stop_event = StopAfterOneIteration()  # type: ignore[assignment]
            app._status_loop()

            self.assertEqual(len(edited), 1)
            self.assertEqual(edited[0][0], 88)
            self.assertIn("Running in background", edited[0][1])
            self.assertEqual(published, [])
            self.assertFalse(view.live)

    def test_background_completion_edits_old_card_without_dumping_chat(self) -> None:
        """A background task that finishes only updates its own card; nothing is posted at the bottom of the current session."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            image = project / "chart.png"
            image.write_bytes(b"\x89PNG fake")
            report = project / "report.py"
            report.write_text("print('hi')\n", encoding="utf-8")
            app = self.make_app(project)
            first = app.sessions.create_headless("pi", project, 1)
            app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(first, "turn-bg-done")
            view.parts.append(f"Saved `{image}` and `{report}`")
            view.status = "completed"
            view.published = True
            view.live = False
            view.message_id = 88
            view.dirty = True

            edited: list[tuple[int, str, bool]] = []
            sent: list[str] = []
            files: list[Path] = []
            app._edit_turn_cards = (  # type: ignore[method-assign]
                lambda _view, rendered, _markup=None, *, rich, allow_split: edited.append(
                    (88, rendered, allow_split)
                )
                or [88]
            )
            app._send_turn_cards = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: sent.append("sent") or [99]
            )
            app.telegram.send_local_file = (  # type: ignore[method-assign]
                lambda _chat_id, path, as_photo=False: files.append(path)
            )

            class StopAfterOneIteration:
                calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 1

            app.stop_event = StopAfterOneIteration()  # type: ignore[assignment]
            app._status_loop()

            self.assertEqual(sent, [])
            self.assertEqual(files, [])
            self.assertEqual(len(edited), 1)
            self.assertFalse(edited[0][2])
            self.assertIn("Done", edited[0][1])
            self.assertNotIn((first.session_id, view.turn_id), app._turns)
            saved = app.sessions.get(first.session_id)
            self.assertEqual(saved.last_completed_message_id, 88)  # type: ignore[union-attr]

    def test_background_completion_without_card_waits_for_switch_back(self) -> None:
        """Switching away before the first progress card: the result is not pushed into the current chat; it appears on switching back."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            first = app.sessions.create_headless("claude", project, 1)
            app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(first, "turn-deferred")
            view.parts.append("long python agent answer")
            view.status = "completed"
            view.dirty = True

            sent: list[str] = []
            app._send_turn_cards = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: sent.append("sent") or [99]
            )
            app._edit_turn_cards = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: self.fail("no existing card to edit")
            )

            class StopAfterOneIteration:
                def __init__(self) -> None:
                    self.calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 1

            app.stop_event = StopAfterOneIteration()  # type: ignore[assignment]
            app._status_loop()

            self.assertEqual(sent, [])
            self.assertIn((first.session_id, view.turn_id), app._turns)

            app.telegram.answer_callback_query = lambda *_args: None  # type: ignore[method-assign]
            app.telegram.edit_message = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            app.handle_update(
                {
                    "callback_query": {
                        "id": "query-back",
                        "from": {"id": 1},
                        "data": f"use:{first.session_id}",
                        "message": {
                            "message_id": 42,
                            "chat": {"id": 1, "type": "private"},
                        },
                    }
                }
            )
            app.stop_event = StopAfterOneIteration()  # type: ignore[assignment]
            app._status_loop()

            self.assertEqual(sent, ["sent"])
            self.assertNotIn((first.session_id, view.turn_id), app._turns)

    def test_switching_back_pins_running_turn_to_fresh_card(self) -> None:
        """Switching back to a running session pins progress to a new card and marks the old card stale."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-a")
            view.published = True
            view.live = False  # frozen earlier
            view.message_id = 88  # the old card
            view.dirty = True
            view.last_edit = -10.0

            sent: list[str] = []
            app.telegram.send_rich_markdown_parts = (  # type: ignore[method-assign]
                lambda chat_id, markdown, reply_markup=None, **_kwargs: sent.append(markdown)
                or [99]
            )
            edited: list[tuple[int, str]] = []
            app.telegram.edit_rich_markdown = (  # type: ignore[method-assign]
                lambda chat_id, message_id, markdown, reply_markup=None, **_kwargs: edited.append(
                    (message_id, markdown)
                )
                or [message_id]
            )

            class StopAfterOneIteration:
                calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 1

            app.stop_event = StopAfterOneIteration()  # type: ignore[assignment]
            app._status_loop()

            self.assertEqual(len(sent), 1)
            self.assertTrue(view.live)
            self.assertEqual(view.message_id, 99)
            self.assertEqual(view.stale_message_ids, [88])
            self.assertEqual(len(edited), 1)
            self.assertEqual(edited[0][0], 88)
            self.assertIn("Running in background", edited[0][1])

    def test_completed_turn_keeps_full_rich_markdown_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-table")
            table = "| Directory | Notes |\n|---|---|\n" + "\n".join(
                f"| project-{index} | value-{index} |" for index in range(300)
            )
            app._update_turn(session, "turn-table", "delta", table)
            app._update_turn(session, "turn-table", "completed")
            rendered, answer = app._render_turn(view, view.started_at + 3)
            self.assertEqual(answer, table)
            self.assertIn("| project-299 | value-299 |", rendered)
            self.assertGreater(len(rendered), 3900)

    def test_running_rich_message_is_edited_in_place_on_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-rich")
            calls: list[tuple[str, object]] = []
            app.telegram.send_rich_markdown_parts = (  # type: ignore[method-assign]
                lambda chat_id, markdown, reply_markup=None, **_kwargs: calls.append(
                    ("send", (chat_id, markdown, reply_markup))
                ) or [88]
            )
            app.telegram.edit_rich_markdown = (  # type: ignore[method-assign]
                lambda chat_id, message_id, markdown, reply_markup=None, **_kwargs: calls.append(
                    ("edit", (chat_id, message_id, markdown, reply_markup))
                ) or [message_id]
            )
            app._publish_running_turn(view, "partial")
            self.assertTrue(view.published)
            self.assertEqual(view.message_id, 88)
            view.status = "completed"
            app._publish_final_turn(view, "| A | B |\n|---|---|\n| 1 | 2 |")
            self.assertEqual([item[0] for item in calls], ["send", "edit"])
            self.assertEqual(calls[1][1][1], 88)
            self.assertIn("|---|---|", calls[1][1][2])

    def test_rich_stream_failure_falls_back_to_safe_html_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-fallback")
            app.telegram.send_rich_markdown_parts = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: (_ for _ in ()).throw(TelegramError("unsupported"))
            )
            sent: list[str] = []
            app.telegram.send_markdown_parts = (  # type: ignore[method-assign]
                lambda _chat_id, markdown, **_kwargs: sent.append(markdown) or [9]
            )
            app._publish_running_turn(view, "**partial**")
            self.assertFalse(view.rich_mode)
            self.assertEqual(view.message_id, 9)
            self.assertEqual(sent, ["**partial**"])

            edited: list[tuple[int, int, str]] = []
            app.telegram.edit_rich_markdown = (  # type: ignore[method-assign]
                lambda chat_id, message_id, markdown, reply_markup=None, **_kwargs: edited.append(
                    (chat_id, message_id, markdown)
                ) or [message_id]
            )
            view.status = "completed"
            app._publish_final_turn(view, "| A | B |\n|---|---|\n| 1 | 2 |")
            self.assertEqual(edited[0][1], 9)
            self.assertIn("|---|---|", edited[0][2])

    def test_rich_stream_rate_limit_is_retried_without_disabling_rich_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-limited")
            app.telegram.send_rich_markdown_parts = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    TelegramError("rate limited", retry_after=5)
                )
            )
            with self.assertRaises(TelegramError):
                app._publish_running_turn(view, "partial")
            self.assertTrue(view.rich_mode)
            self.assertIsNone(view.message_id)

    def test_start_session_turn_records_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.headless.start_turn = (  # type: ignore[method-assign]
                lambda _session, _text, _attachments=(): "turn-x"
            )
            app._send = lambda *_args: None  # type: ignore[method-assign]
            app._start_session_turn(1, session, "hello")
            self.assertEqual(
                app.sessions.stale_in_flight(), [(session.session_id, 1, "turn-x")]
            )

    def test_in_flight_cleared_on_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.set_in_flight(session.session_id, session.chat_id, "turn-1")
            app._update_turn(session, "turn-1", "completed")
            self.assertEqual(app.sessions.stale_in_flight(), [])

    def test_restart_recovery_reports_interrupted_turns_and_keeps_state(self) -> None:
        """An unexpectedly interrupted task: after a restart the user is offered /resume or /cancel and in_flight stays for that decision."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.set_in_flight(session.session_id, session.chat_id, "turn-1")
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app._recover_interrupted_turns()
            self.assertEqual(len(sent), 1)
            self.assertIn("/resume", sent[0])
            self.assertEqual(
                app.sessions.stale_in_flight(), [(session.session_id, 1, "turn-1")]
            )

    def test_auto_resume_restarts_interrupted_turn_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project, auto_resume=True)
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.set_in_flight(session.session_id, session.chat_id, "turn-1")
            started: list[str] = []
            app._start_session_turn = (  # type: ignore[method-assign]
                lambda _chat, _session, text, _attachments=(): started.append(text)
            )
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app._recover_interrupted_turns()
            self.assertEqual(started, [RESUME_PROMPT])
            self.assertIn("resumed automatically", sent[0])

    def test_unexpected_kill_marks_resumable_and_keeps_in_flight(self) -> None:
        """A process killed unexpectedly (not by the user) is marked resumable, awaits recovery and keeps in_flight."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.set_in_flight(session.session_id, session.chat_id, "turn-1")
            app._register_turn(session, "turn-1")
            app._update_turn(session, "turn-1", "interrupted", 15)
            view = app._turns[(session.session_id, "turn-1")]
            self.assertEqual(view.status, "interrupted")
            self.assertTrue(view.resumable)
            self.assertEqual(app._session_states[session.session_id], STATE_INTERRUPTED)
            self.assertEqual(
                app.sessions.stale_in_flight(), [(session.session_id, 1, "turn-1")]
            )
            rendered, _answer = app._render_turn(view, view.started_at + 2)
            self.assertIn("Interrupted 2s", rendered)
            self.assertIn("/resume", rendered)

    def test_user_interrupt_clears_in_flight_and_ignores_late_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.set_in_flight(session.session_id, session.chat_id, "turn-1")
            app._register_turn(session, "turn-1")
            app.headless.interrupt = lambda _session_id: True  # type: ignore[method-assign]
            self.assertTrue(app._interrupt_session(session))
            self.assertEqual(app.sessions.stale_in_flight(), [])
            # The reader then reports interrupted: user-initiated, so it ends quietly and is no recovery candidate
            app._update_turn(session, "turn-1", "interrupted", 15)
            view = app._turns[(session.session_id, "turn-1")]
            self.assertFalse(view.resumable)
            self.assertEqual(app._session_states[session.session_id], STATE_IDLE)
            self.assertNotIn(session.session_id, app._interrupted_sessions)

    def test_resume_command_restarts_pending_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.set_in_flight(session.session_id, session.chat_id, "turn-1")
            started: list[str] = []
            app._start_session_turn = (  # type: ignore[method-assign]
                lambda _chat, _session, text, _attachments=(): started.append(text)
            )
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/resume",
            }})
            self.assertEqual(started, [RESUME_PROMPT])
            self.assertIn("Resuming", sent[0])

    def test_resume_without_pending_task_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            app.sessions.create_headless("pi", project, 1)
            started: list[str] = []
            app._start_session_turn = (  # type: ignore[method-assign]
                lambda _chat, _session, text, _attachments=(): started.append(text)
            )
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/resume",
            }})
            self.assertEqual(started, [])
            self.assertIn("has no task to resume", sent[0])

    def test_cancel_command_discards_pending_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.set_in_flight(session.session_id, session.chat_id, "turn-1")
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/cancel",
            }})
            self.assertEqual(app.sessions.stale_in_flight(), [])
            self.assertEqual(app._session_states[session.session_id], STATE_IDLE)
            self.assertIn("Gave up", sent[0])

    def test_compact_command_uses_pi_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            compacted: list[str] = []
            app.headless.compact_session = (  # type: ignore[method-assign]
                lambda _session: compacted.append(_session.session_id) or "Context compaction started."
            )
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/compact",
            }})
            self.assertEqual(compacted, [session.session_id])
            self.assertIn("Compacting", sent[0])
            self.assertIn("Context compaction started", sent[1])
            self.assertFalse(app._session_is_busy(session))

    def test_compact_rejects_busy_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.headless.is_active = lambda _session_id: True  # type: ignore[method-assign]
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/compact",
            }})
            self.assertIn("/interrupt it before", sent[0])

    def test_compact_codex_uses_thread_compact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            compacted: list[str] = []
            app.codex.compact_thread = (  # type: ignore[method-assign]
                lambda thread_id: compacted.append(thread_id)
            )
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/compact",
            }})
            self.assertEqual(compacted, ["thread-1"])
            self.assertIn("Started context compaction", sent[0])

    def test_compact_codex_background_turn_is_not_published_by_default_bot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project,
                bot_tokens=(("default", "primary:test"), ("3", "third:test")),
            )
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-30", "3"
            )
            sent_by: list[tuple[str, str]] = []
            for bot_key in ("default", "3"):
                app._telegrams[bot_key].send_message = (  # type: ignore[method-assign]
                    lambda _chat_id, text, bot_key=bot_key: sent_by.append(
                        (bot_key, text)
                    ) or 1
                )

            def compact_thread(thread_id: str) -> None:
                def notify_from_reader_thread() -> None:
                    app._on_codex_notification(
                        "turn/started",
                        {"threadId": thread_id, "turn": {"id": "compact-turn"}},
                    )
                    app._on_codex_notification(
                        "item/started",
                        {
                            "threadId": thread_id,
                            "turnId": "compact-turn",
                            "item": {"type": "contextCompaction"},
                        },
                    )
                    app._on_codex_notification(
                        "turn/completed",
                        {
                            "threadId": thread_id,
                            "turn": {"id": "compact-turn", "status": "completed"},
                        },
                    )

                reader = threading.Thread(target=notify_from_reader_thread)
                reader.start()
                reader.join()

            app.codex.compact_thread = compact_thread  # type: ignore[method-assign]

            with app._bot_scope("3"):
                app.handle_update({"message": {
                    "from": {"id": 1},
                    "chat": {"id": 1, "type": "private"},
                    "text": "/compact",
                }})

            self.assertEqual(
                sent_by,
                [("3", f"Started context compaction for {session.label}.")],
            )
            self.assertNotIn((session.session_id, "compact-turn"), app._turns)
            self.assertNotIn("thread-30", app._codex_active_turns)
            self.assertEqual(app._pending_codex_compactions, {})
            self.assertEqual(app._codex_compaction_turns, {})

    def test_compact_codex_failure_clears_pending_internal_turn_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project,
                bot_tokens=(("default", "primary:test"), ("3", "third:test")),
            )
            app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-30", "3"
            )
            app.codex.compact_thread = (  # type: ignore[method-assign]
                lambda _thread_id: (_ for _ in ()).throw(
                    CodexBackendError("compact failed")
                )
            )
            sent_by: list[tuple[str, str]] = []
            for bot_key in ("default", "3"):
                app._telegrams[bot_key].send_message = (  # type: ignore[method-assign]
                    lambda _chat_id, text, bot_key=bot_key: sent_by.append(
                        (bot_key, text)
                    ) or 1
                )

            with app._bot_scope("3"):
                app.handle_update({"message": {
                    "from": {"id": 1},
                    "chat": {"id": 1, "type": "private"},
                    "text": "/compact",
                }})

            self.assertEqual(sent_by, [("3", "Compaction failed: compact failed")])
            self.assertEqual(app._pending_codex_compactions, {})

    def test_clear_headless_rotates_external_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.set_last_completed_message(session.session_id, 88)
            old_external = session.external_id
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/clear",
            }})
            refreshed = app.sessions.get(session.session_id)
            self.assertIsNotNone(refreshed)
            self.assertNotEqual(refreshed.external_id, old_external)  # type: ignore[union-attr]
            self.assertEqual(refreshed.turn_count, 0)  # type: ignore[union-attr]
            self.assertIsNone(refreshed.last_completed_message_id)  # type: ignore[union-attr]
            self.assertIn("started a new conversation", sent[0])

    def test_clear_codex_starts_and_archives_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-old"
            )
            app.sessions.set_last_completed_message(session.session_id, 88)
            started: list[str] = []
            archived: list[str] = []
            app.codex.start_thread = (  # type: ignore[method-assign]
                lambda cwd, ephemeral=False, model=None: started.append(cwd) or "thread-new"
            )
            app.codex.archive_thread = (  # type: ignore[method-assign]
                lambda thread_id: archived.append(thread_id)
            )
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/clear",
            }})
            self.assertEqual(started, [str(project)])
            self.assertEqual(archived, ["thread-old"])
            refreshed = app.sessions.get(session.session_id)
            self.assertEqual(refreshed.external_id, "thread-new")  # type: ignore[union-attr]
            self.assertIsNone(refreshed.last_completed_message_id)  # type: ignore[union-attr]
            self.assertIn("the old thread was archived", sent[0])

    def test_clear_rejects_busy_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.headless.is_active = lambda _session_id: True  # type: ignore[method-assign]
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/clear",
            }})
            self.assertIn("/interrupt it before", sent[0])
            self.assertEqual(
                app.sessions.get(session.session_id).external_id,  # type: ignore[union-attr]
                session.external_id,
            )

    def test_session_keyboard_callback_switches_session_and_updates_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            first = app.sessions.create_headless("claude", project, 1)
            app.sessions.create_headless("pi", project, 1)
            answered: list[tuple[str, str]] = []
            edited: list[tuple[int, int, str, dict[str, Any]]] = []
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.telegram.answer_callback_query = (  # type: ignore[method-assign]
                lambda query_id, text="": answered.append((query_id, text))
            )
            app.telegram.edit_message = (  # type: ignore[method-assign]
                lambda chat_id, message_id, text, reply_markup=None: edited.append(
                    (chat_id, message_id, text, reply_markup)
                )
            )
            app.handle_update(
                {
                    "callback_query": {
                        "id": "query-1",
                        "from": {"id": 1},
                        "data": f"use:{first.session_id}",
                        "message": {
                            "message_id": 42,
                            "chat": {"id": 1, "type": "private"},
                        },
                    }
                }
            )
            self.assertEqual(app.sessions.current(1).session_id, first.session_id)  # type: ignore[union-attr]
            self.assertEqual(answered, [("query-1", f"Switched to {first.session_id}")])
            self.assertIn(f"▶ {first.session_id}", edited[0][2])
            self.assertTrue(edited[0][3]["inline_keyboard"])
            self.assertEqual(sent, [f"[{first.session_id}] · How can I help?"])

    def test_switching_to_completed_session_copies_last_result_to_bottom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            first = app.sessions.create_headless("claude", project, 1)
            app.sessions.set_last_completed_message(first.session_id, 88)
            app.sessions.create_headless("pi", project, 1)
            copied: list[tuple[int, int, int]] = []
            app.telegram.copy_message = (  # type: ignore[method-assign]
                lambda chat_id, from_chat_id, message_id: copied.append(
                    (chat_id, from_chat_id, message_id)
                ) or 99
            )
            app.telegram.answer_callback_query = lambda *_args: None  # type: ignore[method-assign]
            app.telegram.edit_message = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            app._send = lambda *_args: self.fail("completed result should be copied")  # type: ignore[method-assign]

            app.handle_update(
                {
                    "callback_query": {
                        "id": "query-copy",
                        "from": {"id": 1},
                        "data": f"use:{first.session_id}",
                        "message": {
                            "message_id": 42,
                            "chat": {"id": 1, "type": "private"},
                        },
                    }
                }
            )

            self.assertEqual(copied, [(1, 1, 88)])
            refreshed = app.sessions.get(first.session_id)
            self.assertEqual(refreshed.last_completed_message_id, 99)  # type: ignore[union-attr]
            self.assertEqual(app.sessions.session_for_message(1, 99).session_id, first.session_id)  # type: ignore[union-attr]

    def test_switching_during_terminal_publish_moves_result_to_fresh_card(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            first = app.sessions.create_headless("claude", project, 1)
            app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(first, "turn-complete")
            view.status = "completed"
            view.message_id = 88
            app.telegram.answer_callback_query = lambda *_args: None  # type: ignore[method-assign]
            app.telegram.edit_message = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

            app.handle_update(
                {
                    "callback_query": {
                        "id": "query-terminal",
                        "from": {"id": 1},
                        "data": f"use:{first.session_id}",
                        "message": {
                            "message_id": 42,
                            "chat": {"id": 1, "type": "private"},
                        },
                    }
                }
            )

            self.assertIsNone(view.message_id)
            self.assertEqual(view.stale_message_ids, [88])
            self.assertTrue(view.dirty)

    def test_reply_to_old_result_routes_and_switches_to_its_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            first = app.sessions.create_headless("claude", project, 1)
            app.sessions.bind_message(1, 77, first.session_id)
            app.sessions.create_headless("pi", project, 1)
            forwarded: list[tuple[str, str]] = []
            app._send_to_session = (  # type: ignore[method-assign]
                lambda _chat, session, text, _attachments=(): forwarded.append(
                    (session.session_id, text)
                )
            )
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "continue this",
                "reply_to_message": {"message_id": 77},
            }})
            self.assertEqual(forwarded, [(first.session_id, "continue this")])
            self.assertEqual(app.sessions.current(1).session_id, first.session_id)  # type: ignore[union-attr]

    def test_busy_codex_uses_native_steer_instead_of_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            app._codex_active_turns["thread-1"] = "turn-1"
            view = app._register_turn(session, "turn-1")
            view.live = True
            view.message_id = 41
            steered: list[tuple[str, str, str]] = []
            app.codex.steer_turn = (  # type: ignore[method-assign]
                lambda thread, turn, text, _attachments=(): steered.append(
                    (thread, turn, text)
                ) or turn
            )
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            rich_sent: list[str] = []
            app.telegram.send_rich_markdown_parts = (  # type: ignore[method-assign]
                lambda _chat, markdown, reply_markup=None, **_kwargs: rich_sent.append(markdown)
                or [42]
            )
            edited: list[tuple[int, str]] = []
            app.telegram.edit_rich_markdown = (  # type: ignore[method-assign]
                lambda _chat, message_id, markdown, reply_markup=None, **_kwargs: edited.append(
                    (message_id, markdown)
                ) or [message_id]
            )
            app._send_to_session(1, session, "change direction")
            self.assertEqual(steered, [("thread-1", "turn-1", "change direction")])
            self.assertNotIn(session.session_id, app._queues)
            self.assertEqual(sent, [])
            self.assertEqual(view.steered, 1)
            self.assertTrue(view.live)
            self.assertEqual(view.message_id, 41)
            self.assertEqual(view.stale_message_ids, [])
            self.assertEqual(rich_sent, [])
            self.assertEqual(edited, [])
            self.assertEqual(list(view.steer_notices), [(1, 0)])
            app._publish_steer_notice(view)
            self.assertIn("The CLI accepted", rich_sent[0])
            self.assertIsNone(view.message_id)
            self.assertEqual(view.stale_message_ids, [41])
            self.assertEqual(list(view.steer_notices), [])

            app._publish_running_turn(view, "latest update")
            self.assertEqual(edited, [])
            self.assertEqual(rich_sent[-1], "latest update")

    def test_codex_turn_started_reuses_running_view_instead_of_second_card(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            view = app._register_turn(session, "turn-1")
            view.message_id = 41
            app._codex_active_turns["thread-1"] = "turn-1"
            app._on_codex_notification(
                "turn/started", {"threadId": "thread-1", "turn": {"id": "turn-2"}}
            )
            self.assertEqual(view.turn_id, "turn-2")
            self.assertEqual(app._turns[(session.session_id, "turn-2")], view)
            self.assertNotIn((session.session_id, "turn-1"), app._turns)
            app._on_codex_notification(
                "item/agentMessage/delta",
                {"threadId": "thread-1", "turnId": "turn-1", "delta": "hello"},
            )
            self.assertEqual("".join(view.parts), "hello")
            self.assertEqual(len([item for item in app._turns if item[0] == session.session_id]), 1)

    def test_start_session_turn_while_busy_enqueues_instead_of_second_codex_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            app._codex_active_turns["thread-1"] = "turn-1"
            app._turn_claims.add(session.session_id)
            started: list[str] = []
            app.codex.start_turn = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: started.append("started") or "turn-2"
            )
            sent: list[str] = []
            app._send = lambda _chat, text, _bot=None: sent.append(text)  # type: ignore[method-assign]
            app._start_session_turn(1, session, "sneak")
            self.assertEqual(started, [])
            self.assertEqual(len(app._queues[session.session_id]), 1)
            self.assertIn("queued as #", sent[0])

    def test_turn_completion_keeps_busy_until_queued_start_begins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            app._codex_active_turns["thread-1"] = "turn-1"
            app._turn_claims.add(session.session_id)
            app._register_turn(session, "turn-1")
            app._enqueue(session, PendingInput(1, "queued-next"))
            started = threading.Event()
            release = threading.Event()
            turns: list[str] = []

            def blocking_start(_thread, text, *_args, **_kwargs):
                turns.append(text)
                started.set()
                self.assertTrue(release.wait(5))
                return "turn-2"

            app.codex.start_turn = blocking_start  # type: ignore[method-assign]
            app._send = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            app._on_codex_notification(
                "turn/completed",
                {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
            )
            self.assertTrue(started.wait(5))
            self.assertIn(session.session_id, app._turn_claims)
            sneak_started: list[str] = []
            original_start = app.codex.start_turn

            def tracking_start(_thread, text, *_args, **_kwargs):
                sneak_started.append(text)
                return "turn-3"

            app.codex.start_turn = tracking_start  # type: ignore[method-assign]
            app._send_to_session(1, session, "sneak")
            self.assertEqual(sneak_started, [])
            self.assertEqual(app._queues[session.session_id][0].text, "sneak")
            app.codex.start_turn = original_start  # type: ignore[method-assign]
            release.set()
            deadline = time.monotonic() + 5
            while app._codex_active_turns.get("thread-1") == "starting" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(turns, ["queued-next"])

    def test_busy_codex_queues_input_from_another_bot_with_its_reply_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project,
                bot_tokens=(("default", "primary:test"), ("worker", "worker:test")),
            )
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            app._codex_active_turns["thread-1"] = "turn-primary"
            app._register_turn(session, "turn-primary", "default")
            steered: list[str] = []
            app.codex.steer_turn = (  # type: ignore[method-assign]
                lambda *_args: steered.append("called") or "turn-primary"
            )
            sent_by_worker: list[str] = []
            app._telegrams["worker"].send_message = (  # type: ignore[method-assign]
                lambda _chat_id, text: sent_by_worker.append(text) or 1
            )

            with app._bot_scope("worker"):
                app._send_to_session(1, session, "next task")

            self.assertEqual(steered, [])
            queued = app._queues[session.session_id]
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].bot_key, "worker")
            self.assertEqual(
                sent_by_worker,
                [f"{session.label} is running; queued as #1."],
            )

            started_from: list[tuple[str, str]] = []
            app._start_session_turn = (  # type: ignore[method-assign]
                lambda _chat, _session, text, _attachments=(): started_from.append(
                    (app._active_bot_key(), text)
                )
            )
            app._start_next_queued(session)

            self.assertEqual(started_from, [("worker", "next task")])
            self.assertNotIn(session.session_id, app._queues)

    def busy_claude(self, app: GatewayApp, project: Path, bot_key: str = "default") -> tuple[Any, Any, list[str]]:
        run_background_inline(app)
        session = app.sessions.create_headless("claude", project, 1)
        app.headless.is_active = lambda _session_id: True  # type: ignore[method-assign]
        view = app._register_turn(session, "turn-1", bot_key)
        app._update_turn(session, "turn-1", "message_delta", {"id": "msg_1", "delta": "Looking at the cut."})
        sent: list[str] = []
        app._send = lambda _chat, text, *_args: sent.append(text)  # type: ignore[method-assign]
        return session, view, sent

    def test_busy_claude_adds_input_to_the_running_process_instead_of_queueing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session, view, sent = self.busy_claude(app, project)
            steered: list[tuple[str, str]] = []
            waited: list[float] = []

            def steer_turn(session_id: str, text: str, _attachments: Any = ()) -> Any:
                steered.append((session_id, text))
                return lambda timeout: waited.append(timeout) or True

            app.headless.steer_turn = steer_turn  # type: ignore[method-assign]
            app._send_to_session(1, session, "make the chorus louder")

            self.assertEqual(steered, [(session.session_id, "make the chorus louder")])
            self.assertEqual(waited, [HEADLESS_STEER_CONFIRM_SECONDS])
            self.assertNotIn(session.session_id, app._queues)
            self.assertEqual(sent, [])
            self.assertEqual(view.steered, 1)
            self.assertEqual(list(view.steer_notices), [(1, len("Looking at the cut."))])

    def test_claude_input_is_queued_once_the_process_takes_no_more_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session, view, sent = self.busy_claude(app, project)
            app.headless.steer_turn = lambda *_args: None  # type: ignore[method-assign]

            app._send_to_session(1, session, "next task")

            self.assertEqual([item.text for item in app._queues[session.session_id]], ["next task"])
            self.assertEqual(sent, [f"{session.label} is running; queued as #1."])
            self.assertEqual(view.steered, 0)

    def test_unconfirmed_claude_input_is_reported_and_never_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session, view, sent = self.busy_claude(app, project)
            app.headless.steer_turn = (  # type: ignore[method-assign]
                lambda *_args: lambda _timeout: False
            )

            app._send_to_session(1, session, "maybe twice")

            self.assertNotIn(session.session_id, app._queues)
            self.assertEqual(view.steered, 0)
            self.assertEqual(len(sent), 1)
            self.assertIn("was not confirmed", sent[0])

    def test_busy_claude_queues_input_from_another_bot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project,
                bot_tokens=(("default", "primary:test"), ("worker", "worker:test")),
            )
            session, _view, _sent = self.busy_claude(app, project, "default")
            app.headless.steer_turn = (  # type: ignore[method-assign]
                lambda *_args: self.fail("input from another Bot must not join this turn")
            )

            with app._bot_scope("worker"):
                app._send_to_session(1, session, "next task")

            queued = app._queues[session.session_id]
            self.assertEqual([(item.text, item.bot_key) for item in queued], [("next task", "worker")])

    def test_busy_headless_input_is_queued_and_drained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.headless.is_active = lambda _session_id: True  # type: ignore[method-assign]
            app._send = lambda *_args: None  # type: ignore[method-assign]
            app._send_to_session(1, session, "next")
            self.assertEqual(len(app._queues[session.session_id]), 1)
            started: list[str] = []
            app._start_session_turn = (  # type: ignore[method-assign]
                lambda _chat, _session, text, _attachments=(): started.append(text)
            )
            app._start_next_queued(session)
            self.assertEqual(started, ["next"])

    def test_queue_is_bounded_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            for index in range(MAX_PENDING_INPUTS_PER_SESSION):
                position = app._enqueue(session, PendingInput(1, f"message-{index}"))
                self.assertEqual(position, index + 1)
            self.assertIsNone(app._enqueue(session, PendingInput(1, "overflow")))
            self.assertEqual(len(app._queues[session.session_id]), MAX_PENDING_INPUTS_PER_SESSION)

    def test_tasks_offer_queue_controls_and_stop_clears_before_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.headless.is_active = lambda _session_id: True  # type: ignore[method-assign]
            app._enqueue(session, PendingInput(1, "one"))
            app._enqueue(session, PendingInput(1, "two"))

            text, markup = app._tasks_view(1)
            callbacks = {
                button["callback_data"]
                for row in markup["inline_keyboard"]
                for button in row
            }
            self.assertIn("🟡 Running · 2 queued", text)
            self.assertIn(f"taskinterrupt:{session.session_id}", callbacks)
            self.assertIn(f"clearqueue:{session.session_id}", callbacks)

            interrupted: list[str] = []

            def fake_interrupt(target: object) -> bool:
                self.assertNotIn(session.session_id, app._queues)
                interrupted.append(session.session_id)
                return True

            app._interrupt_session = fake_interrupt  # type: ignore[method-assign]
            answered: list[str] = []
            edited: list[str] = []
            app.telegram.answer_callback_query = (  # type: ignore[method-assign]
                lambda _query_id, notice="": answered.append(notice)
            )
            app.telegram.edit_message = (  # type: ignore[method-assign]
                lambda _chat_id, _message_id, updated, reply_markup=None: edited.append(updated)
            )
            app.handle_update({
                "callback_query": {
                    "id": "stop-queue",
                    "from": {"id": 1},
                    "data": f"stopqueue:{session.session_id}",
                    "message": {
                        "message_id": 50,
                        "chat": {"id": 1, "type": "private"},
                    },
                }
            })
            self.assertEqual(interrupted, [session.session_id])
            self.assertEqual(answered, ["Interrupted and cleared 2 queued messages"])
            self.assertNotIn(session.session_id, app._queues)
            self.assertTrue(edited)

    def test_sessions_view_shows_agent_state_badges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)

            app._set_session_state(session.session_id, STATE_WORKING)
            text, _markup = app._sessions_view(1)
            self.assertIn("🟡 Running", text)

            app._set_session_state(session.session_id, STATE_IDLE)
            text, _markup = app._sessions_view(1)
            self.assertIn("🟢 Idle", text)

            app._set_session_state(session.session_id, STATE_FAILED)
            text, _markup = app._sessions_view(1)
            self.assertIn("⚫ Last run failed", text)

    def test_update_turn_transitions_working_to_idle_or_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)

            # Normal completion: working → idle
            app._set_session_state(session.session_id, STATE_WORKING)
            view = app._register_turn(session, "turn-done")
            app._update_turn(session, "turn-done", "completed")
            self.assertEqual(app._session_states[session.session_id], STATE_IDLE)

            # Error: working → failed
            app._set_session_state(session.session_id, STATE_WORKING)
            view = app._register_turn(session, "turn-fail")
            app._update_turn(session, "turn-fail", "error", "boom")
            self.assertEqual(app._session_states[session.session_id], STATE_FAILED)
            self.assertEqual(view.status, "failed")
            self.assertIn("boom", view.error)

    def test_completed_empty_answer_is_not_retried_or_exposes_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)

            view = app._register_turn(session, "turn-empty")
            app._update_turn(session, "turn-empty", "thinking", "private reasoning")
            app._update_turn(session, "turn-empty", "command", "write target.txt")
            app._update_turn(session, "turn-empty", "completed")
            rendered, answer = app._render_turn(view, view.started_at + 2)

            self.assertEqual(view.status, "completed")
            self.assertEqual(answer, "")
            self.assertIn("without a visible answer", rendered)
            self.assertIn("did not retry", rendered)
            self.assertNotIn("private reasoning", rendered)
            self.assertNotIn("retrying", rendered)

    def test_interrupted_session_does_not_show_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app._set_session_state(session.session_id, STATE_WORKING)
            app._interrupted_sessions.add(session.session_id)
            app._register_turn(session, "turn-killed")
            # After an interrupt the reader reports error with a nonzero exit code; the state must not become failed
            app._update_turn(session, "turn-killed", "error", "exited with status -15")
            self.assertEqual(app._session_states[session.session_id], STATE_IDLE)
            self.assertNotIn(session.session_id, app._interrupted_sessions)

    def test_queued_count_appears_in_status_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app._enqueue(session, PendingInput(1, "one"))
            app._enqueue(session, PendingInput(1, "two"))
            self.assertIn("2 queued", app._session_status_text(session))

    def test_publish_retry_caps_local_backoff_but_honors_retry_after(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-retry")

            self.assertEqual(app._schedule_publish_retry(view, TelegramError("offline"), 10), 1)
            self.assertEqual(view.next_publish_at, 11)
            self.assertEqual(app._schedule_publish_retry(view, TelegramError("offline"), 20), 2)
            self.assertEqual(view.next_publish_at, 22)
            view.publish_failures = 10
            self.assertEqual(app._schedule_publish_retry(view, TelegramError("offline"), 25), 30)
            self.assertEqual(view.next_publish_at, 55)
            self.assertEqual(
                app._schedule_publish_retry(
                    view, TelegramError("limited", retry_after=60), 30
                ),
                60,
            )
            self.assertEqual(view.next_publish_at, 90)

    def test_result_paths_create_artifact_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            artifact = project / "report.csv"
            artifact.write_text("a,b\n1,2\n", encoding="utf-8")
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-artifact")
            paths = app._find_artifacts(session, f"Saved to `{artifact}`")
            self.assertEqual(paths, [artifact])
            view.artifacts = paths
            markup = app._artifact_markup(view)
            self.assertIn("Send file: report.csv", markup["inline_keyboard"][0][0]["text"])  # type: ignore[index]

    def test_mp4_button_sends_video_even_from_an_existing_file_button(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            video = project / "reunion.MP4"
            video.write_bytes(b"video bytes")
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-video")
            view.artifacts = [video]
            button = app._artifact_markup(view)["inline_keyboard"][0][0]  # type: ignore[index]
            self.assertEqual(button["text"], "Send video: reunion.MP4")
            self.assertTrue(button["callback_data"].startswith("video:"))

            sent: list[tuple[Path, bool]] = []
            app.telegram.answer_callback_query = lambda *_args: None  # type: ignore[method-assign]
            app.telegram.send_local_file = (  # type: ignore[method-assign]
                lambda _chat_id, path, as_video=False: sent.append((path, as_video))
            )
            for action in ("video", "file"):
                app.handle_update({"callback_query": {
                    "id": f"send-{action}",
                    "from": {"id": 1},
                    "data": f"{action}:{button['callback_data'].split(':', 1)[1]}",
                    "message": {"message_id": 9, "chat": {"id": 1, "type": "private"}},
                }})
            self.assertEqual(sent, [(video, True), (video, True)])

    def make_artifact_app(self, project: Path, mode: str) -> tuple[GatewayApp, list[tuple[Path, bool]]]:
        config = Config(
            project_dir=project,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(project,),
            default_workdir=project,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
            auto_send_artifacts=mode,
        )
        app = GatewayApp(config)
        sent: list[tuple[Path, bool]] = []
        app.telegram.send_local_file = (  # type: ignore[method-assign]
            lambda _chat_id, path, as_photo=False: sent.append((path, as_photo))
        )
        return app, sent

    def test_auto_send_images_mode_sends_only_small_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            image = project / "chart.png"
            image.write_bytes(b"\x89PNG fake")
            large = project / "huge.png"
            large.write_bytes(b"x" * (11 * 1024 * 1024))
            report = project / "report.csv"
            report.write_text("a,b\n", encoding="utf-8")
            app, sent = self.make_artifact_app(project, "images")
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-auto")
            view.artifacts = [image, large, report]
            app._auto_send_artifacts(view)
            self.assertEqual(sent, [(image, True)])
            self.assertEqual(view.auto_sent, [str(image)])

    def test_auto_send_is_idempotent_across_publish_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            image = project / "chart.png"
            image.write_bytes(b"\x89PNG fake")
            app, sent = self.make_artifact_app(project, "images")
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-auto")
            view.artifacts = [image]
            app._auto_send_artifacts(view)
            app._auto_send_artifacts(view)
            self.assertEqual(sent, [(image, True)])

    def test_auto_send_all_mode_sends_files_as_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            image = project / "chart.png"
            image.write_bytes(b"\x89PNG fake")
            report = project / "report.csv"
            report.write_text("a,b\n", encoding="utf-8")
            app, sent = self.make_artifact_app(project, "all")
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-auto")
            view.artifacts = [image, report]
            app._auto_send_artifacts(view)
            self.assertEqual(sent, [(image, True), (report, False)])

    def test_auto_send_all_mode_sends_mp4_as_video(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            video = project / "clip.mp4"
            video.write_bytes(b"video bytes")
            app, _sent = self.make_artifact_app(project, "all")
            sent: list[tuple[Path, bool]] = []
            app.telegram.send_local_file = (  # type: ignore[method-assign]
                lambda _chat_id, path, as_video=False: sent.append((path, as_video))
            )
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-video-auto")
            view.artifacts = [video]
            app._auto_send_artifacts(view)
            self.assertEqual(sent, [(video, True)])
            self.assertEqual(view.auto_sent, [str(video)])

    def test_auto_send_off_keeps_buttons_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            image = project / "chart.png"
            image.write_bytes(b"\x89PNG fake")
            app, sent = self.make_artifact_app(project, "off")
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-auto")
            view.artifacts = [image]
            app._auto_send_artifacts(view)
            self.assertEqual(sent, [])

    def test_auto_send_failure_keeps_artifact_for_manual_button(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            image = project / "chart.png"
            image.write_bytes(b"\x89PNG fake")
            app, _sent = self.make_artifact_app(project, "images")

            def failing_send(_chat_id: int, _path: Path, as_photo: bool = False) -> None:
                raise TelegramError("file too big")

            app.telegram.send_local_file = failing_send  # type: ignore[method-assign]
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-auto")
            view.artifacts = [image]
            app._auto_send_artifacts(view)  # must not raise
            self.assertEqual(view.auto_sent, [])

    def test_model_command_sets_model_on_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/model gpt-5.5",
            }})
            self.assertEqual(app.sessions.get(session.session_id).model, "gpt-5.5")  # type: ignore[union-attr]
            self.assertIn("applies from the next task", sent[0])

    def test_model_command_without_arg_shows_picker_buttons(self) -> None:
        import cli_telegram_gateway.app as app_module

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            original = app_module.list_models
            app_module.list_models = lambda _cli, _command: ["m1", "m2"]  # type: ignore[assignment]
            try:
                sent: list[tuple[str, dict[str, Any]]] = []
                app.telegram.send_message = (  # type: ignore[method-assign]
                    lambda chat_id, text, reply_markup=None: sent.append((text, reply_markup))
                )
                app.handle_update({"message": {
                    "from": {"id": 1},
                    "chat": {"id": 1, "type": "private"},
                    "text": "/model",
                }})
                self.assertEqual(len(sent), 1)
                callbacks = {
                    button["callback_data"]
                    for row in sent[0][1]["inline_keyboard"]
                    for button in row
                }
                self.assertIn(f"model:{session.session_id}:{app._model_choice_id('m1')}", callbacks)
                self.assertIn(f"model:{session.session_id}:{app._model_choice_id('m2')}", callbacks)
                self.assertIn(f"modelclear:{session.session_id}", callbacks)
                self.assertIn("current model: CLI default", sent[0][0])
            finally:
                app_module.list_models = original

    def test_model_callback_selects_listed_model_and_refreshes(self) -> None:
        import cli_telegram_gateway.app as app_module

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            original = app_module.list_models
            app_module.list_models = lambda _cli, _command: ["m1", "m2"]  # type: ignore[assignment]
            try:
                answered: list[str] = []
                edited: list[str] = []
                app.telegram.answer_callback_query = (  # type: ignore[method-assign]
                    lambda _query_id, notice="": answered.append(notice)
                )
                app.telegram.edit_message = (  # type: ignore[method-assign]
                    lambda _chat_id, _message_id, text, reply_markup=None: edited.append(text)
                )
                app.handle_update({
                    "callback_query": {
                        "id": "q",
                        "from": {"id": 1},
                        "data": app._model_view(session)[1]["inline_keyboard"][1][0]["callback_data"],
                        "message": {"message_id": 9, "chat": {"id": 1, "type": "private"}},
                    }
                })
                self.assertEqual(app.sessions.get(session.session_id).model, "m2")  # type: ignore[union-attr]
                self.assertEqual(answered, ["Model set to m2"])
                self.assertIn("current model: m2", edited[0])
            finally:
                app_module.list_models = original

    def test_modelclear_callback_resets_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app.sessions.set_model(session.session_id, "m1")
            answered: list[str] = []
            app.telegram.answer_callback_query = (  # type: ignore[method-assign]
                lambda _query_id, notice="": answered.append(notice)
            )
            app.telegram.edit_message = (  # type: ignore[method-assign]
                lambda *_args, **_kwargs: None
            )
            app.handle_update({
                "callback_query": {
                    "id": "q",
                    "from": {"id": 1},
                    "data": f"modelclear:{session.session_id}",
                    "message": {"message_id": 9, "chat": {"id": 1, "type": "private"}},
                }
            })
            self.assertIsNone(app.sessions.get(session.session_id).model)  # type: ignore[union-attr]
            self.assertEqual(answered, ["Restored the default model"])

    def test_effort_command_sets_effort_on_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("claude", project, 1)
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/effort high",
            }})
            self.assertEqual(app.sessions.get(session.session_id).effort, "high")  # type: ignore[union-attr]
            self.assertIn("reasoning effort set to high", sent[0])

    def test_start_session_turn_passes_model_and_effort_to_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            app.sessions.set_model(session.session_id, "gpt-5.5")
            app.sessions.set_effort(session.session_id, "low")
            captured: dict[str, object] = {}

            def fake_start_turn(
                thread_id: str,
                text: str,
                attachments: tuple[Any, ...] = (),
                model: str | None = None,
                effort: str | None = None,
            ) -> str:
                captured["thread_id"] = thread_id
                captured["model"] = model
                captured["effort"] = effort
                return "turn-1"

            app.codex.start_turn = fake_start_turn  # type: ignore[method-assign]
            app._send = lambda *_args: None  # type: ignore[method-assign]
            app._start_session_turn(1, app.sessions.get(session.session_id), "go")  # type: ignore[arg-type]
            self.assertEqual(captured["model"], "gpt-5.5")
            self.assertEqual(captured["effort"], "low")
            view = app._turns[(session.session_id, "turn-1")]
            app.sessions.set_model(session.session_id, "changed-later")
            app.sessions.set_effort(session.session_id, "max")
            app._update_turn(session, "turn-1", "delta", "answer text")
            app._update_turn(session, "turn-1", "thinking", "private reasoning")
            app._update_turn(session, "turn-1", "completed")
            rendered, answer = app._render_turn(view, view.started_at + 3)
            self.assertTrue(rendered.startswith("model: gpt-5.5 · effort: low\n"))
            self.assertIn("Done 3s", rendered)
            self.assertEqual(answer, "answer text")
            self.assertNotIn("changed-later", rendered)
            self.assertNotIn("private reasoning", rendered)

    def test_turn_header_shows_gateway_defaults_and_cli_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project,
                cli_default_models={"pi": "pi-default", "codex": "gpt-6-astra"},
                cli_default_efforts={"codex": "high"},
            )
            pi_session = app.sessions.create_headless("pi", project, 1)
            app.headless.start_turn = lambda *_args, **_kwargs: "turn-pi"  # type: ignore[method-assign]
            app._send = lambda *_args: None  # type: ignore[method-assign]
            app._start_session_turn(1, pi_session, "go")
            pi_view = app._turns[(pi_session.session_id, "turn-pi")]
            pi_rendered, _answer = app._render_turn(pi_view, pi_view.started_at + 1)
            self.assertTrue(
                pi_rendered.startswith("model: pi-default (gateway default) · effort: CLI default\n")
            )

            codex_session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-defaults"
            )
            app.codex.start_turn = lambda *_args, **_kwargs: "turn-codex"  # type: ignore[method-assign]
            app._start_session_turn(1, codex_session, "go")
            codex_view = app._turns[(codex_session.session_id, "turn-codex")]
            codex_rendered, _answer = app._render_turn(codex_view, codex_view.started_at + 1)
            self.assertTrue(
                codex_rendered.startswith(
                    "model: gpt-6-astra (gateway default) · effort: high (gateway default)\n"
                )
            )

    def test_codex_session_inherits_gateway_model_and_effort_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project,
                cli_default_models={"codex": "gpt-6-astra"},
                cli_default_efforts={"codex": "high"},
            )
            session = app.sessions.create_virtual(
                "codex", project, 1, "codex-app-server", "thread-1"
            )
            captured: dict[str, object] = {}

            def fake_start_turn(
                thread_id: str,
                text: str,
                attachments: tuple[Any, ...] = (),
                model: str | None = None,
                effort: str | None = None,
            ) -> str:
                captured["model"] = model
                captured["effort"] = effort
                return "turn-defaults"

            app.codex.start_turn = fake_start_turn  # type: ignore[method-assign]
            app._send = lambda *_args: None  # type: ignore[method-assign]
            app._start_session_turn(1, session, "go")

            self.assertEqual(captured, {"model": "gpt-6-astra", "effort": "high"})
            self.assertIsNone(app.sessions.get(session.session_id).model)  # type: ignore[union-attr]
            self.assertIn("gpt-6-astra (gateway default)", app._model_view(session)[0])
            self.assertIn("high (gateway default)", app._effort_view(session)[0])

    def test_headless_session_inherits_gateway_defaults_without_persisting_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(
                project,
                cli_default_models={"pi": "pi-default"},
                cli_default_efforts={"pi": "high"},
            )
            session = app.sessions.create_headless("pi", project, 1)
            captured: dict[str, object] = {}

            def fake_start_turn(
                effective_session: Any, _text: str, _attachments: tuple[Any, ...] = ()
            ) -> str:
                captured["model"] = effective_session.model
                captured["effort"] = effective_session.effort
                return "turn-defaults"

            app.headless.start_turn = fake_start_turn  # type: ignore[method-assign]
            app._send = lambda *_args: None  # type: ignore[method-assign]
            app._start_session_turn(1, session, "go")

            self.assertEqual(captured, {"model": "pi-default", "effort": "high"})
            persisted = app.sessions.get(session.session_id)
            self.assertIsNone(persisted.model)  # type: ignore[union-attr]
            self.assertIsNone(persisted.effort)  # type: ignore[union-attr]



if __name__ == "__main__":
    unittest.main()
