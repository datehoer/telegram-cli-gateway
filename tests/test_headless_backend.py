from __future__ import annotations

import io
import json
import sys
import unittest
import subprocess
import threading
from pathlib import Path

from cli_telegram_gateway.attachments import Attachment
from cli_telegram_gateway.headless_backend import HeadlessBackend, HeadlessBackendError, _TurnInput
from cli_telegram_gateway.sessions import CliSession


FAKE_PI_RPC_SCRIPT = """\
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("type") == "compact":
        print(json.dumps({"type": "compaction_start", "reason": "manual"}))
        print(json.dumps({"type": "compaction_end", "reason": "manual", "aborted": False, "willRetry": False}))
        print(json.dumps({"id": "compact", "type": "response", "command": "compact", "success": True}))
        sys.exit(0)
"""


FAKE_PI_RPC_FAIL_SCRIPT = """\
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("type") == "compact":
        print(json.dumps({"type": "compaction_start", "reason": "manual"}))
        print(json.dumps({"id": "compact", "type": "response", "command": "compact", "success": False, "error": "Nothing to compact (session too small)"}))
        sys.exit(0)
"""


# Speaks Claude's stream-json protocol as observed with Claude Code 2.1.289: every
# user message gets command_lifecycle events, a message read mid-turn joins the turn
# at its next tool boundary, and a message read after the last tool call gets a
# follow-up turn and result. Like Claude it exits only at stdin EOF, so a gateway
# that never closes input hangs the test instead of passing it.
FAKE_CLAUDE_SCRIPT = """\
import json, sys
mode = sys.argv[1]
capabilities = ["msg_lifecycle_v1"]

def out(value):
    print(json.dumps(value), flush=True)

def read():
    line = sys.stdin.readline()
    return json.loads(line) if line.strip() else None

def lifecycle(message, state):
    out({"type": "command_lifecycle", "command_uuid": message["uuid"], "state": state})

def say(message_id, text):
    out({"type": "stream_event", "event": {"type": "message_start", "message": {"id": message_id}}})
    out({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}})

def result(text):
    out({"type": "result", "subtype": "success", "is_error": False, "result": text})

first = read()
lifecycle(first, "queued")
if mode == "orphans":
    # Reports background tasks orphaned by the previous process in a no-op turn.
    out({"type": "system", "subtype": "init", "capabilities": capabilities})
    result("")
out({"type": "system", "subtype": "init", "capabilities": capabilities})
lifecycle(first, "started")
prompt = first["message"]["content"]
if mode in {"tool", "orphans"}:
    out({"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "name": "Bash", "input": {"command": "sleep 1"}}}})
    steer = read()
    lifecycle(steer, "queued")
    lifecycle(steer, "started")
    answer = "answered " + prompt + " and " + steer["message"]["content"]
    say("msg_2", answer)
    lifecycle(steer, "completed")
    result(answer)
    lifecycle(first, "completed")
else:
    say("msg_1", "first answer")
    out({"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "name": "Wait", "input": {"command": "wait"}}}})
    steer = read()
    lifecycle(steer, "queued")
    result("first answer")
    lifecycle(first, "completed")
    out({"type": "system", "subtype": "init", "capabilities": capabilities})
    lifecycle(steer, "started")
    say("msg_2", "second answer")
    result("second answer")
    lifecycle(steer, "completed")
for _line in sys.stdin:
    pass
"""


class HeadlessParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = HeadlessBackend({}, lambda *_args: None)

    def test_claude_text_and_tool_events(self) -> None:
        parts: dict[int, list[str]] = {}
        text = self.backend._parse_anthropic_event(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hello"},
                },
            },
            parts,
            False,
        )
        tool = self.backend._parse_anthropic_event(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
                },
            },
            parts,
            True,
        )
        self.assertEqual(text, [("delta", "hello")])
        self.assertEqual(tool, [("command", "pwd")])

    def test_claude_result_marks_only_the_last_message_final(self) -> None:
        def text(value: str) -> dict[str, object]:
            return {"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": value},
            }}

        def start(message_id: str) -> dict[str, object]:
            return {"type": "stream_event", "event": {"type": "message_start", "message": {"id": message_id}}}

        stream = [
            start("msg_1"), text("Checking the date first."),
            start("msg_2"), text("It is October 6."),
            {"type": "result", "subtype": "success", "is_error": False, "result": "It is October 6."},
        ]

        def parse(cli: str, values: list[dict[str, object]]) -> list[tuple[str, object]]:
            events: list[tuple[str, object]] = []
            parts: dict[int, list[str]] = {}
            message: dict[str, str] = {}
            emitted = False
            for value in values:
                for kind, data in self.backend._parse_event(cli, value, parts, emitted, message):
                    events.append((kind, data))
                    emitted = emitted or kind in {"delta", "message_delta"}
            return events

        self.assertEqual(parse("claude", stream), [
            ("message_delta", {"id": "msg_1", "delta": "Checking the date first."}),
            ("message_delta", {"id": "msg_2", "delta": "It is October 6."}),
            ("message_completed", {"id": "msg_2", "phase": "final_answer", "text": "It is October 6."}),
            ("completed", None),
        ])
        # Grok's result has not been verified to repeat only its last message.
        self.assertEqual(parse("grok", stream), [
            ("delta", "Checking the date first."),
            ("delta", "It is October 6."),
            ("completed", None),
        ])
        # Without partial messages the result is still the only visible answer.
        self.assertEqual(parse("claude", stream[-1:]), [("delta", "It is October 6."), ("completed", None)])

    def test_grok_result_returns_errors_list_detail(self) -> None:
        events = self.backend._parse_anthropic_event(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "errors": [
                    "API error (status 402 Payment Required): "
                    "Grok Build usage balance exhausted"
                ],
            },
            {},
            False,
        )

        self.assertEqual(
            events,
            [
                (
                    "error",
                    "API error (status 402 Payment Required): "
                    "Grok Build usage balance exhausted",
                )
            ],
        )

    def test_anthropic_result_prefers_direct_error_detail(self) -> None:
        events = self.backend._parse_anthropic_event(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error": "authentication failed",
                "errors": ["less specific"],
            },
            {},
            False,
        )

        self.assertEqual(events, [("error", "authentication failed")])

    def test_anthropic_result_preserves_structured_error_detail(self) -> None:
        events = self.backend._parse_anthropic_event(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error": {"status": 429, "message": "rate limited"},
            },
            {},
            False,
        )

        self.assertEqual(events, [("error", '{"status":429,"message":"rate limited"}')])

    def test_pi_stream_events(self) -> None:
        self.assertEqual(
            self.backend._parse_pi_event(
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "hi"}}
            ),
            [("delta", "hi")],
        )
        self.assertEqual(
            self.backend._parse_pi_event({"type": "tool_execution_start", "toolName": "bash", "args": {"command": "ls"}}),
            [("command", "ls")],
        )
        self.assertEqual(
            self.backend._parse_pi_event(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "final"}],
                        "stopReason": "stop",
                    },
                }
            ),
            [("delta", "final"), ("completed", None)],
        )
        self.assertEqual(
            self.backend._parse_pi_event(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": "quota exceeded",
                    },
                }
            ),
            [("error", "quota exceeded")],
        )
        self.assertEqual(
            self.backend._parse_pi_event(
                {
                    "type": "message_end",
                    "message": {"role": "assistant", "content": [], "stopReason": "length"},
                }
            ),
            [("error", "Pi stopped before completing the answer because the model output limit was reached")],
        )
        self.assertEqual(
            self.backend._parse_pi_event({"type": "agent_end", "willRetry": True}),
            [("retrying", None)],
        )
        self.assertEqual(self.backend._parse_pi_event({"type": "agent_end"}), [("completed", None)])

    def test_pi_message_error_is_not_overwritten_by_agent_end_or_exit_zero(self) -> None:
        events: list[tuple[str, object]] = []
        backend = HeadlessBackend({}, lambda _sid, _tid, kind, data: events.append((kind, data)))
        script = """
import json
print(json.dumps({"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"quota exceeded"}}))
print(json.dumps({"type":"agent_end","willRetry":False}))
"""
        process = subprocess.Popen(
            ["python3", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        session = CliSession("pi-test", "pi", "/tmp", "", "", 1, "now", "headless-json", "id")

        backend._read_process(session, "turn", process)

        self.assertEqual(events, [("error", "quota exceeded")])

    def test_pi_native_retry_can_replace_a_temporary_error(self) -> None:
        events: list[tuple[str, object]] = []
        backend = HeadlessBackend({}, lambda _sid, _tid, kind, data: events.append((kind, data)))
        script = """
import json
values = [
    {"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"temporary"}},
    {"type":"agent_end","willRetry":True},
    {"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"done"}},
    {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"stopReason":"stop"}},
    {"type":"agent_end","willRetry":False},
]
for value in values:
    print(json.dumps(value))
"""
        process = subprocess.Popen(
            ["python3", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        session = CliSession("pi-test", "pi", "/tmp", "", "", 1, "now", "headless-json", "id")

        backend._read_process(session, "turn", process)

        self.assertEqual(events, [("delta", "done"), ("completed", None)])

    def test_process_exit_always_emits_one_terminal_event(self) -> None:
        events: list[tuple[str, object]] = []
        done = threading.Event()
        backend = HeadlessBackend(
            {},
            lambda _sid, _tid, kind, data: (events.append((kind, data)), done.set())
            if kind in {"completed", "error"}
            else events.append((kind, data)),
        )
        process = subprocess.Popen(
            ["python3", "-c", "print('{\"type\":\"agent_end\"}')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        session = CliSession("pi-test", "pi", "/tmp", "", "", 1, "now", "headless-json", "id")
        backend._read_process(session, "turn", process)
        self.assertTrue(done.is_set())
        self.assertEqual(events, [("completed", None)])

    def test_killed_process_emits_interrupted_not_error(self) -> None:
        """A process killed by a signal (gateway restart or shutdown, OOM, ...) reports
        interrupted, not error, so the caller keeps a recovery candidate instead of a failure."""
        events: list[tuple[str, object]] = []
        backend = HeadlessBackend({}, lambda _sid, _tid, kind, data: events.append((kind, data)))
        process = subprocess.Popen(
            ["python3", "-c", "import os; os.kill(os.getpid(), 15)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        session = CliSession("pi-test", "pi", "/tmp", "", "", 1, "now", "headless-json", "id")
        backend._read_process(session, "turn", process)
        self.assertEqual(events, [("interrupted", -15)])

    def test_abnormal_exit_without_error_event_is_interrupted(self) -> None:
        """A nonzero exit without an explicit error event on stdout is also a recoverable
        interruption: some CLIs map external signals to positive exit codes (Pi uses 143),
        so the sign of returncode alone is not enough."""
        events: list[tuple[str, object]] = []
        backend = HeadlessBackend({}, lambda _sid, _tid, kind, data: events.append((kind, data)))
        process = subprocess.Popen(
            ["python3", "-c", "import sys; sys.exit(3)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        session = CliSession("pi-test", "pi", "/tmp", "", "", 1, "now", "headless-json", "id")
        backend._read_process(session, "turn", process)
        self.assertEqual([kind for kind, _data in events], ["interrupted"])

    def test_stdout_error_event_still_emits_error(self) -> None:
        """An explicit error on stdout (authentication failure, model error) still takes the error path."""
        events: list[tuple[str, object]] = []
        backend = HeadlessBackend({}, lambda _sid, _tid, kind, data: events.append((kind, data)))
        process = subprocess.Popen(
            ["python3", "-c", "import json; print(json.dumps({'type':'result','is_error':True,'result':'boom'}))"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        session = CliSession("claude-test", "claude", "/tmp", "", "", 1, "now", "headless-json", "id")
        backend._read_process(session, "turn", process)
        self.assertEqual([kind for kind, _data in events], ["error"])

    def test_compact_session_emits_compact_command_and_returns_summary(self) -> None:
        """Pi RPC compact: send the compact JSON command and return a summary on success."""
        events: list[tuple[str, object]] = []
        backend = HeadlessBackend(
            {"pi": ("python3", "-c", FAKE_PI_RPC_SCRIPT)},
            lambda _sid, _tid, kind, data: events.append((kind, data)),
        )
        session = CliSession("pi-compact", "pi", "/tmp", "", "", 1, "now", "headless-json", "sess-1")
        result = backend.compact_session(session)
        self.assertEqual(result, "Context compaction started.")
        # Compression invalidates the old occupancy without creating an answer turn.
        self.assertEqual(events, [("usage", {"context_tokens": None, "external_id": "sess-1"})])

    def test_compact_session_reports_failure(self) -> None:
        """A failed Pi RPC compact (nothing to compact, ...) returns an error summary instead of raising."""
        events: list[tuple[str, object]] = []
        backend = HeadlessBackend(
            {"pi": ("python3", "-c", FAKE_PI_RPC_FAIL_SCRIPT)},
            lambda _sid, _tid, kind, data: events.append((kind, data)),
        )
        session = CliSession("pi-compact-fail", "pi", "/tmp", "", "", 1, "now", "headless-json", "sess-1")
        result = backend.compact_session(session)
        self.assertEqual(result, "Compaction did not run: Nothing to compact (session too small)")
        self.assertEqual([kind for kind, _data in events], [])

    def test_build_command_includes_model_and_effort_flags(self) -> None:
        backend = HeadlessBackend(
            {"claude": ("claude",), "grok": ("grok",), "pi": ("pi",)},
            lambda *_args: None,
        )
        claude = CliSession("c1", "claude", "/tmp", "", "", 1, "now", "headless-json", "id", model="sonnet", effort="high")
        args = backend._build_command(claude, "hi", ())
        self.assertIn(("--model", "sonnet"), zip(args, args[1:]))
        self.assertIn(("--effort", "high"), zip(args, args[1:]))
        # The prompt goes through stdin so the turn can take messages sent mid-turn.
        self.assertEqual(args[args.index("--input-format") + 1], "stream-json")
        self.assertNotIn("hi", args)

        grok = CliSession("g1", "grok", "/tmp", "", "", 1, "now", "headless-json", "id", model="grok-4.5", effort="xhigh")
        args = backend._build_command(grok, "hi", ())
        self.assertIn(("--model", "grok-4.5"), zip(args, args[1:]))
        self.assertIn(("--reasoning-effort", "xhigh"), zip(args, args[1:]))

        pi = CliSession("p1", "pi", "/tmp", "", "", 1, "now", "headless-json", "id", model="hotday-deepseek/deepseek-v4-flash", effort="low")
        args = backend._build_command(pi, "hi", ())
        self.assertIn(("--model", "hotday-deepseek/deepseek-v4-flash"), zip(args, args[1:]))
        self.assertIn(("--thinking", "low"), zip(args, args[1:]))

    def test_pi_attaches_images_and_lists_other_files(self) -> None:
        """Pi inlines a non-image @file as text, so binaries such as WAV only get paths."""
        backend = HeadlessBackend({"pi": ("pi",)}, lambda *_args: None)
        pi = CliSession("p1", "pi", "/tmp", "", "", 1, "now", "headless-json", "id")
        photo = Attachment(Path("/up/photo.jpg"), "photo.jpg", "image/jpeg", True)
        heic = Attachment(Path("/up/shot.heic"), "shot.heic", "image/heic", True)
        wav = Attachment(Path("/up/take.wav"), "take.wav", "audio/x-wav", False)
        args = backend._build_command(pi, "listen to this", (photo, heic, wav))
        self.assertEqual([arg for arg in args if arg.startswith("@")], ["@/up/photo.jpg"])
        prompt = args[-1]
        self.assertTrue(prompt.startswith("listen to this\n\n"))
        self.assertIn("- shot.heic: /up/shot.heic", prompt)
        self.assertIn("- take.wav: /up/take.wav", prompt)
        self.assertNotIn("photo.jpg", prompt)
        self.assertEqual(backend._build_command(pi, "hi", (photo,))[-1], "hi")

    def test_build_command_omits_unset_model_and_effort(self) -> None:
        backend = HeadlessBackend(
            {"claude": ("claude",), "pi": ("pi",)},
            lambda *_args: None,
        )
        claude = CliSession("c1", "claude", "/tmp", "", "", 1, "now", "headless-json", "id")
        args = backend._build_command(claude, "hi", ())
        self.assertNotIn("--model", args)
        self.assertNotIn("--effort", args)
        pi = CliSession("p1", "pi", "/tmp", "", "", 1, "now", "headless-json", "id")
        args = backend._build_command(pi, "hi", ())
        self.assertNotIn("--model", args)
        self.assertNotIn("--thinking", args)


        backend = HeadlessBackend(
            {"pi": ("python3", "-c", "pass"), "claude": ("claude",)},
            lambda *_args: None,
        )
        claude = CliSession("claude-c", "claude", "/tmp", "", "", 1, "now", "headless-json", "sess-1")
        with self.assertRaises(HeadlessBackendError):
            backend.compact_session(claude)

        # Refused while the session is busy (a process is already running)
        busy = CliSession("pi-busy", "pi", "/tmp", "", "", 1, "now", "headless-json", "sess-1")
        probe = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)"])
        try:
            backend._active[busy.session_id] = probe
            with self.assertRaises(HeadlessBackendError):
                backend.compact_session(busy)
        finally:
            probe.kill()
            probe.wait()


class ClaudeTurnInputTests(unittest.TestCase):
    def make_input(self) -> tuple[_TurnInput, io.StringIO]:
        stream = io.StringIO()
        return _TurnInput(stream), stream

    def test_input_closes_after_a_result_leaves_no_message_waiting(self) -> None:
        turn_input, stream = self.make_input()
        prompt = turn_input.send("hello")
        assert prompt is not None
        self.assertEqual(json.loads(stream.getvalue()), {
            "type": "user", "uuid": prompt, "message": {"role": "user", "content": "hello"},
        })
        turn_input.on_lifecycle(prompt, "queued")
        turn_input.on_init(["msg_lifecycle_v1"])
        turn_input.on_lifecycle(prompt, "started")
        steer = turn_input.send("also this", steer=True)
        assert steer is not None
        turn_input.on_lifecycle(steer, "queued")
        self.assertTrue(turn_input.wait_accepted(steer, 0))
        # The steer arrived after the last tool call, so Claude answers it in a
        # follow-up turn of the same process: input must stay open meanwhile.
        turn_input.on_result()
        self.assertFalse(stream.closed)
        turn_input.on_lifecycle(prompt, "completed")
        turn_input.on_lifecycle(steer, "started")
        self.assertFalse(stream.closed)
        turn_input.on_result()
        self.assertTrue(stream.closed)
        self.assertIsNone(turn_input.send("too late", steer=True))

    def test_an_orphaned_task_result_before_the_prompt_starts_keeps_input_open(self) -> None:
        turn_input, stream = self.make_input()
        prompt = turn_input.send("hello")
        assert prompt is not None
        turn_input.on_lifecycle(prompt, "queued")
        turn_input.on_init(["msg_lifecycle_v1"])
        turn_input.on_result()
        self.assertFalse(stream.closed)
        turn_input.on_lifecycle(prompt, "started")
        self.assertFalse(stream.closed)
        self.assertIsNotNone(turn_input.send("mid-turn", steer=True))

    def test_a_cancelled_message_releases_input_after_an_error_result(self) -> None:
        turn_input, stream = self.make_input()
        prompt = turn_input.send("hello")
        assert prompt is not None
        turn_input.on_init(["msg_lifecycle_v1"])
        turn_input.on_lifecycle(prompt, "queued")
        turn_input.on_result()
        self.assertFalse(stream.closed)
        turn_input.on_lifecycle(prompt, "cancelled")
        self.assertTrue(stream.closed)

    def test_without_lifecycle_events_input_closes_at_the_first_result_and_refuses_steers(self) -> None:
        turn_input, stream = self.make_input()
        self.assertIsNotNone(turn_input.send("hello"))
        turn_input.on_init(["interrupt_receipt_v1"])
        self.assertIsNone(turn_input.send("steer", steer=True))
        turn_input.on_result()
        self.assertTrue(stream.closed)

    def test_wait_returns_when_the_process_ends_without_a_receipt(self) -> None:
        turn_input, _stream = self.make_input()
        turn_input.on_init(["msg_lifecycle_v1"])
        steer = turn_input.send("steer", steer=True)
        assert steer is not None
        closer = threading.Timer(0.05, turn_input.close)
        closer.start()
        try:
            self.assertFalse(turn_input.wait_accepted(steer, 5))
        finally:
            closer.join()

    def test_a_broken_pipe_reports_the_message_unsent(self) -> None:
        class BrokenStream(io.StringIO):
            def write(self, _text: str) -> int:
                raise BrokenPipeError("claude exited")

        turn_input = _TurnInput(BrokenStream())
        self.assertIsNone(turn_input.send("hello"))
        self.assertIsNone(turn_input.send("again", steer=True))


class ClaudeSteeringProcessTests(unittest.TestCase):
    def run_turn(self, mode: str) -> tuple[list[tuple[str, object]], bool]:
        events: list[tuple[str, object]] = []
        tool_started = threading.Event()
        finished = threading.Event()

        def on_event(_session_id: str, _turn_id: str, kind: str, data: object) -> None:
            events.append((kind, data))
            if kind == "command":
                tool_started.set()
            if kind in {"completed", "error", "interrupted"}:
                finished.set()

        backend = HeadlessBackend(
            {"claude": (sys.executable, "-c", FAKE_CLAUDE_SCRIPT, mode)}, on_event,
        )
        session = CliSession("claude-steer", "claude", "/tmp", "", "", 1, "now", "headless-json", "sess-1")
        try:
            backend.start_turn(session, "the prompt")
            self.assertTrue(tool_started.wait(10), "the fake Claude never reached its tool call")
            confirm = backend.steer_turn(session.session_id, "the steer")
            self.assertIsNotNone(confirm)
            assert confirm is not None
            accepted = confirm(10)
            self.assertTrue(finished.wait(10), "the Claude process never exited")
        finally:
            backend.close()
        self.assertFalse(backend.is_active(session.session_id))
        self.assertIsNone(backend.steer_turn(session.session_id, "after the turn"))
        return events, accepted

    def test_mid_turn_steer_joins_the_running_turn(self) -> None:
        events, accepted = self.run_turn("tool")
        self.assertTrue(accepted)
        self.assertEqual(events[-2:], [
            ("message_completed", {
                "id": "msg_2", "phase": "final_answer", "text": "answered the prompt and the steer",
            }),
            ("completed", None),
        ])

    def test_steer_after_the_last_tool_call_gets_a_follow_up_answer(self) -> None:
        events, accepted = self.run_turn("late")
        self.assertTrue(accepted)
        finals = [data for kind, data in events if kind == "message_completed"]
        self.assertEqual(finals, [
            {"id": "msg_1", "phase": "final_answer", "text": "first answer"},
            {"id": "msg_2", "phase": "final_answer", "text": "second answer"},
        ])
        # Final messages wait for the process to exit, so the first answer is not
        # sealed as the turn's answer while the follow-up is still streaming.
        kinds = [kind for kind, _data in events]
        self.assertLess(kinds.index("message_delta"), kinds.index("message_completed"))
        self.assertGreater(
            kinds.index("message_completed"),
            max(index for index, kind in enumerate(kinds) if kind == "message_delta"),
        )
        self.assertEqual(kinds[-1], "completed")

    def test_orphaned_task_result_does_not_end_steering(self) -> None:
        events, accepted = self.run_turn("orphans")
        self.assertTrue(accepted)
        self.assertEqual(events[-1], ("completed", None))


if __name__ == "__main__":
    unittest.main()
