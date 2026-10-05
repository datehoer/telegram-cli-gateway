from __future__ import annotations

import tempfile
import time
import unittest
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from cli_telegram_gateway.app import BOT_COMMANDS, HELP_TEXT, GatewayApp, PendingInput, STATE_WORKING
from cli_telegram_gateway.codex_backend import CodexAppServer, CodexBackendError
from cli_telegram_gateway.config import Config
from cli_telegram_gateway.headless_backend import HeadlessBackend, HeadlessBackendError
from cli_telegram_gateway.sessions import SessionManager
from cli_telegram_gateway.usage import codex_usage, context_lines, headless_usage, merge_usage, quota_lines


CODEX_USAGE = {
    "last": {"totalTokens": 2000, "inputTokens": 1600, "outputTokens": 400, "cachedInputTokens": 1000},
    "total": {"totalTokens": 900000},
    "modelContextWindow": 10000,
}


class UsageParsingTests(unittest.TestCase):
    def test_codex_context_uses_latest_request_not_accumulated_billing(self) -> None:
        snapshot = codex_usage(CODEX_USAGE)
        text = "\n".join(context_lines(snapshot))
        self.assertIn("2,000 / 10,000", text)
        self.assertIn("20.0%", text)
        self.assertIn("8,000", text)
        self.assertNotIn("900,000", text)
        self.assertEqual(snapshot["total_tokens"], 900000)

    def test_missing_and_invalid_counts_are_unknown_not_zero(self) -> None:
        self.assertEqual(codex_usage(None), {})
        snapshot = codex_usage({"last": {"totalTokens": True}, "modelContextWindow": -1})
        self.assertEqual(snapshot, {})
        self.assertIn("等待 CLI", "\n".join(context_lines(snapshot)))
        self.assertNotIn("0.0%", "\n".join(context_lines(snapshot)))

    def test_anthropic_cached_input_counts_toward_context_and_result_does_not(self) -> None:
        for cli in ("claude", "grok"):
            with self.subTest(cli=cli):
                snapshot = headless_usage(cli, {
                    "type": "assistant", "message": {
                        "model": "model-a", "usage": {
                            "input_tokens": 200, "cache_read_input_tokens": 1000,
                            "cache_creation_input_tokens": 300, "output_tokens": 1,
                        },
                    },
                })
                self.assertEqual(snapshot["context_tokens"], 1500)
                update = headless_usage(cli, {
                    "type": "result", "usage": {"input_tokens": 20000, "output_tokens": 4000},
                    "modelUsage": {"model-a": {"contextWindow": 10000}, "subagent-model": {"contextWindow": 500}},
                })
                snapshot = merge_usage(snapshot, update)
                self.assertEqual(snapshot["context_tokens"], 1500)
                self.assertEqual(snapshot["context_window"], 10000)
                self.assertEqual(snapshot["input_tokens"], 20000)
                self.assertIn("15.0%", "\n".join(context_lines(snapshot)))

    def test_subagent_metrics_and_error_pi_responses_do_not_replace_context(self) -> None:
        self.assertEqual(headless_usage("claude", {
            "type": "assistant", "parent_tool_use_id": "tool-child",
            "message": {"model": "child", "usage": {"input_tokens": 10}},
        }), {})
        self.assertEqual(headless_usage("pi", {
            "type": "message_end", "message": {
                "role": "assistant", "stopReason": "error", "usage": {"input": 0, "output": 0},
            },
        }), {})

    def test_pi_context_includes_cache_and_uses_native_total_when_provided(self) -> None:
        value = {"type": "message_end", "message": {
            "role": "assistant", "model": "pi-model", "stopReason": "stop",
            "usage": {"input": 100, "output": 200, "cacheRead": 300, "cacheWrite": 400},
        }}
        snapshot = headless_usage("pi", value)
        self.assertEqual(snapshot["context_tokens"], 1000)
        self.assertIn("无法计算占比", "\n".join(context_lines(snapshot)))
        value["message"]["usage"]["totalTokens"] = 1100
        self.assertEqual(headless_usage("pi", value)["context_tokens"], 1100)

    def test_model_change_invalidates_old_window_and_compaction_invalidates_occupancy(self) -> None:
        original = {"model": "old", "context_window": 10000, "context_tokens": 9000}
        updated = merge_usage(original, {"model": "new", "context_tokens": 100})
        self.assertNotIn("context_window", updated)
        for cli, event in (
            ("claude", {"type": "system", "subtype": "compact_boundary"}),
            ("pi", {"type": "compaction_end", "aborted": False}),
        ):
            with self.subTest(cli=cli):
                updated = merge_usage(original, headless_usage(cli, event))
                self.assertIsNone(updated["context_tokens"])
                self.assertNotIn("90.0%", "\n".join(context_lines(updated)))
        self.assertEqual(headless_usage("pi", {"type": "compaction_end", "aborted": True}), {})

    def test_quota_windows_use_reported_duration_and_show_zero_usage(self) -> None:
        lines = quota_lines({"rateLimits": {
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1790812800},
            "secondary": {"usedPercent": 35, "windowDurationMins": 10080},
        }})
        text = "\n".join(lines)
        self.assertIn("5小时额度：剩余 100%", text)
        self.assertIn("7天额度：剩余 65%", text)
        self.assertIn("UTC", text)

    def test_multibucket_quota_credits_and_explicit_spend_restriction(self) -> None:
        text = "\n".join(quota_lines({
            "ordinaryUsageAllowed": False,
            "rateLimitsByLimitId": {
                "codex": {"planType": "pro", "credits": {"balance": "12.50", "hasCredits": True, "unlimited": False}},
                "other": {"primary": {"usedPercent": 100}, "spendControlReached": True},
            },
        }))
        self.assertIn("12.50", text)
        self.assertIn("剩余 0%", text)
        self.assertIn("支出限制", text)
        self.assertIn("当前不允许", text)
        self.assertEqual(text.count("账户额度 ·"), 2)

    def test_missing_malformed_and_nonfinite_quota_fields_do_not_imply_allowance(self) -> None:
        for value in (None, {}, {"rateLimits": {"primary": {"usedPercent": float("nan")}, "credits": {"balance": "NaN"}}}):
            with self.subTest(value=value):
                text = "\n".join(quota_lines(value))
                self.assertIn("未返回", text)
                self.assertNotIn("剩余", text)

    def test_codex_quota_request_is_read_only_and_bounded(self) -> None:
        server = CodexAppServer(("codex",), lambda *_: None, lambda *_: None)
        server.request = Mock(return_value={"rateLimits": {}})
        server.read_rate_limits()
        server.request.assert_called_once_with("account/rateLimits/read", {}, timeout=5)


class GatewayUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.claude_root = self.project / "claude-config"
        environment = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude_root)})
        environment.start()
        self.addCleanup(environment.stop)
        config = Config(
            project_dir=self.project, bot_token="test", allowed_user_ids=frozenset({1}),
            allow_groups=False, allowed_roots=(self.project,), default_workdir=self.project,
            cli_commands={cli: ("/bin/sh",) for cli in ("codex", "claude", "grok", "pi")},
            poll_timeout=1, output_poll_interval=0.1, output_max_bytes=65536, tmux_socket_name="unused",
            bot_tokens=(("default", "default:test"), ("worker", "worker:test")),
        )
        self.app = GatewayApp(config)
        self.app._send = Mock()
        self.app.codex.read_rate_limits = Mock(return_value={"rateLimits": {
            "primary": {"usedPercent": 25, "windowDurationMins": 300},
        }})
        self.app.codex.start_turn = Mock(side_effect=AssertionError("status must not run inference"))
        self.app.headless.start_turn = Mock(side_effect=AssertionError("status must not run inference"))
        self.app.headless.read_claude_diagnostics = Mock(return_value={
            "context": {}, "quota_lines": ["账户额度：Claude 未返回额度数据。"],
        })

    def codex_session(self, thread_id: str = "thread-a", bot_key: str = "default"):
        return self.app.sessions.create_virtual("codex", self.project, 1, "codex-app-server", thread_id, bot_key)

    def report_codex_usage(self, thread_id: str = "thread-a") -> None:
        self.app._on_codex_notification("thread/tokenUsage/updated", {
            "threadId": thread_id, "turnId": "finished-turn", "tokenUsage": CODEX_USAGE,
        })

    def test_status_reports_busy_queue_context_and_quota_without_creating_a_turn(self) -> None:
        session = self.codex_session()
        self.app._finished_turns.append((session.session_id, "finished-turn"))
        self.report_codex_usage()
        self.assertEqual(self.app._turns, {})
        self.app._set_session_state(session.session_id, STATE_WORKING)
        self.app._queues[session.session_id] = [PendingInput(1, "queued")]
        view = self.app._register_turn(session, "active-turn")
        view.started_at = time.monotonic() - 10
        self.app._handle_command(1, "status", "")
        text = self.app._send.call_args.args[1]
        for expected in ("运行中", "排队 1", "已运行 10秒", "20.0%", "900,000", "剩余 75%"):
            self.assertIn(expected, text)
        self.app.codex.read_rate_limits.assert_called_once()
        self.app.codex.start_turn.assert_not_called()
        self.app.headless.start_turn.assert_not_called()

    def test_context_queries_only_local_snapshot_for_current_bot(self) -> None:
        first = self.codex_session()
        second = self.codex_session("thread-b", "worker")
        self.report_codex_usage("thread-b")
        with self.app._bot_scope("worker"):
            self.app._handle_command(1, "context", "")
        text = self.app._send.call_args.args[1]
        self.assertIn(second.session_id, text)
        self.assertNotIn(first.session_id, text)
        self.assertIn("20.0%", text)
        self.app.codex.read_rate_limits.assert_not_called()

    def test_snapshot_survives_restart_and_clear_rejects_late_metrics(self) -> None:
        session = self.codex_session()
        self.report_codex_usage()
        reloaded = SessionManager(self.app.config)
        snapshot = reloaded.get(session.session_id).usage
        self.assertEqual(snapshot["context_tokens"], 2000)
        self.assertEqual(snapshot["total_tokens"], 900000)
        reloaded.update_backend(session.session_id, "codex-app-server", "new-thread")
        self.assertIsNone(reloaded.get(session.session_id).usage)
        self.assertFalse(reloaded.update_usage(session.session_id, "thread-a", {"context_tokens": 999}))
        self.assertIsNone(reloaded.get(session.session_id).usage)

    def test_headless_usage_does_not_create_answer_card_and_rotation_clears_it(self) -> None:
        session = self.app.sessions.create_headless("pi", self.project, 1)
        self.app._on_headless_event(session.session_id, "turn-a", "usage", {
            "external_id": session.external_id, "context_tokens": 1000, "raw_transcript": "do not store",
        })
        self.assertEqual(self.app._turns, {})
        snapshot = self.app.sessions.get(session.session_id).usage
        self.assertNotIn("external_id", snapshot)
        self.assertNotIn("raw_transcript", snapshot)
        self.app.sessions.rotate_external_id(session.session_id)
        self.assertIsNone(self.app.sessions.get(session.session_id).usage)
        self.app._on_headless_event(session.session_id, "old-turn", "usage", {
            "external_id": session.external_id, "context_tokens": 999,
        })
        self.assertIsNone(self.app.sessions.get(session.session_id).usage)

    def test_quota_failure_still_returns_local_status(self) -> None:
        session = self.codex_session()
        self.report_codex_usage()
        self.app.codex.read_rate_limits.side_effect = CodexBackendError("not available")
        self.app._handle_command(1, "status", "")
        text = self.app._send.call_args.args[1]
        self.assertIn(session.session_id, text)
        self.assertIn("20.0%", text)
        self.assertIn("暂时无法查询", text)

    def test_headless_reports_missing_capabilities_and_no_session_prompts_creation(self) -> None:
        self.app._handle_command(1, "status", "")
        self.assertIn("/new", self.app._send.call_args.args[1])
        self.app.sessions.create_headless("grok", self.project, 1)
        self.app._handle_command(1, "status", "")
        text = self.app._send.call_args.args[1]
        self.assertIn("等待 CLI", text)
        self.assertIn("未提供额度查询", text)
        self.app.codex.read_rate_limits.assert_not_called()

    def test_claude_status_and_context_backfill_native_history_without_a_model_turn(self) -> None:
        session = self.app.sessions.create_headless("claude", self.project, 1)
        directory = self.claude_root / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(self.project))
        directory.mkdir(parents=True)
        path = directory / f"{session.external_id}.jsonl"
        path.write_text(json.dumps({
            "type": "assistant", "uuid": "native-message", "parentUuid": None,
            "sessionId": session.external_id, "timestamp": "2026-09-30T12:00:00Z",
            "message": {"model": "claude-model", "usage": {
                "input_tokens": 2, "cache_read_input_tokens": 1000, "output_tokens": 70,
            }, "content": [{"type": "text", "text": "private native history"}]},
        }) + "\n")
        for command in ("status", "context"):
            self.app._handle_command(1, command, "")
            text = self.app._send.call_args.args[1]
            self.assertIn("1,002 tokens", text)
            self.assertIn("历史记录", text)
            self.assertIn("2026-09-30T12:00:00", text)
            self.assertNotIn("private native history", text)
        snapshot = SessionManager(self.app.config).get(session.session_id).usage
        self.assertEqual(snapshot["context_tokens"], 1002)
        self.assertNotIn("private native history", json.dumps(snapshot))
        self.assertEqual(self.app._turns, {})
        self.app.headless.start_turn.assert_not_called()
        self.app.codex.read_rate_limits.assert_not_called()
        self.app.sessions.rotate_external_id(session.session_id)
        self.app._handle_command(1, "context", "")
        self.assertIn("等待 CLI", self.app._send.call_args.args[1])

    def test_claude_history_does_not_overwrite_live_reports_compaction_or_clear(self) -> None:
        session = self.app.sessions.create_headless("claude", self.project, 1)
        historical = {"context_tokens": 1000, "reported_at": "2026-09-30T12:00:00Z"}
        def report_live(*_):
            self.app.sessions.update_usage(session.session_id, session.external_id, {"context_tokens": 200})
            return historical
        with patch("cli_telegram_gateway.app.read_claude_usage", side_effect=report_live):
            self.app._handle_command(1, "context", "")
        self.assertEqual(self.app.sessions.get(session.session_id).usage["context_tokens"], 200)
        for tokens in (200, None):
            self.app.sessions.update_usage(session.session_id, session.external_id, {"context_tokens": tokens})
            with patch("cli_telegram_gateway.app.read_claude_usage") as reader:
                self.app._handle_command(1, "context", "")
                reader.assert_not_called()
            self.assertFalse(self.app.sessions.update_usage(
                session.session_id, session.external_id, historical, only_if_missing=True,
            ))
        self.app.sessions.rotate_external_id(session.session_id)
        self.assertFalse(self.app.sessions.update_usage(
            session.session_id, session.external_id, historical, only_if_missing=True,
        ))

    def test_claude_native_window_and_quota_augment_saved_context_without_a_turn(self) -> None:
        session = self.app.sessions.create_headless("claude", self.project, 1)
        self.app.sessions.update_usage(session.session_id, session.external_id, {
            "model": "claude-model", "context_tokens": 291667,
            "reported_at": "2026-09-30T12:00:00Z", "context_basis": "最近成功请求输入 · 历史记录",
        })
        self.app.headless.read_claude_diagnostics.return_value = {
            "context": {"model": "claude-model", "context_window": 1000000},
            "quota_lines": ["账户额度 · Claude（max）", "7天额度：剩余 98%（已用 2%）"],
        }
        for command in ("status", "context"):
            self.app._handle_command(1, command, "")
            self.app.headless.read_claude_diagnostics.assert_called_with(
                unittest.mock.ANY, model="claude-model", include_quota=command == "status",
            )
            text = self.app._send.call_args.args[1]
            self.assertIn("291,667 / 1,000,000", text)
            self.assertIn("29.2%", text)
            self.assertIn("708,333", text)
            self.assertIn("2026-09-30T12:00:00", text)
            self.assertEqual("剩余 98%" in text, command == "status")
        saved = SessionManager(self.app.config).get(session.session_id).usage
        self.assertEqual(saved["context_window"], 1000000)
        self.assertNotIn("quota_lines", saved)
        self.assertEqual(self.app._turns, {})
        self.app.headless.start_turn.assert_not_called()

    def test_claude_native_query_failure_keeps_saved_context_and_window(self) -> None:
        session = self.app.sessions.create_headless("claude", self.project, 1)
        self.app.sessions.update_usage(session.session_id, session.external_id, {
            "model": "claude-model", "context_tokens": 1000, "context_window": 10000,
        })
        self.app.headless.read_claude_diagnostics.side_effect = HeadlessBackendError("temporary failure")
        self.app._handle_command(1, "status", "")
        text = self.app._send.call_args.args[1]
        self.assertIn("1,000 / 10,000", text)
        self.assertIn("原生窗口查询暂时不可用", text)
        self.assertIn("账户额度：暂时无法查询", text)

    def test_claude_window_query_rejects_a_concurrent_model_change(self) -> None:
        session = self.app.sessions.create_headless("claude", self.project, 1)
        self.app.sessions.update_usage(session.session_id, session.external_id, {"model": "old", "context_tokens": 1000})
        def model_changes(*_, **__):
            self.app.sessions.update_usage(session.session_id, session.external_id, {"model": "new", "context_tokens": 50})
            return {"context": {"model": "old", "context_window": 10000}}
        self.app.headless.read_claude_diagnostics.side_effect = model_changes
        self.app._handle_command(1, "context", "")
        snapshot = self.app.sessions.get(session.session_id).usage
        self.assertEqual(snapshot["model"], "new")
        self.assertEqual(snapshot["context_tokens"], 50)
        self.assertNotIn("context_window", snapshot)

    def test_claude_window_query_rejects_clear_and_preserves_compaction_invalidation(self) -> None:
        session = self.app.sessions.create_headless("claude", self.project, 1)
        self.app.sessions.update_usage(session.session_id, session.external_id, {"model": "main", "context_tokens": 1000})
        def compact_during_probe(*_, **__):
            self.app.sessions.update_usage(session.session_id, session.external_id, {"context_tokens": None})
            return {"context": {"model": "main", "context_window": 10000}}
        self.app.headless.read_claude_diagnostics.side_effect = compact_during_probe
        self.app._handle_command(1, "context", "")
        snapshot = self.app.sessions.get(session.session_id).usage
        self.assertIsNone(snapshot["context_tokens"])
        self.assertEqual(snapshot["context_window"], 10000)
        def clear_during_probe(*_, **__):
            self.app.sessions.rotate_external_id(session.session_id)
            return {"context": {"model": "main", "context_window": 10000}}
        self.app.headless.read_claude_diagnostics.side_effect = clear_during_probe
        self.app._handle_command(1, "context", "")
        self.assertIsNone(self.app.sessions.get(session.session_id).usage)

    def test_headless_parser_emits_usage_before_terminal_result(self) -> None:
        backend = HeadlessBackend({}, lambda *_: None)
        events = backend._parse_event("claude", {
            "type": "result", "usage": {"input_tokens": 123, "output_tokens": 456}, "result": "done",
        }, {}, False)
        self.assertEqual([kind for kind, _data in events], ["usage", "delta", "completed"])

    def test_headless_process_stream_records_numeric_usage_and_delivers_answer(self) -> None:
        session = self.app.sessions.create_headless("pi", self.project, 1)
        events = [{"type": "message_end", "message": {
            "role": "assistant", "model": "pi-model", "stopReason": "stop",
            "content": [{"type": "text", "text": "answer"}],
            "usage": {"input": 100, "output": 50, "cacheRead": 200, "cacheWrite": 0},
        }}, {"type": "agent_end"}]
        script = "import json; values = json.loads(" + repr(json.dumps(events)) + "); [print(json.dumps(v)) for v in values]"
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.app.headless._read_process(session, "native-turn", process)
        self.assertEqual(process.returncode, 0)
        snapshot = self.app.sessions.get(session.session_id).usage
        self.assertEqual(snapshot["context_tokens"], 350)
        self.assertEqual(snapshot["model"], "pi-model")
        view = self.app._turns[(session.session_id, "native-turn")]
        self.assertEqual(view.status, "completed")
        self.assertEqual("".join(view.parts), "answer")
        self.assertEqual(len(self.app._turns), 1)

    def test_compaction_clears_occupancy_and_late_duplicate_does_not_erase_new_report(self) -> None:
        session = self.codex_session()
        self.report_codex_usage()
        event = {"threadId": "thread-a", "turnId": "compact-turn", "item": {"type": "contextCompaction"}}
        self.app._on_codex_notification("item/completed", event)
        snapshot = self.app.sessions.get(session.session_id).usage
        self.assertIsNone(snapshot["context_tokens"])
        self.assertEqual(self.app._turns, {})
        self.app._finished_turns.append((session.session_id, "compact-turn"))
        self.report_codex_usage()
        self.app._on_codex_notification("item/completed", event)
        self.assertEqual(self.app.sessions.get(session.session_id).usage["context_tokens"], 2000)

    def test_status_distinguishes_active_model_from_new_selection(self) -> None:
        session = self.codex_session()
        self.app.sessions.set_model(session.session_id, "old-model")
        session = self.app.sessions.get(session.session_id)
        self.app._register_turn(session, "active")
        self.app.sessions.set_model(session.session_id, "new-model")
        self.app._handle_command(1, "status", "")
        text = self.app._send.call_args.args[1]
        self.assertIn("模型：new-model", text)
        self.assertIn("当前任务模型：old-model", text)

    def test_commands_are_registered_in_menu_help_and_reserved_for_extensions(self) -> None:
        from cli_telegram_gateway.app import BUILTIN_COMMANDS
        for command in ("status", "context"):
            self.assertIn(command, dict(BOT_COMMANDS))
            self.assertIn(command, BUILTIN_COMMANDS)
            self.assertIn(f"/{command}", HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
