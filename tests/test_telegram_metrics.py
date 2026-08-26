from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli_telegram_gateway.telegram_metrics import TelegramMetrics


class TelegramMetricsTests(unittest.TestCase):
    def test_daily_counters_persist_without_message_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "telegram-metrics.json"
            now = lambda: datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
            metrics = TelegramMetrics(path, now=now)
            metrics.record("sendMessage", "success")
            metrics.record("editMessageText", "success")
            metrics.record("answerCallbackQuery", "success")
            metrics.record("sendRichMessage", "rate_limited")
            metrics.record("copyMessage", "local_deferred")

            snapshot = TelegramMetrics(path, now=now).snapshot()
            today = snapshot["today"]
            self.assertEqual(snapshot["utc_day"], "2026-08-26")
            self.assertEqual(today["network_requests"], 4)
            self.assertEqual(today["successful_requests"], 3)
            self.assertEqual(today["failed_requests"], 1)
            self.assertEqual(today["rate_limited"], 1)
            self.assertEqual(today["local_deferrals"], 1)
            self.assertEqual(today["new_messages"], 1)
            self.assertEqual(today["message_edits"], 1)
            self.assertEqual(today["other_writes"], 1)
            stored = path.read_text(encoding="utf-8")
            self.assertNotIn("chat_id", stored)
            self.assertNotIn("message text", stored)

    def test_read_methods_are_not_counted(self) -> None:
        metrics = TelegramMetrics()
        metrics.record("getUpdates", "success")
        metrics.record("getFile", "failed")
        today = metrics.snapshot()["today"]
        self.assertEqual(today["network_requests"], 0)
        self.assertEqual(today["failed_requests"], 0)

    def test_retention_keeps_only_latest_fourteen_utc_days(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            current = [datetime(2026, 8, 1, tzinfo=timezone.utc)]
            path = Path(temporary) / "telegram-metrics.json"
            metrics = TelegramMetrics(path, now=lambda: current[0])
            for offset in range(16):
                current[0] = datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(
                    days=offset
                )
                metrics.record("sendMessage", "success")

            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored["days"]), 14)
            self.assertNotIn("2026-08-01", stored["days"])
            self.assertNotIn("2026-08-02", stored["days"])
            self.assertIn("2026-08-16", stored["days"])

    def test_malformed_bucket_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "telegram-metrics.json"
            path.write_text(
                '{"days":{"2026-08-26":{"network_requests":"broken"}}}',
                encoding="utf-8",
            )
            metrics = TelegramMetrics(
                path, now=lambda: datetime(2026, 8, 26, tzinfo=timezone.utc)
            )
            metrics.record("sendMessage", "success")
            self.assertEqual(metrics.snapshot()["today"]["new_messages"], 1)


if __name__ == "__main__":
    unittest.main()
