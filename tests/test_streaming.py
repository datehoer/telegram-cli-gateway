from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import test_app
from cli_telegram_gateway.telegram import TelegramError
from cli_telegram_gateway.telegram import TelegramClient


class LongTaskStreamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        project = Path(self.temporary.name)
        self.app = test_app.GatewayEventTests.make_app(self, project)
        self.session = self.app.sessions.create_virtual(
            "codex", project, 1, "codex-app-server", "thread-stream"
        )
        self.view = self.app._register_turn(self.session, "turn-stream")
        self.view.live = True
        self.app._codex_active_turns["thread-stream"] = "turn-stream"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.messages: dict[int, str] = {}
        self.next_id = 100
        self.should_fail = None
        self.app.telegram._call = self.call  # type: ignore[method-assign]
        self.app.codex.steer_turn = lambda _thread, turn, _text, _attachments=(): turn

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload.copy()))
        if self.should_fail is not None and self.should_fail(method, payload):
            raise TelegramError("rate limited", retry_after=2)
        if method.startswith("send"):
            self.next_id += 1
            message_id = self.next_id
        else:
            message_id = payload["message_id"]
        self.messages[message_id] = payload.get("rich_message", {}).get("markdown", payload.get("text", ""))
        return {"message_id": message_id}

    def event(self, method: str, **params: Any) -> None:
        self.app._on_codex_notification(method, {
            "threadId": "thread-stream", "turnId": "turn-stream", **params,
        })

    def message(self, item_id: str, text: str, phase: str | None = "commentary") -> None:
        self.event("item/completed", item={
            "type": "agentMessage", "id": item_id, "phase": phase, "text": text,
        })

    def publish(self) -> None:
        rendered, _answer = self.app._render_turn(self.view, time.monotonic())
        self.app._publish_running_turn(self.view, rendered)

    def complete(self, status: str = "completed") -> None:
        self.app._update_turn(
            self.session, "turn-stream", "completed" if status == "completed" else status,
            "test failure" if status == "error" else None,
        )

    def finalize(self) -> None:
        rendered, _answer = self.app._render_turn(self.view, time.monotonic())
        self.app._publish_final_turn(self.view, rendered, with_artifacts=False)

    def loop(self, count: int = 1) -> None:
        class StopAfter:
            calls = 0

            def wait(self, _timeout: float) -> bool:
                self.calls += 1
                return self.calls > count

        self.app.stop_event = StopAfter()  # type: ignore[assignment]
        self.app._status_loop()

    def test_native_item_phases_and_completion_text_are_not_duplicated(self) -> None:
        self.event("item/started", item={"type": "agentMessage", "id": "c", "phase": "commentary", "text": ""})
        self.event("item/agentMessage/delta", itemId="c", delta="正在")
        self.event("item/agentMessage/delta", itemId="c", delta="处理")
        self.message("c", "正在处理")
        self.message("c", "正在处理")
        self.event("item/agentMessage/delta", itemId="c", delta="重复的迟到事件")
        self.assertEqual(self.app._turn_progress(self.view), "正在处理")
        self.message("f", "最终结果", "final_answer")
        rendered, answer = self.app._render_turn(self.view, time.monotonic())
        self.assertEqual(answer, "最终结果")
        self.assertNotIn("正在处理", rendered)

    def test_unknown_phase_and_legacy_delta_remain_visible(self) -> None:
        self.message("unknown", "旧版输出", None)
        self.event("item/agentMessage/delta", delta="没有 itemId 的输出")
        self.complete()
        _rendered, answer = self.app._render_turn(self.view, time.monotonic())
        self.assertIn("旧版输出", answer)
        self.assertIn("没有 itemId", answer)

    def headless_turn(self, cli: str) -> tuple[Any, Any]:
        session = self.app.sessions.create_headless(cli, Path(self.temporary.name), 1)
        self.app.sessions.switch(1, session.session_id)
        view = self.app._register_turn(session, "turn-headless")
        view.live = True
        return session, view

    def headless(self, session: Any, view: Any, kind: str, data: Any = None) -> None:
        self.app._on_headless_event(session.session_id, "turn-headless", kind, data)
        rendered, _answer = self.app._render_turn(view, time.monotonic())
        if view.status == "running":
            self.app._publish_running_turn(view, rendered)
        else:
            self.app._publish_final_turn(view, rendered, with_artifacts=False)
            self.app._resolve_stale(view)

    def test_long_reply_without_phases_stays_one_message(self) -> None:
        # Pi and Grok stream the answer itself; reading records would repeat it.
        session, view = self.headless_turn("pi")
        source = "".join(f"第 {i:03d} 段：" + "回答内容" * 20 + "\n\n" for i in range(40))
        for start in range(0, len(source), 400):
            self.headless(session, view, "delta", source[start:start + 400])
        self.headless(session, view, "completed")
        self.assertEqual(list(self.messages), [view.message_id])
        body = self.messages[view.message_id]
        for i in range(40):
            self.assertEqual(body.count(f"第 {i:03d} 段："), 1)
        self.assertNotIn("运行记录", body)

    def test_claude_narration_becomes_a_record_and_the_result_gets_its_own_card(self) -> None:
        session, view = self.headless_turn("claude")
        self.headless(session, view, "message_delta", {"id": "m1", "delta": "先检查日期。"})
        self.headless(session, view, "command", "date -u")
        summary = "摘要 → https://pages.example/p/abc"
        self.headless(session, view, "message_delta", {"id": "m2", "delta": summary})
        self.headless(session, view, "message_completed", {"id": "m2", "phase": "final_answer", "text": summary})
        self.headless(session, view, "completed")
        record_id, final_id = sorted(self.messages)
        self.assertIn("运行记录", self.messages[record_id])
        self.assertIn("先检查日期", self.messages[record_id])
        self.assertNotIn(summary, self.messages[record_id])
        self.assertIn(summary, self.messages[final_id])
        self.assertNotIn("先检查日期", self.messages[final_id])
        sends = [payload for method, payload in self.calls if method == "sendRichMessage"]
        self.assertEqual([bool(payload.get("disable_notification")) for payload in sends], [True, False])

    def test_claude_reply_without_narration_stays_one_message(self) -> None:
        session, view = self.headless_turn("claude")
        self.headless(session, view, "message_delta", {"id": "m1", "delta": "简短回答"})
        self.headless(session, view, "message_completed", {"id": "m1", "phase": "final_answer", "text": "简短回答"})
        self.headless(session, view, "completed")
        self.assertEqual(list(self.messages), [view.message_id])
        self.assertIn("简短回答", self.messages[view.message_id])
        self.assertNotIn("运行记录", self.messages[view.message_id])

    def test_only_latest_segment_is_edited_and_history_survives_completion(self) -> None:
        source = "\n".join(f"步骤-{i:03d}：" + "内容" * 15 for i in range(150))
        self.message("c", source)
        self.publish()
        active_id = self.view.message_id
        archived = {key: body for key, body in self.messages.items() if key != active_id}
        self.assertGreater(len(archived), 2)
        self.assertEqual(self.view.stale_message_ids, [])
        before = len(self.calls)
        self.message("c2", "继续检查尾部")
        self.publish()
        self.assertEqual([payload["message_id"] for method, payload in self.calls[before:] if method.startswith("edit")], [active_id])
        self.message("f", "检查完成", "final_answer")
        self.publish()
        self.complete()
        self.finalize()
        self.app._resolve_stale(self.view)
        for key, body in archived.items():
            self.assertEqual(self.messages[key], body)
        progress_bodies = "\n".join(body for body in self.messages.values() if "运行记录" in body)
        for i in range(150):
            self.assertIn(f"步骤-{i:03d}", progress_bodies)
        self.assertIn("继续检查尾部", progress_bodies)
        self.assertNotIn("步骤-", self.messages[self.view.message_id])
        self.assertIn("检查完成", self.messages[self.view.message_id])

    def test_progress_is_silent_and_final_has_its_own_message(self) -> None:
        self.message("c", "过程说明")
        self.publish()
        progress_id = self.view.message_id
        self.message("f", "最终回答", "final_answer")
        self.publish()
        final_id = self.view.message_id
        self.assertNotEqual(progress_id, final_id)
        self.assertIn("运行记录", self.messages[progress_id])
        self.assertNotIn("过程说明", self.messages[final_id])
        sends = [payload for method, payload in self.calls if method == "sendRichMessage"]
        self.assertTrue(sends[0]["disable_notification"])
        self.assertNotIn("disable_notification", sends[-1])
        self.complete()
        self.finalize()
        self.assertEqual(self.view.message_id, final_id)
        self.assertIn("完成", self.messages[final_id])

    def test_final_card_never_shows_progress_rendered_before_the_final(self) -> None:
        self.message("c", "一些过程说明")
        self.publish()
        progress_id = self.view.message_id
        # The status loop renders, then the reader thread starts the final answer.
        stale, _answer = self.app._render_turn(self.view, time.monotonic())
        self.event("item/started", item={"type": "agentMessage", "id": "f", "phase": "final_answer", "text": ""})
        self.event("item/agentMessage/delta", itemId="f", delta="最终答复开头")
        self.app._publish_running_turn(self.view, stale)
        self.assertNotEqual(self.view.message_id, progress_id)
        self.assertIn("一些过程说明", self.messages[progress_id])
        self.assertIn("最终答复开头", self.messages[self.view.message_id])
        self.assertNotIn("一些过程说明", self.messages[self.view.message_id])
        notifying = [
            payload for method, payload in self.calls
            if method == "sendRichMessage" and not payload.get("disable_notification")
        ]
        self.assertEqual(len(notifying), 1)
        self.assertIn("最终答复开头", notifying[0]["rich_message"]["markdown"])

    def test_commentary_without_final_is_preserved_without_inventing_an_answer(self) -> None:
        self.message("c", "已经做完的步骤")
        self.publish()
        progress_id = self.view.message_id
        self.complete()
        self.finalize()
        self.assertIn("已经做完", self.messages[progress_id])
        self.assertIn("没有提供单独的最终回答", self.messages[self.view.message_id])

    def test_rollover_failure_does_not_advance_the_cursor(self) -> None:
        self.message("c", "开始检查")
        self.publish()
        original_id = self.view.message_id
        self.message("long", "长段落" * 1700)
        self.should_fail = lambda method, _payload: method == "editMessageText"
        with self.assertRaises(TelegramError):
            self.publish()
        self.assertEqual(self.view.progress_offset, 0)
        self.assertEqual(self.view.message_id, original_id)
        self.should_fail = None
        self.publish()
        self.assertGreater(self.view.progress_offset, 0)
        self.assertNotEqual(self.view.message_id, original_id)
        self.assertIn("运行记录", self.messages[original_id])

    def test_failure_after_sealing_a_page_does_not_reedit_that_page(self) -> None:
        self.message("c", "开始检查")
        self.publish()
        original_id = self.view.message_id
        self.message("long", "内容" * 900)
        self.should_fail = lambda method, _payload: method == "sendRichMessage"
        with self.assertRaises(TelegramError):
            self.publish()
        cursor = self.view.progress_offset
        sealed = self.messages[original_id]
        self.assertGreater(cursor, 0)
        before = len(self.calls)
        self.should_fail = None
        self.publish()
        self.assertEqual(self.view.progress_offset, cursor)
        self.assertEqual(self.messages[original_id], sealed)
        self.assertFalse(any(payload.get("message_id") == original_id for _method, payload in self.calls[before:]))

    def test_final_retry_does_not_republish_archived_progress(self) -> None:
        self.message("c", "详细过程" * 500)
        self.publish()
        self.message("f", "完成答复", "final_answer")
        self.complete()
        self.should_fail = lambda method, payload: method == "sendRichMessage" and "完成答复" in payload["rich_message"]["markdown"]
        with self.assertRaises(TelegramError):
            self.finalize()
        self.assertTrue(self.view.progress_closed)
        archived = self.messages.copy()
        before = len(self.calls)
        self.should_fail = None
        self.finalize()
        self.assertEqual(len(self.calls) - before, 1)
        for key, body in archived.items():
            self.assertEqual(self.messages[key], body)

    def test_html_fallback_preserves_segments_and_keeps_progress_silent(self) -> None:
        call = self.call

        def unsupported_rich(method: str, payload: dict[str, Any]) -> dict[str, Any]:
            if method == "sendRichMessage":
                raise TelegramError("unsupported rich messages")
            return call(method, payload)

        self.app.telegram._call = unsupported_rich
        self.message("c", "回退过程" * 950)
        self.publish()
        self.assertFalse(self.view.rich_mode)
        self.assertGreater(len(self.messages), 2)
        self.assertTrue(all(payload.get("disable_notification") for method, payload in self.calls if method == "sendMessage"))
        archived = self.messages.copy()
        self.message("f", "回退后的最终答复", "final_answer")
        self.complete()
        self.finalize()
        self.assertIn("回退后的最终答复", self.messages[self.view.message_id])
        self.assertNotIn("回退过程", self.messages[self.view.message_id])
        for key, body in archived.items():
            self.assertIn("回退过程", self.messages[key])

    def test_steer_ack_and_final_stay_with_the_original_bot(self) -> None:
        worker = TelegramClient("worker-test")
        worker._call = self.call
        self.app._telegrams["worker"] = worker
        self.view.bot_key = "worker"
        self.app.sessions.switch(1, self.session.session_id, "worker")
        self.app.telegram._call = lambda *_args, **_kwargs: self.fail("wrong bot")
        self.message("c", "worker 的过程")
        self.publish()
        with self.app._bot_scope("worker"):
            self.app._send_to_session(1, self.session, "worker 的追加要求")
        self.loop()
        ack_id = max(self.messages)
        self.assertIn("CLI 已接收", self.messages[ack_id])
        self.assertIsNone(self.app.sessions.session_for_message(1, ack_id))
        self.assertEqual(self.app.sessions.session_for_message(1, ack_id, "worker").session_id, self.session.session_id)
        self.message("f", "worker 的结果", "final_answer")
        self.complete()
        self.loop()
        self.assertIn("worker 的结果", self.messages[max(self.messages)])

    def test_accepted_steer_does_not_wait_for_the_publisher_lock(self) -> None:
        acquired = threading.Event()
        release = threading.Event()

        def hold_publisher() -> None:
            with self.app._publish_lock:
                acquired.set()
                release.wait(3)

        thread = threading.Thread(target=hold_publisher)
        thread.start()
        try:
            self.assertTrue(acquired.wait(1))
            start = time.monotonic()
            self.app._send_to_session(1, self.session, "追加要求")
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(self.calls, [])
            self.assertEqual(len(self.view.steer_notices), 1)
        finally:
            release.set()
            thread.join(1)

    def test_steer_ack_is_below_old_progress_and_next_output_is_below_ack(self) -> None:
        self.message("c", "追加之前的过程")
        self.publish()
        progress_id = self.view.message_id
        self.app._send_to_session(1, self.session, "追加要求")
        self.message("new", "追加之后的过程")
        self.loop()
        ack_id = max(self.messages)
        self.assertIn("CLI 已接收", self.messages[ack_id])
        self.assertIn("运行记录", self.messages[progress_id])
        self.assertNotIn("追加之后", self.messages[progress_id])
        self.loop()
        self.assertGreater(self.view.message_id, ack_id)
        self.assertIn("追加之后", self.messages[self.view.message_id])
        self.assertNotIn("追加之前", self.messages[self.view.message_id])
        routed = self.app.sessions.session_for_message(1, ack_id)
        self.assertEqual(routed.session_id, self.session.session_id)

    def test_steer_ack_retry_survives_completion_and_does_not_replay_cli_input(self) -> None:
        accepted = []
        self.app.codex.steer_turn = lambda _thread, turn, text, _attachments=(): accepted.append(text) or turn
        self.message("c", "过程中")
        self.publish()
        self.app._send_to_session(1, self.session, "追加一次")
        self.message("f", "最终答复", "final_answer")
        self.complete()
        self.should_fail = lambda method, payload: method == "sendRichMessage" and "CLI 已接收" in payload["rich_message"]["markdown"]
        self.loop()
        self.assertEqual(len(self.view.steer_notices), 1)
        self.assertIn((self.session.session_id, "turn-stream"), self.app._turns)
        self.should_fail = None
        self.view.next_publish_at = 0
        self.loop(2)
        self.assertEqual(accepted, ["追加一次"])
        self.assertEqual(len(self.view.steer_notices), 0)
        self.assertNotIn((self.session.session_id, "turn-stream"), self.app._turns)
        self.assertIn("最终答复", self.messages[max(self.messages)])

    def test_steer_without_phases_keeps_a_single_copy_of_the_answer(self) -> None:
        # Deltas without an itemId carry no phase, so the text is the answer.
        self.event("item/agentMessage/delta", delta="追加之前的回答。" * 300)
        self.publish()
        first_id = self.view.message_id
        self.app._send_to_session(1, self.session, "追加要求")
        self.loop()
        ack_id = max(self.messages)
        self.assertIn("CLI 已接收", self.messages[ack_id])
        self.event("item/agentMessage/delta", delta="追加之后的回答。")
        self.complete()
        self.loop(2)
        bodies = [self.messages[key] for key in sorted(self.messages)]
        self.assertEqual(sum(body.count("追加之前的回答。") for body in bodies), 300)
        self.assertNotIn("运行记录", "".join(bodies))
        self.assertGreater(max(self.messages), ack_id)
        self.assertIn("追加之后的回答。", self.messages[max(self.messages)])
        self.assertNotIn("追加之前", self.messages[first_id])

    def test_output_arriving_during_edit_keeps_the_view_dirty(self) -> None:
        self.message("c", "已有过程")
        self.publish()
        call = self.call
        injected = False

        def racing_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal injected
            if not injected:
                injected = True
                self.message("new", "网络请求期间的新进展")
            return call(method, payload)

        self.app.telegram._call = racing_call
        self.view.last_edit = 0
        self.loop()
        self.assertTrue(self.view.dirty)
        self.view.last_edit = 0
        self.loop()
        self.assertIn("网络请求期间", self.messages[self.view.message_id])
        self.assertFalse(self.view.dirty)

    def test_background_final_edits_only_existing_card_and_leaves_archives(self) -> None:
        self.message("c", "后台过程" * 1200)
        self.publish()
        active_id = self.view.message_id
        archived = {key: body for key, body in self.messages.items() if key != active_id}
        other = self.app.sessions.create_headless("pi", Path(self.temporary.name), 1)
        self.app.sessions.switch(1, other.session_id)
        self.app._background_turn(self.view, time.monotonic())
        self.message("f", "后台的最终答复", "final_answer")
        self.complete()
        before = len(self.calls)
        with mock.patch.object(self.app, "_auto_send_artifacts") as auto_send:
            self.loop()
        self.assertFalse(auto_send.called)
        self.assertEqual([method for method, _payload in self.calls[before:]], ["editMessageText"])
        self.assertIn("后台的最终答复", self.messages[active_id])
        for key, body in archived.items():
            self.assertEqual(self.messages[key], body)

    def test_switching_back_retires_only_the_duplicate_active_card(self) -> None:
        self.message("c", "之前的过程" * 1100)
        self.publish()
        original_id = self.view.message_id
        archived = {key: body for key, body in self.messages.items() if key != original_id}
        self.app._background_turn(self.view, time.monotonic())
        self.app._pin_turn(self.view, time.monotonic())
        self.assertEqual(self.view.stale_message_ids, [original_id])
        self.message("f", "最终结果", "final_answer")
        self.complete()
        self.finalize()
        self.app._resolve_stale(self.view)
        for key, body in archived.items():
            self.assertEqual(self.messages[key], body)
        self.assertIn("此任务已完成", self.messages[original_id])

    def test_interruption_preserves_all_progress_and_its_resume_notice(self) -> None:
        self.message("c", "中断前过程" * 850)
        self.publish()
        self.complete("interrupted")
        self.finalize()
        self.assertIn("/resume", self.messages[self.view.message_id])
        self.assertIn("中断前过程", "".join(self.messages.values()))
        self.assertTrue(self.view.progress_closed)

    def test_only_real_final_answer_paths_are_scanned_for_artifacts(self) -> None:
        self.message("c", "过程中的 /srv/projects/artifacts/unrelated.txt")
        self.message("f", "最终文件 /srv/projects/artifacts/result.txt", "final_answer")
        self.complete()
        rendered, _answer = self.app._render_turn(self.view, time.monotonic())
        with mock.patch.object(self.app, "_find_artifacts", return_value=[]) as find:
            self.app._publish_final_turn(self.view, rendered, auto_send=False)
        self.assertEqual(find.call_args.args[1], "最终文件 /srv/projects/artifacts/result.txt")


if __name__ == "__main__":
    unittest.main()
