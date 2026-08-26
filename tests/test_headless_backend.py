from __future__ import annotations

import unittest
import subprocess
import threading

from cli_telegram_gateway.headless_backend import HeadlessBackend, HeadlessBackendError
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
        """进程被信号杀死（网关重启/关机连累、OOM 等）时上报 interrupted，
        而不是 error：上层据此保留恢复候选而不是标记失败。"""
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
        """非零退出且 stdout 没有明确 error 事件时也按可恢复的中断处理：
        有些 CLI 把外部信号转成正的退出码（如 pi 的 143），不能只凭
        returncode 符号判断。"""
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
        """CLI 在 stdout 明确报告 error（如认证失败、模型错误）仍走 error 分支。"""
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
        """Pi 的 RPC compact：发 compact JSON 命令，收到成功响应后返回摘要。"""
        events: list[tuple[str, object]] = []
        backend = HeadlessBackend(
            {"pi": ("python3", "-c", FAKE_PI_RPC_SCRIPT)},
            lambda _sid, _tid, kind, data: events.append((kind, data)),
        )
        session = CliSession("pi-compact", "pi", "/tmp", "", "", 1, "now", "headless-json", "sess-1")
        result = backend.compact_session(session)
        self.assertEqual(result, "已触发上下文压缩。")
        # RPC compact 只发 JSON 命令，不走事件流，因此不产生任何 CLI 事件。
        self.assertEqual([kind for kind, _data in events], [])

    def test_compact_session_reports_failure(self) -> None:
        """Pi RPC compact 失败（无可压缩内容等）返回错误摘要而不是抛异常。"""
        events: list[tuple[str, object]] = []
        backend = HeadlessBackend(
            {"pi": ("python3", "-c", FAKE_PI_RPC_FAIL_SCRIPT)},
            lambda _sid, _tid, kind, data: events.append((kind, data)),
        )
        session = CliSession("pi-compact-fail", "pi", "/tmp", "", "", 1, "now", "headless-json", "sess-1")
        result = backend.compact_session(session)
        self.assertEqual(result, "压缩未执行：Nothing to compact (session too small)")
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

        grok = CliSession("g1", "grok", "/tmp", "", "", 1, "now", "headless-json", "id", model="grok-4.5", effort="xhigh")
        args = backend._build_command(grok, "hi", ())
        self.assertIn(("--model", "grok-4.5"), zip(args, args[1:]))
        self.assertIn(("--reasoning-effort", "xhigh"), zip(args, args[1:]))

        pi = CliSession("p1", "pi", "/tmp", "", "", 1, "now", "headless-json", "id", model="hotday-deepseek/deepseek-v4-flash", effort="low")
        args = backend._build_command(pi, "hi", ())
        self.assertIn(("--model", "hotday-deepseek/deepseek-v4-flash"), zip(args, args[1:]))
        self.assertIn(("--thinking", "low"), zip(args, args[1:]))

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

        # 会话忙碌时拒绝（前面有个运行中的进程）
        busy = CliSession("pi-busy", "pi", "/tmp", "", "", 1, "now", "headless-json", "sess-1")
        probe = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)"])
        try:
            backend._active[busy.session_id] = probe
            with self.assertRaises(HeadlessBackendError):
                backend.compact_session(busy)
        finally:
            probe.kill()
            probe.wait()


if __name__ == "__main__":
    unittest.main()
