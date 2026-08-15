from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

from cli_telegram_gateway.codex_backend import CodexAppServer
from cli_telegram_gateway.config import Config
from cli_telegram_gateway.headless_backend import HeadlessBackend
from cli_telegram_gateway.sessions import CliSession


RUN_REAL = os.environ.get("RUN_REAL_CLI_E2E") == "1"


@unittest.skipUnless(RUN_REAL, "set RUN_REAL_CLI_E2E=1 to spend a real model turn per CLI")
class RealCliEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = Config.load(Path(__file__).resolve().parents[1])
        cls.e2e_root = cls.config.runtime_dir / "e2e"
        cls.e2e_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def test_codex_real_streamed_turn(self) -> None:
        finished = threading.Event()
        answer: list[str] = []

        def notification(method: str, params: dict[str, Any]) -> None:
            if method == "item/agentMessage/delta" and isinstance(params.get("delta"), str):
                answer.append(params["delta"])
            if method == "turn/completed":
                finished.set()

        server = CodexAppServer(
            self.config.cli_commands["codex"],
            notification,
            lambda request_id, _method, _params: server.respond(
                request_id, {"decision": "acceptForSession"}
            ),
        )
        try:
            server.start()
            with tempfile.TemporaryDirectory(dir=self.e2e_root) as cwd:
                thread_id = server.start_thread(cwd, ephemeral=True)
                server.start_turn(thread_id, "Reply with exactly CODEX_E2E_OK")
                self.assertTrue(finished.wait(180), "Codex turn timed out")
            self.assertIn("CODEX_E2E_OK", "".join(answer))
        finally:
            server.close()

    def test_claude_grok_and_pi_real_streamed_turns(self) -> None:
        for cli in ("claude", "grok", "pi"):
            with self.subTest(cli=cli), tempfile.TemporaryDirectory(dir=self.e2e_root) as cwd:
                finished = threading.Event()
                answer: list[str] = []
                errors: list[str] = []

                def on_event(_session_id: str, _turn_id: str, kind: str, data: Any) -> None:
                    if kind == "delta" and isinstance(data, str):
                        answer.append(data)
                    elif kind == "error":
                        errors.append(str(data))
                        finished.set()
                    elif kind == "completed":
                        finished.set()

                backend = HeadlessBackend(self.config.cli_commands, on_event)
                session_id = f"{cli}-e2e"
                session = CliSession(
                    session_id=session_id,
                    cli=cli,
                    cwd=cwd,
                    tmux_name="",
                    log_path="",
                    chat_id=1,
                    created_at="now",
                    backend="headless-json",
                    external_id=str(uuid.uuid4()),
                )
                try:
                    backend.start_turn(session, f"Reply with exactly {cli.upper()}_E2E_OK")
                    self.assertTrue(finished.wait(180), f"{cli} turn timed out")
                    self.assertEqual(errors, [])
                    self.assertIn(f"{cli.upper()}_E2E_OK", "".join(answer))

                    deadline = time.monotonic() + 5
                    while backend.is_active(session_id) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    session.turn_count = 1
                    finished.clear()
                    answer.clear()
                    backend.start_turn(session, f"Reply with exactly {cli.upper()}_RESUME_OK")
                    self.assertTrue(finished.wait(180), f"{cli} resumed turn timed out")
                    self.assertEqual(errors, [])
                    self.assertIn(f"{cli.upper()}_RESUME_OK", "".join(answer))
                finally:
                    backend.close()


if __name__ == "__main__":
    unittest.main()
