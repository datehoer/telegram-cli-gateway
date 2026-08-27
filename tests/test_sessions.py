from __future__ import annotations

import secrets
import tempfile
import time
import unittest
from pathlib import Path

from cli_telegram_gateway.config import Config
from cli_telegram_gateway.sessions import SessionManager, clean_terminal_output


class TerminalOutputTests(unittest.TestCase):
    def test_removes_ansi_and_applies_backspace(self) -> None:
        raw = b"\x1b[31mred\x1b[0m ab\bcd\r\n"
        self.assertEqual(clean_terminal_output(raw), "red acd")


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
                output_poll_interval=0.1,
                output_max_bytes=65536,
                tmux_socket_name="unused",
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


class TmuxIntegrationTests(unittest.TestCase):
    def test_create_send_read_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            socket_name = f"tcg-test-{secrets.token_hex(4)}"
            config = Config(
                project_dir=project,
                bot_token="test",
                allowed_user_ids=frozenset({1}),
                allow_groups=False,
                allowed_roots=(project,),
                default_workdir=project,
                cli_commands={"sh": ("/bin/sh", "-i")},
                poll_timeout=1,
                output_poll_interval=0.1,
                output_max_bytes=65536,
                tmux_socket_name=socket_name,
            )
            manager = SessionManager(config)
            session = manager.create("sh", project, 1)
            try:
                self.assertTrue(manager.is_alive(session))
                time.sleep(0.2)
                manager.read_new_output(session)
                manager.send_text(session, "printf 'gateway-test-output\\n'")

                output = ""
                deadline = time.monotonic() + 4
                while "gateway-test-output" not in output and time.monotonic() < deadline:
                    time.sleep(0.1)
                    output += manager.read_new_output(session)
                self.assertIn("gateway-test-output", output)
            finally:
                manager.stop(session)
            self.assertFalse(manager.is_alive(session))


if __name__ == "__main__":
    unittest.main()
