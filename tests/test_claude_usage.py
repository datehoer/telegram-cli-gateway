from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_telegram_gateway.claude_usage import read_claude_usage
from cli_telegram_gateway.usage import context_lines, headless_usage, merge_usage


class ClaudeHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "claude-config"
        self.cwd = str(Path(temporary.name) / "project.with space_中文")
        self.external_id = "00000000-0000-4000-8000-000000000001"
        directory = self.root / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", self.cwd)
        directory.mkdir(parents=True)
        self.path = directory / f"{self.external_id}.jsonl"
        environment = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def assistant(self, record_id: str, parent: str | None, **overrides):
        value = {
            "type": "assistant", "uuid": record_id, "parentUuid": parent,
            "sessionId": self.external_id, "isSidechain": False,
            "timestamp": "2026-09-30T12:34:56.789Z",
            "message": {
                "model": "main-model", "content": [{"type": "text", "text": "private transcript"}],
                "usage": {
                    "input_tokens": 2, "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 50, "output_tokens": 70,
                },
            },
        }
        value.update(overrides)
        return value

    def write(self, *records, suffix: bytes = b"") -> None:
        self.path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records) + suffix)

    def test_last_success_skips_failed_zero_and_subagent_records_and_keeps_report_time(self) -> None:
        self.write(
            self.assistant("success", None),
            self.assistant("error", "success", isApiErrorMessage=True),
            self.assistant("zero", "error", message={"model": "synthetic", "usage": {"input_tokens": 0}}),
            self.assistant("child", "zero", isSidechain=True),
            {"type": "cost-state", "sessionId": self.external_id,
             "modelUsage": {"main-model": {"contextWindow": 10000}, "child": {"contextWindow": 500}}},
            suffix=b"invalid JSON\n{\"type\":\"assistant\"",
        )
        snapshot = merge_usage(None, read_claude_usage(self.external_id, self.cwd))
        self.assertEqual(snapshot["context_tokens"], 1052)
        self.assertEqual(snapshot["context_window"], 10000)
        self.assertEqual(snapshot["output_tokens"], 70)
        self.assertEqual(snapshot["reported_at"], "2026-09-30T12:34:56+00:00")
        text = "\n".join(context_lines(snapshot))
        self.assertIn("历史记录", text)
        self.assertIn("2026-09-30T12:34:56", text)
        self.assertNotIn("private transcript", json.dumps(snapshot))
        # Billing/result updates do not make historical occupancy look fresh.
        self.assertEqual(merge_usage(snapshot, {"output_tokens": 90})["reported_at"], snapshot["reported_at"])
        live = merge_usage(snapshot, {"context_tokens": 200, "context_basis": "最近请求输入"})
        self.assertNotIn("reported_at", live)
        self.assertNotIn("历史记录", "\n".join(context_lines(live)))

    def test_rewound_branch_uses_its_parent_chain(self) -> None:
        self.write(
            self.assistant("original", None),
            self.assistant("discarded", "original", message={"model": "other", "usage": {"input_tokens": 99999}}),
            {"type": "user", "uuid": "rewound", "parentUuid": "original", "sessionId": self.external_id},
            self.assistant("failed", "rewound", isApiErrorMessage=True),
        )
        self.assertEqual(read_claude_usage(self.external_id, self.cwd)["context_tokens"], 1052)

    def test_compaction_prevents_using_old_context_until_a_new_success(self) -> None:
        before = self.assistant("before", None)
        boundary = {
            "type": "system", "subtype": "compact_boundary", "uuid": "compact", "parentUuid": "before",
            "sessionId": self.external_id, "timestamp": "2026-09-30T13:00:00Z",
        }
        self.write(before, boundary, self.assistant("failed", "compact", isApiErrorMessage=True))
        snapshot = read_claude_usage(self.external_id, self.cwd)
        self.assertIsNone(snapshot["context_tokens"])
        self.assertNotIn("1,052", "\n".join(context_lines(snapshot)))
        self.write(before, boundary, self.assistant("new", "compact", message={"usage": {"input_tokens": 100}}))
        self.assertEqual(read_claude_usage(self.external_id, self.cwd)["context_tokens"], 100)

    def test_missing_history_and_invalid_session_ids_return_no_usage(self) -> None:
        self.assertEqual(read_claude_usage(self.external_id, self.cwd), {})
        self.write(self.assistant("success", None, sessionId="other-session"))
        self.assertEqual(read_claude_usage(self.external_id, self.cwd), {})
        for invalid in ("../../credentials", "not-a-uuid", None):
            with self.subTest(external_id=invalid):
                self.assertEqual(read_claude_usage(invalid, self.cwd), {})

    def test_moved_directory_finds_only_unique_exact_session_filename(self) -> None:
        self.write(self.assistant("success", None))
        moved = self.root / "projects" / "other-encoding"
        self.path.parent.rename(moved)
        self.path = moved / self.path.name
        self.assertEqual(read_claude_usage(self.external_id, self.cwd)["context_tokens"], 1052)
        duplicate = self.root / "projects" / "another-directory"
        duplicate.mkdir()
        (duplicate / self.path.name).write_bytes(self.path.read_bytes())
        self.assertEqual(read_claude_usage(self.external_id, self.cwd), {})

    def test_bounded_tail_accepts_complete_recent_records(self) -> None:
        self.path.write_bytes(b"x" * 2048 + b"\n" + json.dumps(self.assistant("recent", None)).encode() + b"\n")
        with patch("cli_telegram_gateway.claude_usage.MAX_HISTORY_BYTES", 1024):
            self.assertEqual(read_claude_usage(self.external_id, self.cwd)["context_tokens"], 1052)

    def test_default_native_config_directory(self) -> None:
        self.write(self.assistant("success", None))
        self.root.rename(self.root.parent / ".claude")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}), patch(
            "cli_telegram_gateway.claude_usage.Path.home", return_value=self.root.parent,
        ):
            self.assertEqual(read_claude_usage(self.external_id, self.cwd)["context_tokens"], 1052)

    def test_live_error_and_synthetic_messages_do_not_erase_previous_context(self) -> None:
        for event in (
            self.assistant("error", None, isApiErrorMessage=True),
            self.assistant("zero", None, message={"model": "synthetic", "usage": {"input_tokens": 0, "output_tokens": 0}}),
            self.assistant("subagent", None, isSidechain=True),
        ):
            with self.subTest(event=event["uuid"]):
                self.assertEqual(headless_usage("claude", event), {})


if __name__ == "__main__":
    unittest.main()
