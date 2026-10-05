from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cli_telegram_gateway.app import GatewayApp
from cli_telegram_gateway.commands import DirectCommand, DirectCommandRun
from cli_telegram_gateway.config import Config
from cli_telegram_gateway.headless_backend import HeadlessBackendError
from cli_telegram_gateway.telegram import TelegramError

WAIT = 5.0
PRIVATE = {"id": 1, "type": "private"}


class OffDispatchTests(unittest.TestCase):
    """Slow actions must not hold the update dispatch shared by every Bot entrance."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.app = GatewayApp(Config(
            project_dir=self.project,
            bot_token="a:x",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(self.project,),
            default_workdir=self.project,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
            bot_tokens=(("default", "a:x"), ("worker", "b:y")),
            auto_send_artifacts="images",
        ))
        self.replies: list[tuple[str, str]] = []
        self.network: list[str] = []
        for bot_key, telegram in self.app._telegrams.items():
            telegram._call = self.no_network  # type: ignore[method-assign]
            telegram.send_action = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            telegram.send_message = self.recorder(bot_key)  # type: ignore[method-assign]
            telegram.answer_callback_query = self.answer_recorder(bot_key)  # type: ignore[method-assign]
        self.addCleanup(lambda: self.assertEqual(self.network, []))

    def no_network(self, method: str, *_args: Any, **_kwargs: Any) -> Any:
        self.network.append(method)
        raise TelegramError(f"{method}: network disabled in tests")

    def recorder(self, bot_key: str):
        def send_message(_chat_id: int, text: str, reply_markup: Any = None) -> int:
            self.replies.append((bot_key, text))
            return 1
        return send_message

    def answer_recorder(self, bot_key: str):
        def answer(_query_id: str, text: str = "") -> None:
            self.replies.append((bot_key, f"answer:{text}"))
        return answer

    def text_update(self, text: str) -> dict[str, Any]:
        return {"message": {"message_id": 9, "from": {"id": 1}, "chat": PRIVATE, "text": text}}

    def tap(self, bot_key: str, query_id: str, data: str) -> None:
        self.app._dispatch_update(bot_key, {"callback_query": {
            "id": query_id, "from": {"id": 1}, "data": data,
            "message": {"message_id": 5, "chat": PRIVATE},
        }})

    def wait_until(self, predicate) -> None:
        deadline = time.monotonic() + WAIT
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("condition not reached in time")
            time.sleep(0.01)

    def test_upload_on_one_bot_does_not_hold_up_another_bot(self) -> None:
        session = self.app.sessions.create_headless("pi", self.project, 1, "worker")
        video = self.project / "clip.mp4"
        video.write_bytes(b"0" * 64)
        token = self.app.sessions.register_artifact(1, session.session_id, video, "default")
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def slow_upload(_chat_id: int, _path: Path, **_kwargs: Any) -> None:
            started.set()
            release.wait(WAIT)
            finished.set()

        self.app._telegrams["default"].send_local_file = slow_upload  # type: ignore[method-assign]
        self.tap("default", "q1", f"video:{token}")
        self.assertTrue(started.wait(WAIT))
        self.assertIn(("default", "answer:正在发送"), self.replies)

        self.app._dispatch_update("worker", self.text_update("/where"))
        self.assertTrue(any(bot == "worker" and "当前" in text for bot, text in self.replies))
        self.assertFalse(finished.is_set())
        release.set()
        self.assertTrue(finished.wait(WAIT))
        self.wait_until(lambda: not self.app._artifact_uploads)

    def test_repeated_tap_during_upload_sends_the_file_once(self) -> None:
        session = self.app.sessions.create_headless("pi", self.project, 1)
        image = self.project / "chart.png"
        image.write_bytes(b"\x89PNG fake")
        token = self.app.sessions.register_artifact(1, session.session_id, image, "default")
        uploads: list[Path] = []
        release = threading.Event()

        def slow_upload(_chat_id: int, path: Path, **_kwargs: Any) -> None:
            uploads.append(path)
            release.wait(WAIT)

        self.app._telegrams["default"].send_local_file = slow_upload  # type: ignore[method-assign]
        self.tap("default", "q1", f"photo:{token}")
        self.wait_until(lambda: len(uploads) == 1)
        self.tap("default", "q2", f"photo:{token}")
        self.assertIn(("default", "answer:正在发送，请稍候"), self.replies)
        release.set()
        self.wait_until(lambda: not self.app._artifact_uploads)
        self.assertEqual(len(uploads), 1)

        self.tap("default", "q3", f"photo:{token}")
        self.wait_until(lambda: len(uploads) == 2 and not self.app._artifact_uploads)

    def test_pi_compaction_runs_in_background_and_starts_queued_input_afterwards(self) -> None:
        session = self.app.sessions.create_headless("pi", self.project, 1)
        compaction_started, release = threading.Event(), threading.Event()
        turns: list[str] = []
        turn_started = threading.Event()

        def slow_compaction(_session) -> str:
            compaction_started.set()
            release.wait(WAIT)
            return "压缩完成。"

        def start_turn(_session, text: str, _attachments=()) -> str:
            turns.append(text)
            turn_started.set()
            return "turn-after-compact"

        self.app.headless.compact_session = slow_compaction  # type: ignore[method-assign]
        self.app.headless.start_turn = start_turn  # type: ignore[method-assign]
        self.app._dispatch_update("default", self.text_update("/compact"))
        self.assertTrue(compaction_started.wait(WAIT))
        self.assertTrue(any("正在压缩" in text for _bot, text in self.replies))

        self.app._dispatch_update("default", self.text_update("continue please"))
        self.assertTrue(any("已加入队列" in text for _bot, text in self.replies))
        self.assertEqual(turns, [])

        release.set()
        self.assertTrue(turn_started.wait(WAIT))
        self.assertEqual(turns, ["continue please"])
        self.assertTrue(any("压缩完成" in text for _bot, text in self.replies))
        self.wait_until(lambda: self.app.sessions.get_in_flight(session.session_id) == "turn-after-compact")

    def test_interrupted_pi_compaction_is_reported_as_interrupted(self) -> None:
        session = self.app.sessions.create_headless("pi", self.project, 1)

        def interrupted_compaction(compacting) -> str:
            self.app._interrupted_sessions.add(compacting.session_id)  # what /interrupt records
            raise HeadlessBackendError("pi RPC 未返回 compact 结果（exit -15）")

        self.app.headless.compact_session = interrupted_compaction  # type: ignore[method-assign]
        self.app._run_in_background = (  # type: ignore[method-assign]
            lambda _name, target, *args: target(*args)
        )
        self.app._dispatch_update("default", self.text_update("/compact"))
        self.assertIn(("default", "压缩已中断。"), self.replies)
        self.assertNotIn(session.session_id, self.app._interrupted_sessions)
        self.assertFalse(self.app._session_is_busy(session))

    def test_status_probe_does_not_hold_up_dispatch(self) -> None:
        session = self.app.sessions.create_headless("claude", self.project, 1)
        self.app.sessions.switch(1, session.session_id, "worker")
        probe_started, release = threading.Event(), threading.Event()

        def slow_diagnostics(_session, *, full: bool) -> str:
            probe_started.set()
            release.wait(WAIT)
            return f"诊断结果 full={full}"

        self.app._session_diagnostics_text = slow_diagnostics  # type: ignore[method-assign]
        self.app._dispatch_update("default", self.text_update("/status"))
        self.assertTrue(probe_started.wait(WAIT))
        self.app._dispatch_update("worker", self.text_update("/where"))
        self.assertTrue(any(bot == "worker" and "当前" in text for bot, text in self.replies))
        release.set()
        self.wait_until(lambda: ("default", "诊断结果 full=True") in self.replies)

    def test_auto_send_runs_after_the_publish_lock_is_released(self) -> None:
        session = self.app.sessions.create_headless("pi", self.project, 1)
        image = self.project / "chart.png"
        image.write_bytes(b"\x89PNG fake")
        view = self.app._register_turn(session, "turn-image")
        view.parts.append(f"Saved `{image}`")
        view.status = "completed"
        view.dirty = True
        telegram = self.app._telegrams["default"]
        telegram.send_rich_markdown_parts = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: [77]
        )
        lock_free: list[bool] = []

        def upload(_chat_id: int, path: Path, **kwargs: Any) -> None:
            def probe() -> None:
                acquired = self.app._publish_lock.acquire(timeout=1)
                if acquired:
                    self.app._publish_lock.release()
                lock_free.append(acquired)

            prober = threading.Thread(target=probe)
            prober.start()
            prober.join()
            self.assertEqual((path, kwargs), (image, {"as_photo": True}))

        telegram.send_local_file = upload  # type: ignore[method-assign]

        class StopAfterOneIteration:
            calls = 0

            def wait(self, _timeout: float) -> bool:
                self.calls += 1
                return self.calls > 1

        self.app.stop_event = StopAfterOneIteration()  # type: ignore[assignment]
        self.app._status_loop()
        self.assertEqual(lock_free, [True])
        self.assertEqual(view.auto_sent, [str(image)])

    def test_republishing_a_final_card_reuses_artifact_tokens(self) -> None:
        session = self.app.sessions.create_headless("pi", self.project, 1)
        report = self.project / "report.txt"
        report.write_text("done", encoding="utf-8")
        view = self.app._register_turn(session, "turn-file")
        view.artifacts = [report]
        first = self.app._artifact_markup(view)
        second = self.app._artifact_markup(view)
        self.assertEqual(first, second)
        self.assertEqual(len(self.app.sessions._state["artifacts"]), 1)

    def test_command_finishing_while_its_card_is_sent_still_gets_buttons(self) -> None:
        result = self.project / "result.txt"
        result.write_text("done", encoding="utf-8")
        command = DirectCommand(
            id="demo", name="demo", command="demo", usage="", description="",
            argv=("/bin/true",), cwd=self.project, manifest_path=self.project / "manifest.json",
            timeout_seconds=30, max_output_bytes=4096,
        )
        run = DirectCommandRun(
            command=command, chat_id=1, bot_key="default", args=[], raw_args="", user_id=1
        )
        run.output_lines.append(f"wrote {result}\n")
        self.app.direct_commands._runs[run.turn_id] = run
        calls: list[tuple[str, Any]] = []

        def call(method: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            if method == "sendRichMessage":
                run.status = "completed"  # the runner thread finishes meanwhile
            calls.append((method, payload.get("reply_markup")))
            return {"message_id": 50}

        self.app._telegrams["default"]._call = call  # type: ignore[method-assign]
        self.app._pump_direct_commands(time.monotonic())
        self.assertIsNotNone(self.app.direct_commands.get(run.turn_id))
        self.app._pump_direct_commands(time.monotonic())
        self.assertIsNone(self.app.direct_commands.get(run.turn_id))
        method, markup = calls[-1]
        self.assertEqual(method, "editMessageText")
        self.assertIn("file:", json.dumps(markup))

    def test_reload_refreshes_model_listings(self) -> None:
        session = self.app.sessions.create_headless("pi", self.project, 1)
        with patch("cli_telegram_gateway.app.clear_listing_cache") as clear:
            self.app._reload_cli_backends(1, session.session_id)
        clear.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
