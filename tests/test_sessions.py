from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cli_telegram_gateway.config import Config
from cli_telegram_gateway.sessions import SessionManager


class SessionStateTests(unittest.TestCase):
    def make_manager(self, project: Path) -> SessionManager:
        return SessionManager(
            Config(
                project_dir=project,
                bot_token="test",
                allowed_user_ids=frozenset({1}),
                allow_groups=False,
                allowed_roots=(project,),
                default_workdir=project,
                cli_commands={"pi": ("pi",)},
                poll_timeout=1,
            )
        )

    def test_message_routes_names_and_archives_persist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            session = manager.create_headless("pi", project, 1)
            manager.rename(session.session_id, "研究助手")
            manager.bind_message(1, 99, session.session_id)
            reloaded = self.make_manager(project)
            self.assertEqual(reloaded.session_for_message(1, 99).label, "研究助手")  # type: ignore[union-attr]
            reloaded.set_archived(session.session_id, True)
            self.assertIsNone(reloaded.session_for_message(1, 99))
            self.assertEqual(reloaded.list_for_chat(1), [])
            self.assertEqual(len(reloaded.list_for_chat(1, include_archived=True)), 1)

    def test_bot_entrances_share_sessions_but_isolate_navigation_and_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            first = manager.create_headless("pi", project, 1)
            second = manager.create_headless("pi", project, 1)

            manager.switch(1, first.session_id, "worker")
            self.assertEqual(manager.current(1).session_id, second.session_id)  # type: ignore[union-attr]
            self.assertEqual(manager.current(1, "worker").session_id, first.session_id)  # type: ignore[union-attr]

            manager.bind_message(1, 99, second.session_id)
            manager.bind_message(1, 99, first.session_id, "worker")
            self.assertEqual(manager.session_for_message(1, 99).session_id, second.session_id)  # type: ignore[union-attr]
            self.assertEqual(manager.session_for_message(1, 99, "worker").session_id, first.session_id)  # type: ignore[union-attr]

            manager.set_telegram_offset(10)
            manager.set_telegram_offset(20, "worker")
            self.assertEqual(manager.get_telegram_offset(), 10)
            self.assertEqual(manager.get_telegram_offset("worker"), 20)

    def test_in_flight_turn_remembers_origin_bot_for_restart_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            session = manager.create_headless("pi", project, 1)
            manager.set_in_flight(session.session_id, 1, "turn-1", "worker")
            self.assertEqual(
                manager.stale_in_flight_routes(),
                [(session.session_id, 1, "turn-1", "worker")],
            )

    def test_model_and_effort_persist_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            session = manager.create_headless("pi", project, 1)
            self.assertIsNone(session.model)
            self.assertIsNone(session.effort)
            manager.set_model(session.session_id, "gpt-5.5")
            manager.set_effort(session.session_id, "low")
            reloaded = self.make_manager(project)
            restored = reloaded.get(session.session_id)
            self.assertEqual(restored.model, "gpt-5.5")  # type: ignore[union-attr]
            self.assertEqual(restored.effort, "low")  # type: ignore[union-attr]
            reloaded.set_model(session.session_id, None)
            reloaded.set_effort(session.session_id, None)
            cleared = reloaded.get(session.session_id)
            self.assertIsNone(cleared.model)  # type: ignore[union-attr]
            self.assertIsNone(cleared.effort)  # type: ignore[union-attr]

    def test_last_completed_message_pointer_persists_and_clears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            session = manager.create_headless("pi", project, 1)
            manager.set_last_completed_message(session.session_id, 88)

            reloaded = self.make_manager(project)
            restored = reloaded.get(session.session_id)
            self.assertEqual(restored.last_completed_message_id, 88)  # type: ignore[union-attr]

            reloaded.set_last_completed_message(session.session_id, None)
            cleared = self.make_manager(project).get(session.session_id)
            self.assertIsNone(cleared.last_completed_message_id)  # type: ignore[union-attr]

    def test_legacy_idle_session_uses_latest_bound_message_as_initial_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            session = manager.create_headless("pi", project, 1)
            manager.bind_message(1, 88, session.session_id)
            manager.bind_message(1, 99, session.session_id)
            manager._state["sessions"][session.session_id].pop(  # type: ignore[attr-defined]
                "last_completed_message_id"
            )
            manager._save_state()  # type: ignore[attr-defined]

            restored = self.make_manager(project).get(session.session_id)
            self.assertEqual(restored.last_completed_message_id, 99)  # type: ignore[union-attr]

    def test_legacy_in_flight_session_does_not_infer_completed_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            session = manager.create_headless("pi", project, 1)
            manager.bind_message(1, 88, session.session_id)
            manager.set_in_flight(session.session_id, 1, "turn-running")
            manager._state["sessions"][session.session_id].pop(  # type: ignore[attr-defined]
                "last_completed_message_id"
            )
            manager._save_state()  # type: ignore[attr-defined]

            restored = self.make_manager(project).get(session.session_id)
            self.assertIsNone(restored.last_completed_message_id)  # type: ignore[union-attr]


class UsagePersistenceTests(unittest.TestCase):
    make_manager = SessionStateTests.make_manager

    def stored_usage(self, project: Path, session_id: str) -> dict:
        state = json.loads((project / ".runtime" / "state.json").read_text(encoding="utf-8"))
        return state["sessions"][session_id]["usage"] or {}

    def test_usage_refresh_stays_in_memory_until_the_next_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            session = manager.create_headless("pi", project, 1)
            manager.update_usage(
                session.session_id, session.external_id, {"context_tokens": 1200}, deferred=True,
            )
            self.assertEqual(manager.get(session.session_id).usage["context_tokens"], 1200)  # type: ignore[union-attr,index]
            self.assertEqual(self.stored_usage(project, session.session_id), {})
            manager.set_in_flight(session.session_id, 1, "turn-1")
            self.assertEqual(self.stored_usage(project, session.session_id)["context_tokens"], 1200)

    def test_flush_writes_pending_usage_and_is_a_no_op_when_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            session = manager.create_headless("pi", project, 1)
            manager.update_usage(
                session.session_id, session.external_id, {"context_tokens": 900}, deferred=True,
            )
            manager.flush()
            self.assertEqual(self.stored_usage(project, session.session_id)["context_tokens"], 900)
            state_path = project / ".runtime" / "state.json"
            written = state_path.stat().st_mtime_ns
            manager.flush()
            self.assertEqual(state_path.stat().st_mtime_ns, written)

    def test_explicit_usage_updates_are_written_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            session = manager.create_headless("pi", project, 1)
            manager.update_usage(session.session_id, session.external_id, {"context_tokens": 700})
            self.assertEqual(self.stored_usage(project, session.session_id)["context_tokens"], 700)

    def test_compaction_invalidation_is_written_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manager = self.make_manager(project)
            session = manager.create_headless("pi", project, 1)
            manager.update_usage(session.session_id, session.external_id, {"context_tokens": 5000})
            manager.update_usage(
                session.session_id, session.external_id, {"context_tokens": None}, deferred=True,
            )
            self.assertIsNone(self.stored_usage(project, session.session_id)["context_tokens"])


if __name__ == "__main__":
    unittest.main()
