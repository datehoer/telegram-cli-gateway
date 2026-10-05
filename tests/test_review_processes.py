from __future__ import annotations

import io
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from cli_telegram_gateway.codex_backend import CodexAppServer, CodexBackendError
from cli_telegram_gateway.headless_backend import HeadlessBackend, HeadlessBackendError
from cli_telegram_gateway.sessions import CliSession


class ProcessRegressionTests(unittest.TestCase):
    def test_failed_initialization_reaps_the_app_server(self) -> None:
        script = (
            "import sys,json,time; msg=json.loads(sys.stdin.readline()); "
            "print(json.dumps({'id':msg['id'],'error':{'message':'fixture'}}),flush=True); "
            "time.sleep(30)"
        )
        server = CodexAppServer((sys.executable, "-c", script), lambda *_: None, lambda *_: None)
        with self.assertRaises(CodexBackendError):
            server.start()
        self.assertIsNone(server.process)

    def test_reader_survives_malformed_events_and_callback_failure(self) -> None:
        received = []

        def handler(method, _params):
            received.append(method)
            if method == "bad-handler":
                raise ValueError("fixture failure")

        server = CodexAppServer(("unused",), handler, lambda *_: None)
        response = queue.Queue(maxsize=1)
        server._pending[7] = response
        messages = [[], None, {"method": "bad-handler"}, {"id": []},
                    {"id": 7, "result": "ok"}, {"id": 7, "result": "duplicate"},
                    {"method": "after"}]
        server.process = SimpleNamespace(stdout=io.StringIO(
            "\n".join(json.dumps(item) for item in messages)
        ))
        server._closing.set()
        with self.assertLogs("telegram-cli-gateway.codex", level="ERROR"):
            server._reader_loop()
        self.assertEqual(received, ["bad-handler", "after"])
        self.assertEqual(response.get_nowait()["result"], "ok")

    def test_eof_releases_pending_requests_and_reports_disconnect(self) -> None:
        disconnected = []
        server = CodexAppServer(("unused",), lambda *_: None, lambda *_: None,
                                lambda: disconnected.append(True))
        response = queue.Queue(maxsize=1)
        server._pending[1] = response
        server.process = SimpleNamespace(stdout=io.StringIO(""))
        with self.assertLogs("telegram-cli-gateway.codex", level="WARNING"):
            server._reader_loop()
        self.assertIn("error", response.get_nowait())
        self.assertEqual(disconnected, [True])

    def test_compaction_timeout_bounds_silent_rpc_and_drains_stderr(self) -> None:
        # Previously readline() had no timeout and stderr was never drained.
        script = "import sys,time; sys.stderr.write('x'*200000); sys.stderr.flush(); time.sleep(30)"
        backend = HeadlessBackend({"pi": (sys.executable, "-c", script)}, lambda *_: None)
        session = CliSession("pi-compact", "pi", "/tmp", "", "", 1, "now", "headless-json", "id")
        started = time.monotonic()
        with self.assertRaisesRegex(HeadlessBackendError, "超时"):
            backend.compact_session(session, timeout=0.2)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(backend.is_active(session.session_id))

    def test_interrupt_terminates_tool_children_and_emits_one_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready"
            stopped = Path(temporary) / "child-stopped"
            child = (
                "import pathlib,signal,time; "
                f"signal.signal(signal.SIGTERM, lambda *_: (pathlib.Path({str(stopped)!r}).touch(), exit(0))); "
                f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,signal; "
                "signal.signal(signal.SIGTERM, lambda *_: None); "
                f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); p.wait()"
            )
            finished = threading.Event()
            terminal = []

            def on_event(_sid, _tid, kind, _data):
                if kind in {"completed", "error", "interrupted"}:
                    terminal.append(kind)
                    finished.set()

            backend = HeadlessBackend({"pi": (sys.executable, "-c", parent)}, on_event)
            session = CliSession("pi-tree", "pi", temporary, "", "", 1, "now", "headless-json", "id")
            try:
                backend.start_turn(session, "unused")
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                self.assertTrue(backend.interrupt(session.session_id))
                self.assertTrue(finished.wait(3))
                self.assertTrue(stopped.exists())
                self.assertEqual(terminal, ["interrupted"])
            finally:
                backend.close()

    def test_headless_ignores_non_object_json(self) -> None:
        events = []
        backend = HeadlessBackend({}, lambda _s, _t, kind, _d: events.append(kind))
        process = subprocess.Popen(
            [sys.executable, "-c", "print('[]'); print('null'); print('{\"type\":\"agent_end\"}')"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        session = CliSession("pi-json", "pi", "/tmp", "", "", 1, "now", "headless-json", "id")
        backend._read_process(session, "turn", process)
        self.assertEqual(events, ["completed"])

    def test_tool_json_does_not_leak_between_blocks_with_reused_index(self) -> None:
        backend = HeadlessBackend({}, lambda *_: None)
        parts = {}
        commands = []
        for command in ("first", "second"):
            backend._parse_event("claude", {"type": "stream_event", "event": {
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "name": "Bash"},
            }}, parts, False)
            backend._parse_event("claude", {"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps({"command": command})},
            }}, parts, False)
            events = backend._parse_event("claude", {"type": "stream_event", "event": {
                "type": "content_block_stop", "index": 0,
            }}, parts, False)
            commands.extend(events)
        self.assertEqual(commands, [("command", "first"), ("command", "second")])

    def test_tool_json_is_parsed_once_when_its_block_closes(self) -> None:
        backend = HeadlessBackend({}, lambda *_: None)
        parts: dict[int, list[str]] = {}
        payload = json.dumps({"command": "echo " + "x" * 5000})
        fragments = [payload[offset:offset + 7] for offset in range(0, len(payload), 7)]
        parsed: list[str] = []
        original = backend._command_from_tool_json

        def counting(raw: str) -> str | None:
            parsed.append(raw)
            return original(raw)

        backend._command_from_tool_json = counting  # type: ignore[method-assign]
        streamed = []
        for fragment in fragments:
            streamed.extend(backend._parse_event("claude", {"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": fragment},
            }}, parts, False))
        self.assertEqual(streamed, [])
        closed = backend._parse_event("claude", {"type": "stream_event", "event": {
            "type": "content_block_stop", "index": 2,
        }}, parts, False)
        self.assertEqual(closed, [("command", "echo " + "x" * 5000)])
        self.assertEqual(parsed, [payload])
        self.assertEqual(parts, {})
