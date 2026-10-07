from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cli_telegram_gateway.headless_backend import HeadlessBackend, HeadlessBackendError
from cli_telegram_gateway.sessions import CliSession
from cli_telegram_gateway.usage import claude_context_window, claude_quota_lines, context_lines, merge_usage


CONTEXT_RESPONSE = {
    "model": "main-model", "rawMaxTokens": 1000000, "maxTokens": 500000,
    "totalTokens": 500, "apiUsage": None,
    "memoryFiles": [{"path": "/private/memory.md", "tokens": 10}],
}
QUOTA_RESPONSE = {
    "subscription_type": "max", "rate_limits_available": True,
    "rate_limits": {
        "limits": [
            {"kind": "session", "percent": 0, "resets_at": None},
            {"kind": "weekly_all", "percent": 2, "resets_at": "2026-10-01T12:00:00Z"},
            {"kind": "weekly_scoped", "percent": 25, "scope": {"model": {"display_name": "Opus"}}},
        ],
        "extra_usage": {"is_enabled": False},
    },
    "account": {"token": "private-account-data"},
    "behaviors": {"private-transcript": "do not display or persist"},
}


class ClaudeDiagnosticsParsingTests(unittest.TestCase):
    def test_window_uses_native_capacity_and_does_not_import_empty_probe_usage(self) -> None:
        self.assertEqual(claude_context_window(CONTEXT_RESPONSE), {
            "model": "main-model", "context_window": 1000000,
        })
        self.assertEqual(claude_context_window({"model": "legacy", "maxTokens": 200000}), {
            "model": "legacy", "context_window": 200000,
        })
        for invalid in (None, {}, {"model": "m", "rawMaxTokens": True}, {"rawMaxTokens": 10000}):
            self.assertEqual(claude_context_window(invalid), {})

    def test_window_refresh_preserves_the_last_live_request_time(self) -> None:
        previous = {"model": "main-model", "context_tokens": 1000, "updated_at": "2026-09-30T12:00:00+00:00"}
        updated = merge_usage(previous, claude_context_window(CONTEXT_RESPONSE))
        self.assertEqual(updated["reported_at"], previous["updated_at"])
        self.assertIn("2026-09-30T12:00:00", "\n".join(context_lines(updated)))
        updated = merge_usage(updated, {"context_tokens": 2000})
        self.assertNotIn("reported_at", updated)

    def test_native_meter_rows_show_limits_scopes_zero_usage_and_utc_resets(self) -> None:
        text = "\n".join(claude_quota_lines(QUOTA_RESPONSE))
        for expected in ("Claude (max)", "5-hour quota: 100% left", "7-day quota: 98% left", "10-01 12:00 UTC", "Opus: 75% left", "Extra usage: off"):
            self.assertIn(expected, text)
        self.assertNotIn("private-account-data", text)
        self.assertNotIn("private-transcript", text)

    def test_legacy_native_windows_and_enabled_extra_usage(self) -> None:
        text = "\n".join(claude_quota_lines({
            "rate_limits_available": True, "rate_limits": {
                "five_hour": {"utilization": 100, "resets_at": "invalid"},
                "seven_day_sonnet": {"utilization": 10},
                "extra_usage": {"is_enabled": True, "utilization": 20},
            },
        }))
        self.assertIn("5-hour quota: 0% left", text)
        self.assertIn("Sonnet: 90% left", text)
        self.assertIn("Extra usage: on, 20% used", text)
        self.assertNotIn("invalid", text)

    def test_unavailable_and_malformed_quotas_do_not_imply_available_allowance(self) -> None:
        self.assertIn("sign-in", "\n".join(claude_quota_lines({"rate_limits_available": False})))
        for value in (None, {}, {"rate_limits": {"limits": [
            {"percent": True}, {"percent": -1}, {"percent": float("nan")},
        ]}}):
            text = "\n".join(claude_quota_lines(value))
            self.assertIn("returned no", text)
            self.assertNotIn("% left", text)
        # Invalid identifiers must not raise or become user-facing labels.
        text = "\n".join(claude_quota_lines({
            "subscription_type": {}, "rate_limits": {"limits": [{"kind": {}, "percent": 50}]},
        }))
        self.assertIn("50% left", text)


class ClaudeControlQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        self.requests_path = self.project / "requests.json"
        self.session = CliSession("claude-test", "claude", str(self.project), "", "", 1, "now", "headless-json", "native-id")

    def backend(self, *, error_subtype: str | None = None, partial: bool = False, oversized: bool = False):
        script = self.project / "fake_claude.py"
        script.write_text("\n".join([
            "import json,sys,time,os",
            "from pathlib import Path",
            "path=Path(sys.argv[1])",
            "log={'args':sys.argv[2:],'requests':[]}",
            "path.write_text(json.dumps(log))",
            "replies=" + repr({"initialize": {"account": "private-account-data"}, "get_context_usage": CONTEXT_RESPONSE, "get_usage": QUOTA_RESPONSE}),
            "error_subtype=" + repr(error_subtype),
            "partial=" + repr(partial),
            "oversized=" + repr(oversized),
            "for line in sys.stdin:",
            "    request=json.loads(line)",
            "    log['requests'].append(request)",
            "    path.write_text(json.dumps(log))",
            "    if partial:",
            "        sys.stdout.write('{partial-frame');sys.stdout.flush();time.sleep(60)",
            "    if oversized:",
            "        sys.stdout.write('x'*(2*1024*1024+1));sys.stdout.flush();time.sleep(60)",
            "    subtype=request['request']['subtype']",
            "    response={'subtype':'success','request_id':request['request_id'],'response':replies[subtype]}",
            "    if subtype==error_subtype:response={'subtype':'error','request_id':request['request_id'],'error':'private-account-data'}",
            "    print('invalid JSON',flush=True)",
            "    print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':'unrelated','response':{}}}),flush=True)",
            "    frame=json.dumps({'type':'control_response','response':response})+'\\n'",
            "    os.write(sys.stdout.fileno(),frame[:17].encode())",
            "    os.write(sys.stdout.fileno(),frame[17:].encode())",
        ]))
        return HeadlessBackend({"claude": (sys.executable, str(script), str(self.requests_path))}, lambda *_: self.fail("diagnostics emitted a turn event"))

    def test_native_controls_are_read_only_and_return_only_diagnostics(self) -> None:
        backend = self.backend()
        result = backend.read_claude_diagnostics(self.session, model="main-model", include_quota=True)
        self.assertEqual(result["context"], {"model": "main-model", "context_window": 1000000})
        self.assertIn("98% left", "\n".join(result["quota_lines"]))
        self.assertNotIn("private-account-data", json.dumps(result))
        self.assertNotIn("/private/memory.md", json.dumps(result))
        self.assertEqual(backend._active, {})
        log = json.loads(self.requests_path.read_text())
        self.assertEqual([r["type"] for r in log["requests"]], ["control_request"] * 3)
        self.assertEqual([r["request"] for r in log["requests"]], [
            {"subtype": "initialize"}, {"subtype": "get_context_usage", "detail": "summary"},
            {"subtype": "get_usage", "skip_behaviors": True},
        ])
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--settings", "--model"):
            self.assertIn(flag, log["args"])
        self.assertIn('{"disableAllHooks":true}', log["args"])
        self.assertNotIn("--resume", log["args"])
        self.assertNotIn("--session-id", log["args"])

    def test_context_command_does_not_read_account_quotas(self) -> None:
        result = self.backend().read_claude_diagnostics(self.session, model="main-model")
        self.assertNotIn("quota_lines", result)
        log = json.loads(self.requests_path.read_text())
        self.assertEqual(len(log["requests"]), 2)

    def test_quota_failure_preserves_successful_window(self) -> None:
        result = self.backend(error_subtype="get_usage").read_claude_diagnostics(self.session, include_quota=True)
        self.assertEqual(result["context"]["context_window"], 1000000)
        self.assertIn("temporarily unavailable", "\n".join(result["quota_lines"]))
        self.assertNotIn("private-account-data", json.dumps(result))

    def test_unsupported_context_control_still_reads_quotas(self) -> None:
        result = self.backend(error_subtype="get_context_usage").read_claude_diagnostics(self.session, include_quota=True)
        self.assertEqual(result["context"], {})
        self.assertIn("98% left", "\n".join(result["quota_lines"]))

    def test_partial_response_is_bounded_by_timeout_and_cleans_up(self) -> None:
        backend = self.backend(partial=True)
        start = time.monotonic()
        with self.assertRaisesRegex(HeadlessBackendError, "timed out"):
            backend.read_claude_diagnostics(self.session, timeout=0.2)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(backend._active, {})

    def test_oversized_response_and_missing_executable_fail_without_raw_output(self) -> None:
        with self.assertRaisesRegex(HeadlessBackendError, "exceeded the size limit"):
            self.backend(oversized=True).read_claude_diagnostics(self.session)
        backend = HeadlessBackend({"claude": ("/missing/claude",)}, lambda *_: None)
        with self.assertRaisesRegex(HeadlessBackendError, "Could not start"):
            backend.read_claude_diagnostics(self.session)


if __name__ == "__main__":
    unittest.main()
