from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from cli_telegram_gateway.app import (
    BOT_COMMANDS,
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
from cli_telegram_gateway.telegram import TelegramError


class FakeCodex:
    def __init__(self) -> None:
        self.responses: list[tuple[str | int, dict[str, Any]]] = []

    def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))


class GatewayEventTests(unittest.TestCase):
    def make_app(
        self,
        project: Path,
        auto_resume: bool = False,
        enabled_clis: tuple[str, ...] = ("claude", "codex", "grok", "pi"),
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
            output_poll_interval=0.1,
            output_max_bytes=65536,
            tmux_socket_name="tcg-app-test",
            enabled_clis=enabled_clis,
            auto_resume=auto_resume,
        )
        return GatewayApp(config)

    def test_local_file_commands_are_not_exposed_or_handled(self) -> None:
        command_names = {name for name, _description in BOT_COMMANDS}
        self.assertNotIn("file", command_names)
        self.assertNotIn("photo", command_names)
        self.assertNotIn("/file", HELP_TEXT)
        self.assertNotIn("/photo", HELP_TEXT)

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

            self.assertEqual(sent, ["未知命令。使用 /help 查看可用命令。"] * 2)

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
            self.assertIn(f"默认目录：{project}", sent[0][0])
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
            self.assertEqual(answered, [("new-pi", "正在创建 pi")])
            self.assertEqual(edited, [(1, 42, "已选择 pi，正在创建…")])

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
            self.assertEqual(answered, ["CLI 已失效，请重新 /new"])

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
            self.assertEqual(answered, ["没有权限"])

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
            self.assertIn("完成 2秒", rendered)
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
                output_poll_interval=0.1,
                output_max_bytes=65536,
                tmux_socket_name="tcg-app-test",
                enabled_clis=("codex",),
            )
            sent: list[str] = []
            app._send = lambda _chat_id, text: sent.append(text)  # type: ignore[method-assign]
            app._new_session(1, "claude", None)
            self.assertIn("未启用 claude", sent[-1])
            app._handle_command(1, "use", disabled.session_id)
            self.assertEqual(app.sessions.current(1).session_id, enabled.session_id)  # type: ignore[union-attr]
            app._send_to_session(1, disabled, "do not run")
            self.assertIn("不能继续该会话", sent[-1])

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
                        "caption": "看看这张图",
                        "photo": [{"file_id": "small", "file_size": 1}, {"file_id": "large", "file_size": 9}],
                    }
                }
            )
            self.assertEqual(downloaded[0][0], "large")
            self.assertEqual(forwarded[0][1], "看看这张图")
            self.assertTrue(forwarded[0][2][0].is_image)
            self.assertEqual(app.sessions.current(1).session_id, session.session_id)  # type: ignore[union-attr]

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
                        "caption": "看这些图",
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
            self.assertEqual(message["caption"], "看这些图")
            self.assertEqual(
                [item["photo"][0]["file_id"] if "photo" in item else item["document"]["file_id"]
                 for item in message["media"]],
                ["a", "b", "c"],
            )
            self.assertNotIn("photo", message)
            self.assertNotIn("document", message)
            self.assertEqual(coalesced[1], updates[3])

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
                        "caption": "一起看",
                        "media": [
                            {"photo": [{"file_id": "p1", "file_size": 2}]},
                            {"document": {"file_id": "d1", "file_name": "a.pdf"}},
                        ],
                    }
                }
            )
            self.assertEqual(downloaded, ["p1", "d1"])
            self.assertEqual(len(forwarded), 1)
            self.assertEqual(forwarded[0][1], "一起看")
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
            self.assertEqual(forwarded[0][1], "请查看并处理这些附件。")

    def test_running_turn_render_includes_command_and_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-1")
            app._update_turn(session, "turn-1", "command", "ls -la")
            app._update_turn(session, "turn-1", "command_output", "file.txt")
            rendered, _answer = app._render_turn(view, view.started_at + 7)
            self.assertIn("运行中 7秒", rendered)
            self.assertIn("当前命令：\n```text\nls -la\n```", rendered)
            self.assertIn("file.txt", rendered)

    def test_status_loop_does_not_edit_without_new_cli_events(self) -> None:
        """CLI 无新输出时保留现有预览，避免只为刷新计时持续调用 Telegram。"""
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
            self.assertIn("运行中", published[0])

    def test_completion_during_running_publish_still_publishes_final_status(self) -> None:
        """运行态编辑期间到达 completed 时，下一轮必须补发完成态，不能提前移除。"""
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
                lambda _view, rendered, with_artifacts=True: final_updates.append(rendered)
            )

            class StopAfterTwoIterations:
                calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 2

            app.stop_event = StopAfterTwoIterations()  # type: ignore[assignment]
            app._status_loop()

            self.assertEqual(len(running_updates), 1)
            self.assertIn("运行中", running_updates[0])
            self.assertEqual(len(final_updates), 1)
            self.assertIn("完成", final_updates[0])
            self.assertNotIn((session.session_id, view.turn_id), app._turns)
            self.assertEqual(app.sessions.stale_in_flight(), [])
            saved = app.sessions.get(session.session_id)
            self.assertEqual(saved.last_completed_message_id, 88)  # type: ignore[union-attr]

    def test_switching_away_backgrounds_running_turn(self) -> None:
        """切走后，运行中的 turn 定格为“后台运行中”，不再周期直播。"""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            first = app.sessions.create_headless("pi", project, 1)
            app.sessions.create_headless("pi", project, 1)  # 第二个成为当前，切走第一个
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
            self.assertIn("后台运行中", edited[0][1])
            self.assertEqual(published, [])
            self.assertFalse(view.live)

    def test_switching_back_pins_running_turn_to_fresh_card(self) -> None:
        """切回运行中的 session 时，把进度重新钉到一条新卡片，旧卡片记入 stale。"""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn-a")
            view.published = True
            view.live = False  # 之前被冻结
            view.message_id = 88  # 旧卡片
            view.dirty = True
            view.last_edit = -10.0

            sent: list[str] = []
            app.telegram.send_rich_markdown = (  # type: ignore[method-assign]
                lambda chat_id, markdown, reply_markup=None: sent.append(markdown) or 99
            )
            edited: list[tuple[int, str]] = []
            app.telegram.edit_rich_markdown = (  # type: ignore[method-assign]
                lambda chat_id, message_id, markdown, reply_markup=None: edited.append(
                    (message_id, markdown)
                )
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
            self.assertIn("后台运行中", edited[0][1])

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
            app.telegram.send_rich_markdown = (  # type: ignore[method-assign]
                lambda chat_id, markdown, reply_markup=None: calls.append(
                    ("send", (chat_id, markdown, reply_markup))
                ) or 88
            )
            app.telegram.edit_rich_markdown = (  # type: ignore[method-assign]
                lambda chat_id, message_id, markdown, reply_markup=None: calls.append(
                    ("edit", (chat_id, message_id, markdown, reply_markup))
                )
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
            app.telegram.send_rich_markdown = (  # type: ignore[method-assign]
                lambda *_args: (_ for _ in ()).throw(TelegramError("unsupported"))
            )
            sent: list[str] = []
            app.telegram.send_markdown = (  # type: ignore[method-assign]
                lambda _chat_id, markdown: sent.append(markdown) or 9
            )
            app._publish_running_turn(view, "**partial**")
            self.assertFalse(view.rich_mode)
            self.assertEqual(view.message_id, 9)
            self.assertEqual(sent, ["**partial**"])

            edited: list[tuple[int, int, str]] = []
            app.telegram.edit_rich_markdown = (  # type: ignore[method-assign]
                lambda chat_id, message_id, markdown, reply_markup=None: edited.append(
                    (chat_id, message_id, markdown)
                )
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
            app.telegram.send_rich_markdown = (  # type: ignore[method-assign]
                lambda *_args: (_ for _ in ()).throw(
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
        """意外中断的任务：重启后提示 /resume、/cancel，但保留 in_flight 供用户决定。"""
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
            self.assertIn("已自动恢复", sent[0])

    def test_unexpected_kill_marks_resumable_and_keeps_in_flight(self) -> None:
        """进程意外被杀（非用户主动）：标记可恢复、状态待恢复，in_flight 保留。"""
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
            self.assertIn("已中断 2秒", rendered)
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
            # 读线程随后上报 interrupted：用户主动，静默收尾，不进入恢复候选
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
            self.assertIn("正在恢复", sent[0])

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
            self.assertIn("没有待恢复的任务", sent[0])

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
            self.assertIn("已放弃", sent[0])

    def test_compact_command_uses_pi_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            compacted: list[str] = []
            app.headless.compact_session = (  # type: ignore[method-assign]
                lambda _session: compacted.append(_session.session_id) or "已触发上下文压缩。"
            )
            sent: list[str] = []
            app._send = lambda _chat, text: sent.append(text)  # type: ignore[method-assign]
            app.handle_update({"message": {
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                "text": "/compact",
            }})
            self.assertEqual(compacted, [session.session_id])
            self.assertIn("已触发上下文压缩", sent[0])

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
            self.assertIn("请先 /interrupt", sent[0])

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
            self.assertIn("已触发", sent[0])

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
            self.assertIn("已开新对话", sent[0])

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
            self.assertIn("旧线程已归档", sent[0])

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
            self.assertIn("请先 /interrupt", sent[0])
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
            self.assertEqual(answered, [("query-1", f"已切换到 {first.session_id}")])
            self.assertIn(f"▶ {first.session_id}", edited[0][2])
            self.assertTrue(edited[0][3]["inline_keyboard"])
            self.assertEqual(sent, [f"[{first.session_id}] · 有什么可以帮你？"])

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
            app._register_turn(session, "turn-1")
            steered: list[tuple[str, str, str]] = []
            app.codex.steer_turn = (  # type: ignore[method-assign]
                lambda thread, turn, text, _attachments=(): steered.append(
                    (thread, turn, text)
                ) or turn
            )
            app._send = lambda *_args: None  # type: ignore[method-assign]
            app._send_to_session(1, session, "change direction")
            self.assertEqual(steered, [("thread-1", "turn-1", "change direction")])
            self.assertNotIn(session.session_id, app._queues)

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
            self.assertIn("🟡 运行中 · 排队 2", text)
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
            self.assertEqual(answered, ["已中断并清空 2 条等待消息"])
            self.assertNotIn(session.session_id, app._queues)
            self.assertTrue(edited)

    def test_sessions_view_shows_agent_state_badges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)

            app._set_session_state(session.session_id, STATE_WORKING)
            text, _markup = app._sessions_view(1)
            self.assertIn("🟡 运行中", text)

            app._set_session_state(session.session_id, STATE_IDLE)
            text, _markup = app._sessions_view(1)
            self.assertIn("🟢 空闲", text)

            app._set_session_state(session.session_id, STATE_FAILED)
            text, _markup = app._sessions_view(1)
            self.assertIn("⚫ 上次失败", text)

    def test_update_turn_transitions_working_to_idle_or_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)

            # 正常完成：working → idle
            app._set_session_state(session.session_id, STATE_WORKING)
            view = app._register_turn(session, "turn-done")
            app._update_turn(session, "turn-done", "completed")
            self.assertEqual(app._session_states[session.session_id], STATE_IDLE)

            # 出错：working → failed
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
            self.assertIn("没有返回可见回答", rendered)
            self.assertIn("未自动重试", rendered)
            self.assertNotIn("private reasoning", rendered)
            self.assertNotIn("正在自动重试", rendered)

    def test_interrupted_session_does_not_show_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            app._set_session_state(session.session_id, STATE_WORKING)
            app._interrupted_sessions.add(session.session_id)
            app._register_turn(session, "turn-killed")
            # 中断后读线程会以非零退出码上报 error，不应把状态置为 failed
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
            self.assertIn("排队 2", app._session_status_text(session))

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
            self.assertIn("发送文件：report.csv", markup["inline_keyboard"][0][0]["text"])  # type: ignore[index]

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
            output_poll_interval=0.1,
            output_max_bytes=65536,
            tmux_socket_name="tcg-app-test",
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
            app._auto_send_artifacts(view)  # 不应抛出
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
            self.assertIn("下次任务生效", sent[0])

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
                self.assertIn(f"model:{session.session_id}:0", callbacks)
                self.assertIn(f"model:{session.session_id}:1", callbacks)
                self.assertIn(f"modelclear:{session.session_id}", callbacks)
                self.assertIn("当前模型：默认", sent[0][0])
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
                        "data": f"model:{session.session_id}:1",
                        "message": {"message_id": 9, "chat": {"id": 1, "type": "private"}},
                    }
                })
                self.assertEqual(app.sessions.get(session.session_id).model, "m2")  # type: ignore[union-attr]
                self.assertEqual(answered, ["模型已设为 m2"])
                self.assertIn("当前模型：m2", edited[0])
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
            self.assertEqual(answered, ["已恢复默认模型"])

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
            self.assertIn("推理力度已设为 high", sent[0])

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



if __name__ == "__main__":
    unittest.main()
