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
        self.event("item/agentMessage/delta", itemId="c", delta="work")
        self.event("item/agentMessage/delta", itemId="c", delta="ing")
        self.message("c", "working")
        self.message("c", "working")
        self.event("item/agentMessage/delta", itemId="c", delta="late duplicate event")
        self.assertEqual(self.app._turn_progress(self.view), "working")
        self.message("f", "final result", "final_answer")
        rendered, answer = self.app._render_turn(self.view, time.monotonic())
        self.assertEqual(answer, "final result")
        self.assertNotIn("working", rendered)

    def test_unknown_phase_and_legacy_delta_remain_visible(self) -> None:
        self.message("unknown", "legacy output", None)
        self.event("item/agentMessage/delta", delta="output without itemId")
        self.complete()
        _rendered, answer = self.app._render_turn(self.view, time.monotonic())
        self.assertIn("legacy output", answer)
        self.assertIn("without itemId", answer)

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
        source = "".join(f"Part {i:03d}: " + "answer " * 11 + "\n\n" for i in range(40))
        for start in range(0, len(source), 400):
            self.headless(session, view, "delta", source[start:start + 400])
        self.headless(session, view, "completed")
        self.assertEqual(list(self.messages), [view.message_id])
        body = self.messages[view.message_id]
        for i in range(40):
            self.assertEqual(body.count(f"Part {i:03d}: "), 1)
        self.assertNotIn("Run log", body)

    def test_claude_narration_becomes_a_record_and_the_result_gets_its_own_card(self) -> None:
        session, view = self.headless_turn("claude")
        self.headless(session, view, "message_delta", {"id": "m1", "delta": "Checking the date first."})
        self.headless(session, view, "command", "date -u")
        summary = "Summary → https://pages.example/p/abc"
        self.headless(session, view, "message_delta", {"id": "m2", "delta": summary})
        self.headless(session, view, "message_completed", {"id": "m2", "phase": "final_answer", "text": summary})
        self.headless(session, view, "completed")
        record_id, final_id = sorted(self.messages)
        self.assertIn("Run log", self.messages[record_id])
        self.assertIn("Checking the date", self.messages[record_id])
        self.assertNotIn(summary, self.messages[record_id])
        self.assertIn(summary, self.messages[final_id])
        self.assertNotIn("Checking the date", self.messages[final_id])
        sends = [payload for method, payload in self.calls if method == "sendRichMessage"]
        self.assertEqual([bool(payload.get("disable_notification")) for payload in sends], [True, False])

    def test_claude_reply_without_narration_stays_one_message(self) -> None:
        session, view = self.headless_turn("claude")
        self.headless(session, view, "message_delta", {"id": "m1", "delta": "Short answer"})
        self.headless(session, view, "message_completed", {"id": "m1", "phase": "final_answer", "text": "Short answer"})
        self.headless(session, view, "completed")
        self.assertEqual(list(self.messages), [view.message_id])
        self.assertIn("Short answer", self.messages[view.message_id])
        self.assertNotIn("Run log", self.messages[view.message_id])

    def test_claude_narration_rolls_into_records_while_the_turn_runs(self) -> None:
        session, view = self.headless_turn("claude")
        for n in range(1, 7):
            self.headless(session, view, "message_delta", {
                "id": f"m{n}", "delta": f"Step {n}: " + "checking the render pipeline. " * 30,
            })
            self.headless(session, view, "message_completed", {"id": f"m{n}", "phase": "commentary"})
            self.headless(session, view, "command", f"tool {n}")
        records = [body for body in self.messages.values() if "Run log" in body]
        self.assertGreaterEqual(len(records), 2)
        self.assertNotIn("Run log", self.messages[view.message_id])
        self.assertIn("Step 6:", self.messages[view.message_id])

        # The reply may be the answer until the turn ends, so it streams in the live card only.
        answer = "Final report: " + "the cut is ready. " * 200
        self.headless(session, view, "message_delta", {"id": "m7", "delta": answer})
        self.assertIn("Final report:", self.messages[view.message_id])
        self.assertFalse(any("Final report:" in body for body in self.messages.values() if "Run log" in body))

        self.headless(session, view, "message_completed", {"id": "m7", "phase": "final_answer", "text": answer})
        self.headless(session, view, "completed")
        bodies = [self.messages[key] for key in sorted(self.messages)]
        self.assertEqual(sum(body.count("Final report:") for body in bodies), 1)
        for n in range(1, 7):
            self.assertEqual(sum(body.count(f"Step {n}:") for body in bodies), 1)
        self.assertIn("Final report:", bodies[-1])
        self.assertNotIn("Run log", bodies[-1])

    def test_claude_reply_text_never_rolls_into_a_record(self) -> None:
        # After a short narration, the next 1,500-unit segment would end inside the
        # reply, which has no line break to cut at and may still be the answer.
        session, view = self.headless_turn("claude")
        self.headless(session, view, "message_delta", {"id": "m1", "delta": "Short narration before the tool."})
        self.headless(session, view, "message_completed", {"id": "m1", "phase": "commentary"})
        reply = "Reply word " * 300
        self.headless(session, view, "message_delta", {"id": "m2", "delta": reply})
        self.assertFalse(any("Reply word" in body for body in self.messages.values() if "Run log" in body))
        self.headless(session, view, "message_completed", {"id": "m2", "phase": "final_answer", "text": reply})
        self.headless(session, view, "completed")
        bodies = [self.messages[key] for key in sorted(self.messages)]
        self.assertEqual(sum(body.count("Reply word") for body in bodies), 300)

    def test_interrupted_claude_reply_stays_on_its_card_once(self) -> None:
        session, view = self.headless_turn("claude")
        self.headless(session, view, "message_delta", {"id": "m1", "delta": "Planning the edit. " * 100})
        self.headless(session, view, "message_completed", {"id": "m1", "phase": "commentary"})
        self.headless(session, view, "message_delta", {"id": "m2", "delta": "Partial reply before the stop."})
        self.headless(session, view, "interrupted", -15)
        bodies = [self.messages[key] for key in sorted(self.messages)]
        self.assertEqual(sum(body.count("Partial reply before the stop.") for body in bodies), 1)
        self.assertIn("Partial reply before the stop.", bodies[-1])
        self.assertTrue(any("Planning the edit." in body and "Run log" in body for body in bodies))

    def test_only_latest_segment_is_edited_and_history_survives_completion(self) -> None:
        source = "\n".join(f"step-{i:03d}: " + "content " * 3 for i in range(150))
        self.message("c", source)
        self.publish()
        active_id = self.view.message_id
        archived = {key: body for key, body in self.messages.items() if key != active_id}
        self.assertGreater(len(archived), 2)
        self.assertEqual(self.view.stale_message_ids, [])
        before = len(self.calls)
        self.message("c2", "checking the tail")
        self.publish()
        self.assertEqual([payload["message_id"] for method, payload in self.calls[before:] if method.startswith("edit")], [active_id])
        self.message("f", "check complete", "final_answer")
        self.publish()
        self.complete()
        self.finalize()
        self.app._resolve_stale(self.view)
        for key, body in archived.items():
            self.assertEqual(self.messages[key], body)
        progress_bodies = "\n".join(body for body in self.messages.values() if "Run log" in body)
        for i in range(150):
            self.assertIn(f"step-{i:03d}", progress_bodies)
        self.assertIn("checking the tail", progress_bodies)
        self.assertNotIn("step-", self.messages[self.view.message_id])
        self.assertIn("check complete", self.messages[self.view.message_id])

    def test_progress_is_silent_and_final_has_its_own_message(self) -> None:
        self.message("c", "progress note")
        self.publish()
        progress_id = self.view.message_id
        self.message("f", "final answer text", "final_answer")
        self.publish()
        final_id = self.view.message_id
        self.assertNotEqual(progress_id, final_id)
        self.assertIn("Run log", self.messages[progress_id])
        self.assertNotIn("progress note", self.messages[final_id])
        sends = [payload for method, payload in self.calls if method == "sendRichMessage"]
        self.assertTrue(sends[0]["disable_notification"])
        self.assertNotIn("disable_notification", sends[-1])
        self.complete()
        self.finalize()
        self.assertEqual(self.view.message_id, final_id)
        self.assertIn("Done", self.messages[final_id])

    def test_final_card_never_shows_progress_rendered_before_the_final(self) -> None:
        self.message("c", "some progress notes")
        self.publish()
        progress_id = self.view.message_id
        # The status loop renders, then the reader thread starts the final answer.
        stale, _answer = self.app._render_turn(self.view, time.monotonic())
        self.event("item/started", item={"type": "agentMessage", "id": "f", "phase": "final_answer", "text": ""})
        self.event("item/agentMessage/delta", itemId="f", delta="start of the final reply")
        self.app._publish_running_turn(self.view, stale)
        self.assertNotEqual(self.view.message_id, progress_id)
        self.assertIn("some progress notes", self.messages[progress_id])
        self.assertIn("start of the final reply", self.messages[self.view.message_id])
        self.assertNotIn("some progress notes", self.messages[self.view.message_id])
        notifying = [
            payload for method, payload in self.calls
            if method == "sendRichMessage" and not payload.get("disable_notification")
        ]
        self.assertEqual(len(notifying), 1)
        self.assertIn("start of the final reply", notifying[0]["rich_message"]["markdown"])

    def test_commentary_without_final_is_preserved_without_inventing_an_answer(self) -> None:
        self.message("c", "steps already done")
        self.publish()
        progress_id = self.view.message_id
        self.complete()
        self.finalize()
        self.assertIn("already done", self.messages[progress_id])
        self.assertIn("gave no separate final answer", self.messages[self.view.message_id])

    def test_rollover_failure_does_not_advance_the_cursor(self) -> None:
        self.message("c", "starting checks")
        self.publish()
        original_id = self.view.message_id
        self.message("long", "long paragraph " * 340)
        self.should_fail = lambda method, _payload: method == "editMessageText"
        with self.assertRaises(TelegramError):
            self.publish()
        self.assertEqual(self.view.progress_offset, 0)
        self.assertEqual(self.view.message_id, original_id)
        self.should_fail = None
        self.publish()
        self.assertGreater(self.view.progress_offset, 0)
        self.assertNotEqual(self.view.message_id, original_id)
        self.assertIn("Run log", self.messages[original_id])

    def test_failure_after_sealing_a_page_does_not_reedit_that_page(self) -> None:
        self.message("c", "starting checks")
        self.publish()
        original_id = self.view.message_id
        self.message("long", "content " * 225)
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
        self.message("c", "details " * 250)
        self.publish()
        self.message("f", "complete reply", "final_answer")
        self.complete()
        self.should_fail = lambda method, payload: method == "sendRichMessage" and "complete reply" in payload["rich_message"]["markdown"]
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
        self.message("c", "fallback" * 475)
        self.publish()
        self.assertFalse(self.view.rich_mode)
        self.assertGreater(len(self.messages), 2)
        self.assertTrue(all(payload.get("disable_notification") for method, payload in self.calls if method == "sendMessage"))
        archived = self.messages.copy()
        self.message("f", "reply after the HTML switch", "final_answer")
        self.complete()
        self.finalize()
        self.assertIn("reply after the HTML switch", self.messages[self.view.message_id])
        self.assertNotIn("fallback", self.messages[self.view.message_id])
        for key, body in archived.items():
            self.assertIn("fallback", self.messages[key])

    def test_steer_ack_and_final_stay_with_the_original_bot(self) -> None:
        worker = TelegramClient("worker-test")
        worker._call = self.call
        self.app._telegrams["worker"] = worker
        self.view.bot_key = "worker"
        self.app.sessions.switch(1, self.session.session_id, "worker")
        self.app.telegram._call = lambda *_args, **_kwargs: self.fail("wrong bot")
        self.message("c", "worker progress")
        self.publish()
        with self.app._bot_scope("worker"):
            self.app._send_to_session(1, self.session, "worker follow-up request")
        self.loop()
        ack_id = max(self.messages)
        self.assertIn("The CLI accepted", self.messages[ack_id])
        self.assertIsNone(self.app.sessions.session_for_message(1, ack_id))
        self.assertEqual(self.app.sessions.session_for_message(1, ack_id, "worker").session_id, self.session.session_id)
        self.message("f", "worker result", "final_answer")
        self.complete()
        self.loop()
        self.assertIn("worker result", self.messages[max(self.messages)])

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
            self.app._send_to_session(1, self.session, "follow-up request")
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertEqual(self.calls, [])
            self.assertEqual(len(self.view.steer_notices), 1)
        finally:
            release.set()
            thread.join(1)

    def test_steer_ack_is_below_old_progress_and_next_output_is_below_ack(self) -> None:
        self.message("c", "progress before the follow-up")
        self.publish()
        progress_id = self.view.message_id
        self.app._send_to_session(1, self.session, "follow-up request")
        self.message("new", "progress after the follow-up")
        self.loop()
        ack_id = max(self.messages)
        self.assertIn("The CLI accepted", self.messages[ack_id])
        self.assertIn("Run log", self.messages[progress_id])
        self.assertNotIn("after the follow-up", self.messages[progress_id])
        self.loop()
        self.assertGreater(self.view.message_id, ack_id)
        self.assertIn("after the follow-up", self.messages[self.view.message_id])
        self.assertNotIn("before the follow-up", self.messages[self.view.message_id])
        routed = self.app.sessions.session_for_message(1, ack_id)
        self.assertEqual(routed.session_id, self.session.session_id)

    def test_steer_ack_retry_survives_completion_and_does_not_replay_cli_input(self) -> None:
        accepted = []
        self.app.codex.steer_turn = lambda _thread, turn, text, _attachments=(): accepted.append(text) or turn
        self.message("c", "in progress")
        self.publish()
        self.app._send_to_session(1, self.session, "one follow-up")
        self.message("f", "final reply", "final_answer")
        self.complete()
        self.should_fail = lambda method, payload: method == "sendRichMessage" and "The CLI accepted" in payload["rich_message"]["markdown"]
        self.loop()
        self.assertEqual(len(self.view.steer_notices), 1)
        self.assertIn((self.session.session_id, "turn-stream"), self.app._turns)
        self.should_fail = None
        self.view.next_publish_at = 0
        self.loop(2)
        self.assertEqual(accepted, ["one follow-up"])
        self.assertEqual(len(self.view.steer_notices), 0)
        self.assertNotIn((self.session.session_id, "turn-stream"), self.app._turns)
        self.assertIn("final reply", self.messages[max(self.messages)])

    def test_steer_without_phases_keeps_a_single_copy_of_the_answer(self) -> None:
        # Deltas without an itemId carry no phase, so the text is the answer.
        self.event("item/agentMessage/delta", delta="Answer before the steer. " * 300)
        self.publish()
        first_id = self.view.message_id
        self.app._send_to_session(1, self.session, "follow-up request")
        self.loop()
        ack_id = max(self.messages)
        self.assertIn("The CLI accepted", self.messages[ack_id])
        self.event("item/agentMessage/delta", delta="Answer after the steer.")
        self.complete()
        self.loop(2)
        bodies = [self.messages[key] for key in sorted(self.messages)]
        self.assertEqual(sum(body.count("Answer before the steer. ") for body in bodies), 300)
        self.assertNotIn("Run log", "".join(bodies))
        self.assertGreater(max(self.messages), ack_id)
        self.assertIn("Answer after the steer.", self.messages[max(self.messages)])
        self.assertNotIn("before the follow-up", self.messages[first_id])

    def test_claude_steer_ack_precedes_its_progress_and_both_answers(self) -> None:
        session, view = self.headless_turn("claude")
        test_app.run_background_inline(self.app)
        self.app.headless.is_active = lambda _session_id: True  # type: ignore[method-assign]
        self.app.headless.steer_turn = (  # type: ignore[method-assign]
            lambda *_args: lambda _timeout: True
        )
        self.headless(session, view, "message_delta", {"id": "m1", "delta": "Reading the storyboard."})
        first_id = view.message_id
        self.app._send_to_session(1, session, "make the chorus louder")
        self.loop()
        ack_id = max(self.messages)
        self.assertIn("The CLI accepted", self.messages[ack_id])

        # Claude answered the original request, then the added one in a follow-up
        # turn; the backend reports both final messages when the process exits.
        answers = (("m2", "The first cut is ready."), ("m3", "The chorus is louder now."))
        for item_id, text in answers:
            self.app._update_turn(session, "turn-headless", "message_delta", {"id": item_id, "delta": text})
        for item_id, text in answers:
            self.app._update_turn(session, "turn-headless", "message_completed", {
                "id": item_id, "phase": "final_answer", "text": text,
            })
        self.app._update_turn(session, "turn-headless", "completed")
        self.loop(2)

        final_id = max(self.messages)
        self.assertGreater(final_id, ack_id)
        self.assertIn("The first cut is ready.", self.messages[final_id])
        self.assertIn("The chorus is louder now.", self.messages[final_id])
        self.assertNotIn("Reading the storyboard.", self.messages[final_id])
        records = [self.messages[key] for key in sorted(self.messages) if ack_id < key < final_id]
        self.assertTrue(any("Reading the storyboard." in record for record in records))
        self.assertNotIn("Reading the storyboard.", self.messages[first_id])

    def test_output_arriving_during_edit_keeps_the_view_dirty(self) -> None:
        self.message("c", "existing progress")
        self.publish()
        call = self.call
        injected = False

        def racing_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal injected
            if not injected:
                injected = True
                self.message("new", "new progress during the request")
            return call(method, payload)

        self.app.telegram._call = racing_call
        self.view.last_edit = 0
        self.loop()
        self.assertTrue(self.view.dirty)
        self.view.last_edit = 0
        self.loop()
        self.assertIn("during the request", self.messages[self.view.message_id])
        self.assertFalse(self.view.dirty)

    def test_background_final_edits_only_existing_card_and_leaves_archives(self) -> None:
        self.message("c", "background work " * 300)
        self.publish()
        active_id = self.view.message_id
        archived = {key: body for key, body in self.messages.items() if key != active_id}
        other = self.app.sessions.create_headless("pi", Path(self.temporary.name), 1)
        self.app.sessions.switch(1, other.session_id)
        self.app._background_turn(self.view, time.monotonic())
        self.message("f", "background final reply", "final_answer")
        self.complete()
        before = len(self.calls)
        with mock.patch.object(self.app, "_auto_send_artifacts") as auto_send:
            self.loop()
        self.assertFalse(auto_send.called)
        self.assertEqual([method for method, _payload in self.calls[before:]], ["editMessageText"])
        self.assertIn("background final reply", self.messages[active_id])
        for key, body in archived.items():
            self.assertEqual(self.messages[key], body)

    def test_switching_back_retires_only_the_duplicate_active_card(self) -> None:
        self.message("c", "earlier progress " * 324)
        self.publish()
        original_id = self.view.message_id
        archived = {key: body for key, body in self.messages.items() if key != original_id}
        self.app._background_turn(self.view, time.monotonic())
        self.app._pin_turn(self.view, time.monotonic())
        self.assertEqual(self.view.stale_message_ids, [original_id])
        self.message("f", "final result", "final_answer")
        self.complete()
        self.finalize()
        self.app._resolve_stale(self.view)
        for key, body in archived.items():
            self.assertEqual(self.messages[key], body)
        self.assertIn("This task finished", self.messages[original_id])

    def test_interruption_preserves_all_progress_and_its_resume_notice(self) -> None:
        self.message("c", "pre-interrupt " * 304)
        self.publish()
        self.complete("interrupted")
        self.finalize()
        self.assertIn("/resume", self.messages[self.view.message_id])
        self.assertIn("pre-interrupt", "".join(self.messages.values()))
        self.assertTrue(self.view.progress_closed)

    def test_only_real_final_answer_paths_are_scanned_for_artifacts(self) -> None:
        self.message("c", "in progress /srv/projects/artifacts/unrelated.txt")
        self.message("f", "final file /srv/projects/artifacts/result.txt", "final_answer")
        self.complete()
        rendered, _answer = self.app._render_turn(self.view, time.monotonic())
        with mock.patch.object(self.app, "_find_artifacts", return_value=[]) as find:
            self.app._publish_final_turn(self.view, rendered, auto_send=False)
        self.assertEqual(find.call_args.args[1], "final file /srv/projects/artifacts/result.txt")


if __name__ == "__main__":
    unittest.main()
