from __future__ import annotations

import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from cli_telegram_gateway.app import PendingInput, STATE_FAILED, STATE_IDLE, STATE_INTERRUPTED
from cli_telegram_gateway.codex_backend import CodexBackendError
import test_app


class LifecycleRegressionTests(unittest.TestCase):
    make_app = test_app.GatewayEventTests.make_app

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.app = self.make_app(
            self.project, bot_tokens=(("default", "test:one"), ("worker", "test:two"))
        )
        self.notices: list[str] = []
        self.app._send = lambda _chat, text, *_args: self.notices.append(text)
        for telegram in self.app._telegrams.values():
            telegram._call = lambda *_args, **_kwargs: {"message_id": 10}

    def session(self, cli: str = "pi"):
        if cli == "codex":
            return self.app.sessions.create_virtual(
                cli, self.project, 1, "codex-app-server", "thread-1"
            )
        return self.app.sessions.create_headless(cli, self.project, 1)

    def running(self, cli: str = "pi"):
        session = self.session(cli)
        self.app._register_turn(session, "turn-1")
        self.app._turn_claims.add(session.session_id)
        self.app.sessions.set_in_flight(session.session_id, 1, "turn-1")
        if cli == "codex":
            self.app._codex_active_turns["thread-1"] = "turn-1"
        return session

    def codex_error(self, *, retry: bool = False) -> dict:
        return {
            "threadId": "thread-1", "turnId": "turn-1", "willRetry": retry,
            "error": {"message": "Selected model is at capacity.", "codexErrorInfo": "serverOverloaded"},
        }

    def test_codex_error_waits_for_completion_and_releases_failed_turn(self) -> None:
        session = self.running("codex")
        self.app._on_codex_notification("error", self.codex_error())
        view = self.app._turns[(session.session_id, "turn-1")]
        self.assertEqual(view.status, "running")
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "turn-1")
        self.assertTrue(self.app._session_is_busy(session))
        self.assertIn("Selected model is at capacity.", self.app._render_turn(view, view.started_at)[0])

        self.app._on_codex_notification("turn/completed", {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "failed", "error": self.codex_error()["error"]},
        })
        self.assertEqual(view.status, "failed")
        self.assertEqual(view.error, "Selected model is at capacity.")
        self.assertEqual(self.app._session_states[session.session_id], STATE_FAILED)
        self.assertFalse(self.app._session_is_busy(session))
        self.assertIsNone(self.app.sessions.get_in_flight(session.session_id))
        self.app.codex.steer_turn = lambda *_: self.fail("must not steer a finished turn")
        started = []
        self.app.codex.start_turn = lambda *_a, **_kw: started.append(True) or "turn-2"
        self.app._send_to_session(1, session, "next")
        self.assertEqual(started, [True])
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "turn-2")

    def test_codex_retry_error_keeps_accepting_output_and_success(self) -> None:
        session = self.running("codex")
        self.app._on_codex_notification("error", self.codex_error(retry=True))
        view = self.app._turns[(session.session_id, "turn-1")]
        rendered, _ = self.app._render_turn(view, view.started_at)
        self.assertEqual(view.status, "running")
        self.assertIn("重试", rendered)
        self.assertNotIn((session.session_id, "turn-1"), self.app._finished_turns)
        self.app._on_codex_notification("item/agentMessage/delta", {
            "threadId": "thread-1", "turnId": "turn-1", "delta": "recovered answer",
        })
        self.assertNotIn("at capacity", self.app._render_turn(view, view.started_at)[0])
        self.app._on_codex_notification("turn/completed", {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"},
        })
        rendered, answer = self.app._render_turn(view, view.started_at)
        self.assertEqual(answer, "recovered answer")
        self.assertEqual(view.status, "completed")
        self.assertNotIn("at capacity", rendered)
        self.assertFalse(self.app._session_is_busy(session))

    def test_codex_error_then_disconnect_preserves_recovery(self) -> None:
        session = self.running("codex")
        self.app._on_codex_notification("error", self.codex_error())
        self.app._on_codex_disconnect()
        view = self.app._turns[(session.session_id, "turn-1")]
        self.assertEqual(view.status, "interrupted")
        self.assertTrue(view.resumable)
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "turn-1")
        self.assertFalse(self.app._session_is_busy(session))

    def test_codex_failed_completion_drains_queue_once_and_ignores_late_events(self) -> None:
        session = self.running("codex")
        self.app._enqueue(session, PendingInput(1, "queued next", bot_key="worker"))
        started = []
        successor_started = threading.Event()

        def start(*_args, **_kwargs):
            started.append(True)
            return "turn-2"

        self.app.codex.start_turn = start
        original_drain = self.app._start_next_queued

        def drain(selected):
            original_drain(selected)
            successor_started.set()

        self.app._start_next_queued = drain
        completion = {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "failed", "error": self.codex_error()["error"]},
        }
        self.app._on_codex_notification("error", self.codex_error())
        self.assertEqual(started, [])
        self.app._on_codex_notification("turn/completed", completion)
        self.assertTrue(successor_started.wait(2))
        self.app._on_codex_notification("turn/completed", completion)
        self.app._on_codex_notification("error", self.codex_error())
        self.assertEqual(started, [True])
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "turn-2")
        self.assertTrue(self.app._session_is_busy(session))
        self.assertEqual(self.app._turns[(session.session_id, "turn-2")].bot_key, "worker")

    def test_codex_failed_events_before_start_response_release_claim(self) -> None:
        session = self.session("codex")

        def start(*_args, **_kwargs):
            self.app._on_codex_notification("turn/started", {
                "threadId": "thread-1", "turn": {"id": "turn-1"},
            })
            self.app._on_codex_notification("error", self.codex_error())
            self.app._on_codex_notification("turn/completed", {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "failed", "error": self.codex_error()["error"]},
            })
            return "turn-1"

        self.app.codex.start_turn = start
        with self.app._bot_scope("worker"):
            self.app._start_session_turn(1, session, "go")
        view = self.app._turns[(session.session_id, "turn-1")]
        self.assertEqual(view.bot_key, "worker")
        self.assertEqual(view.status, "failed")
        self.assertFalse(self.app._session_is_busy(session))
        self.assertIsNone(self.app.sessions.get_in_flight(session.session_id))

    def test_headless_interrupt_callback_before_return_releases_busy(self) -> None:
        session = self.running()

        def interrupt(_sid):
            self.app._on_headless_event(session.session_id, "turn-1", "interrupted", -15)
            return True

        self.app.headless.interrupt = interrupt
        self.assertTrue(self.app._interrupt_session(session))
        view = self.app._turns[(session.session_id, "turn-1")]
        self.assertFalse(view.resumable)
        self.assertFalse(self.app._session_is_busy(session))
        self.assertEqual(self.app._session_states[session.session_id], STATE_IDLE)
        self.assertIsNone(self.app.sessions.get_in_flight(session.session_id))

    def test_unexpected_interrupt_releases_busy_but_preserves_recovery_and_queue(self) -> None:
        session = self.running()
        self.app._enqueue(session, PendingInput(1, "next"))
        self.app._start_next_queued = lambda *_: self.fail("must await recovery decision")
        self.app._on_headless_event(session.session_id, "turn-1", "interrupted", 143)
        self.assertFalse(self.app._session_is_busy(session))
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "turn-1")
        self.assertEqual(len(self.app._queues[session.session_id]), 1)
        self.assertEqual(self.app._session_states[session.session_id], STATE_INTERRUPTED)

    def test_codex_interrupt_is_not_published_as_success(self) -> None:
        session = self.running("codex")

        def interrupt(_thread, _turn):
            self.app._on_codex_notification("turn/completed", {
                "threadId": "thread-1", "turn": {"id": "turn-1", "status": "interrupted"}
            })

        self.app.codex.interrupt = interrupt
        self.assertTrue(self.app._interrupt_session(session))
        view = self.app._turns[(session.session_id, "turn-1")]
        self.assertEqual(view.status, "interrupted")
        self.assertFalse(view.resumable)
        self.assertFalse(self.app._session_is_busy(session))
        self.assertIsNone(self.app.sessions.get_in_flight(session.session_id))

    def test_fast_headless_completion_cannot_restore_in_flight(self) -> None:
        session = self.session()

        def start(_session, _text, _attachments):
            self.app._on_headless_event(session.session_id, "fast", "delta", "done")
            self.app._on_headless_event(session.session_id, "fast", "completed", None)
            return "fast"

        self.app.headless.start_turn = start
        with self.app._bot_scope("worker"):
            self.app._start_session_turn(1, session, "go")
        view = self.app._turns[(session.session_id, "fast")]
        self.assertEqual(view.status, "completed")
        self.assertEqual(view.bot_key, "worker")
        self.assertIsNone(self.app.sessions.get_in_flight(session.session_id))
        self.assertFalse(self.app._session_is_busy(session))
        self.assertEqual(self.app._session_states[session.session_id], STATE_IDLE)

    def test_codex_events_before_start_response_keep_origin_and_terminal_state(self) -> None:
        session = self.session("codex")

        def start(*_args, **_kwargs):
            def notify():
                self.app._on_codex_notification("turn/started", {
                    "threadId": "thread-1", "turn": {"id": "fast"}
                })
                self.app._on_codex_notification("turn/completed", {
                    "threadId": "thread-1", "turn": {"id": "fast", "status": "completed"}
                })
            reader = threading.Thread(target=notify)
            reader.start()
            reader.join(2)
            self.assertFalse(reader.is_alive(), "RPC reader must not wait for start response")
            return "fast"

        self.app.codex.start_turn = start
        with self.app._bot_scope("worker"):
            self.app._start_session_turn(1, session, "go")
        self.assertEqual(self.app._turns[(session.session_id, "fast")].bot_key, "worker")
        self.assertIsNone(self.app.sessions.get_in_flight(session.session_id))
        self.assertFalse(self.app._session_is_busy(session))

    def test_queued_turn_refreshes_native_resume_count_and_model(self) -> None:
        session = self.session("claude")
        self.app._enqueue(session, PendingInput(1, "next"))
        self.app.sessions.increment_turn_count(session.session_id)
        self.app.sessions.set_model(session.session_id, "updated-model")
        captured = []
        self.app.headless.start_turn = lambda fresh, *_: captured.append(fresh) or "next"
        self.app._start_next_queued(session)
        self.assertEqual(captured[0].turn_count, 1)
        self.assertEqual(captured[0].model, "updated-model")

    def test_resume_and_cancel_reject_live_turns(self) -> None:
        session = self.running()
        self.app._handle_command(1, "resume", "")
        self.app._handle_command(1, "cancel", "")
        self.assertNotIn(session.session_id, self.app._queues)
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "turn-1")
        self.assertTrue(self.app._session_is_busy(session))

    def test_archiving_clears_queue_before_terminal_callback(self) -> None:
        session = self.running()
        self.app._enqueue(session, PendingInput(1, "must not run"))

        def interrupt(_session):
            self.assertNotIn(session.session_id, self.app._queues)
            self.app._on_headless_event(session.session_id, "turn-1", "completed", None)
            return True

        self.app._interrupt_session = interrupt
        self.app._start_next_queued = lambda *_: self.fail("archived queue ran")
        self.app._archive_session(1, session)
        self.assertTrue(self.app.sessions.get(session.session_id).archived)

    def test_failed_resume_preserves_existing_codex_thread(self) -> None:
        session = self.session("codex")
        self.app.codex.resume_thread = lambda *_a, **_kw: (_ for _ in ()).throw(
            CodexBackendError("temporary failure")
        )
        self.app.codex.start_thread = lambda *_a, **_kw: self.fail("must not replace context")
        self.app._prepare_sessions()
        self.assertEqual(self.app.sessions.get(session.session_id).external_id, "thread-1")

    def test_shutdown_does_not_start_queued_work(self) -> None:
        session = self.running()
        self.app._enqueue(session, PendingInput(1, "must not run"))
        self.app.stop_event.set()
        self.app._start_next_queued = lambda *_: self.fail("shutdown launched work")
        self.app._on_headless_event(session.session_id, "turn-1", "completed", None)

    def test_disconnect_preserves_recovery_and_stops_queue(self) -> None:
        session = self.running("codex")
        self.app._enqueue(session, PendingInput(1, "wait"))
        self.app._on_codex_disconnect()
        self.assertFalse(self.app._session_is_busy(session))
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "turn-1")
        self.assertEqual(len(self.app._queues[session.session_id]), 1)

    def test_late_terminal_event_cannot_finish_successor(self) -> None:
        session = self.running()
        self.app._on_headless_event(session.session_id, "turn-1", "completed", None)
        self.app._turns.pop((session.session_id, "turn-1"))
        self.app._register_turn(session, "turn-2")
        self.app._turn_claims.add(session.session_id)
        self.app.sessions.set_in_flight(session.session_id, 1, "turn-2")
        self.app._on_headless_event(session.session_id, "turn-1", "error", "late")
        self.assertTrue(self.app._session_is_busy(session))
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "turn-2")
        self.assertEqual(self.app._turns[(session.session_id, "turn-2")].status, "running")

    def test_interrupt_does_not_clear_successors_recovery_record(self) -> None:
        session = self.running()
        self.app._enqueue(session, PendingInput(1, "next"))
        self.app.headless.start_turn = lambda *_: "turn-2"

        def interrupt(_sid):
            self.app._on_headless_event(session.session_id, "turn-1", "interrupted", -15)
            return True

        self.app.headless.interrupt = interrupt
        self.app._interrupt_session(session)
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "turn-2")
        self.assertTrue(self.app._session_is_busy(session))

    def test_ambiguous_steer_is_never_replayed_as_queued_turn(self) -> None:
        session = self.running("codex")

        def steer(*_args):
            raise CodexBackendError("timeout after possible acceptance", ambiguous=True)

        self.app.codex.steer_turn = steer
        self.app._send_to_session(1, session, "do this once")
        self.assertNotIn(session.session_id, self.app._queues)
        self.assertIn("未自动重发", self.notices[-1])

    def test_delete_removes_pending_publications_and_queue(self) -> None:
        session = self.running()
        self.app._enqueue(session, PendingInput(1, "must not run"))
        self.app.headless.interrupt = lambda *_: True
        self.app._retire_session(session, delete=True)
        self.assertIsNone(self.app.sessions.get(session.session_id))
        self.assertNotIn((session.session_id, "turn-1"), self.app._turns)
        self.assertNotIn(session.session_id, self.app._turn_claims)
        self.assertNotIn(session.session_id, self.app._queues)

    def test_switch_back_uses_full_result_instead_of_truncated_card(self) -> None:
        session = self.session()
        full = "full result " * 4000
        self.app.sessions.set_last_completed_message(session.session_id, 42)
        self.app._last_completed_results[session.session_id] = full
        sent = []
        self.app.telegram.copy_message = lambda *_: self.fail("truncated card copied")
        self.app.telegram.send_rich_markdown = lambda _chat, text: sent.append(text) or 50
        self.app._surface_switched_session(session)
        self.assertEqual(sent, [full])

    def test_failed_clear_keeps_recovery_record(self) -> None:
        session = self.session("codex")
        self.app.sessions.set_in_flight(session.session_id, 1, "recover-me")
        self.app.codex.start_thread = lambda *_a, **_kw: (_ for _ in ()).throw(CodexBackendError("fixture"))
        self.app._clear_session(1, session)
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "recover-me")

    def test_clear_removes_old_pending_result_before_it_can_republish(self) -> None:
        session = self.session()
        view = self.app._register_turn(session, "old")
        view.status = "completed"
        self.app._clear_session(1, session)
        self.assertNotIn((session.session_id, "old"), self.app._turns)

    def test_pending_compaction_is_busy_before_turn_started_arrives(self) -> None:
        session = self.session("codex")
        self.app.codex.compact_thread = lambda *_: None
        self.app._compact_session(1, session)
        self.assertTrue(self.app._session_is_busy(session))

    def test_lost_start_response_keeps_observed_turn_and_bot_route(self) -> None:
        session = self.session("codex")

        def start(*_args, **_kwargs):
            self.app._on_codex_notification("turn/started", {
                "threadId": "thread-1", "turn": {"id": "accepted"}
            })
            raise CodexBackendError("response lost", ambiguous=True)

        self.app.codex.start_turn = start
        with self.app._bot_scope("worker"):
            self.app._start_session_turn(1, session, "once")
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "accepted")
        self.assertEqual(self.app._turns[(session.session_id, "accepted")].bot_key, "worker")
        self.assertTrue(self.app._session_is_busy(session))

    def test_model_button_keeps_its_value_when_catalog_order_changes(self) -> None:
        session = self.session()
        with patch("cli_telegram_gateway.app.list_models", return_value=["first", "chosen"]):
            callback = self.app._model_view(session)[1]["inline_keyboard"][1][0]["callback_data"]
        with patch("cli_telegram_gateway.app.list_models", return_value=["chosen", "first"]):
            self.app._handle_callback_query({
                "id": "q", "from": {"id": 1}, "data": callback,
                "message": {"message_id": 9, "chat": {"id": 1, "type": "private"}},
            })
        self.assertEqual(self.app.sessions.get(session.session_id).model, "chosen")

    def test_disconnect_during_start_does_not_restore_a_dead_turn(self) -> None:
        session = self.session("codex")

        def start(*_args, **_kwargs):
            self.app._on_codex_notification("turn/started", {
                "threadId": "thread-1", "turn": {"id": "accepted"}
            })
            self.app._on_codex_disconnect()
            raise CodexBackendError("EOF", ambiguous=True)

        self.app.codex.start_turn = start
        self.app._start_session_turn(1, session, "once")
        self.assertFalse(self.app._session_is_busy(session))
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "accepted")
        self.assertEqual(self.app._turns[(session.session_id, "accepted")].status, "interrupted")

    def test_disconnect_clears_pending_compaction_so_reload_is_possible(self) -> None:
        session = self.session("codex")
        self.app._pending_codex_compactions["thread-1"] = "worker"
        self.app._on_codex_disconnect()
        self.assertFalse(self.app._session_is_busy(session))
        self.assertFalse(self.app._pending_codex_compactions)

    def test_removed_bot_recovery_never_falls_back_to_default_entrance(self) -> None:
        session = self.session()
        self.app.sessions.set_in_flight(session.session_id, 1, "old", "removed-bot")
        with self.assertLogs("telegram-cli-gateway", level="WARNING"):
            self.app._recover_interrupted_turns()
        self.assertEqual(self.notices, [])
        self.assertEqual(self.app.sessions.get_in_flight(session.session_id), "old")


if __name__ == "__main__":
    unittest.main()
