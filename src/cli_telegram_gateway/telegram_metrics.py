from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("telegram-cli-gateway")
RETENTION_DAYS = 14
IGNORED_METHODS = frozenset({"getUpdates", "getFile"})
NEW_MESSAGE_METHODS = frozenset(
    {
        "sendMessage",
        "sendRichMessage",
        "copyMessage",
        "sendPhoto",
        "sendDocument",
        "sendMediaGroup",
    }
)
EDIT_METHODS = frozenset({"editMessageText", "editMessageReplyMarkup"})
COUNTER_FIELDS = (
    "network_requests",
    "successful_requests",
    "failed_requests",
    "rate_limited",
    "local_deferrals",
    "new_messages",
    "message_edits",
    "other_writes",
)


def _empty_bucket() -> dict[str, Any]:
    return {**{field: 0 for field in COUNTER_FIELDS}, "methods": {}}


class TelegramMetrics:
    """Bounded UTC daily counters for Telegram write-side API activity."""

    def __init__(
        self,
        path: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._days = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("could not load Telegram metrics: %s", exc)
            return {}
        days = value.get("days") if isinstance(value, dict) else None
        if not isinstance(days, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for day, bucket in days.items():
            try:
                normalized[str(day)] = self._normalized_bucket(bucket)
            except (TypeError, ValueError):
                LOGGER.warning("ignoring malformed Telegram metrics bucket for %s", day)
        return normalized

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"days": self._days}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except OSError as exc:
            LOGGER.warning("could not persist Telegram metrics: %s", exc)

    def record(self, method: str, outcome: str) -> None:
        if method in IGNORED_METHODS:
            return
        if outcome not in {"success", "failed", "rate_limited", "local_deferred"}:
            raise ValueError(f"unknown Telegram metrics outcome: {outcome}")

        day = self._now().astimezone(timezone.utc).date().isoformat()
        with self._lock:
            bucket = self._days.setdefault(day, _empty_bucket())
            methods = bucket.setdefault("methods", {})
            method_counts = methods.setdefault(
                method,
                {"success": 0, "failed": 0, "rate_limited": 0, "local_deferred": 0},
            )
            method_counts[outcome] = int(method_counts.get(outcome, 0)) + 1

            if outcome == "local_deferred":
                bucket["local_deferrals"] = int(bucket.get("local_deferrals", 0)) + 1
            else:
                bucket["network_requests"] = int(bucket.get("network_requests", 0)) + 1
                if outcome == "success":
                    bucket["successful_requests"] = (
                        int(bucket.get("successful_requests", 0)) + 1
                    )
                    category = (
                        "new_messages"
                        if method in NEW_MESSAGE_METHODS
                        else "message_edits"
                        if method in EDIT_METHODS
                        else "other_writes"
                    )
                    bucket[category] = int(bucket.get(category, 0)) + 1
                else:
                    bucket["failed_requests"] = int(bucket.get("failed_requests", 0)) + 1
                    if outcome == "rate_limited":
                        bucket["rate_limited"] = int(bucket.get("rate_limited", 0)) + 1

            for stale_day in sorted(self._days)[:-RETENTION_DAYS]:
                self._days.pop(stale_day, None)
            self._save()

    def snapshot(self, days: int = 7) -> dict[str, Any]:
        now = self._now().astimezone(timezone.utc)
        day_names = [
            (now.date() - timedelta(days=offset)).isoformat()
            for offset in range(max(1, days))
        ]
        with self._lock:
            today = self._normalized_bucket(self._days.get(day_names[0], {}))
            total = _empty_bucket()
            for day in day_names:
                bucket = self._normalized_bucket(self._days.get(day, {}))
                for field in COUNTER_FIELDS:
                    total[field] += bucket[field]
                for method, counts in bucket["methods"].items():
                    combined = total["methods"].setdefault(
                        method,
                        {
                            "success": 0,
                            "failed": 0,
                            "rate_limited": 0,
                            "local_deferred": 0,
                        },
                    )
                    for outcome, count in counts.items():
                        combined[outcome] = int(combined.get(outcome, 0)) + int(count)
        return {"utc_day": day_names[0], "today": today, "last_7_days": total}

    @staticmethod
    def _normalized_bucket(value: object) -> dict[str, Any]:
        raw = value if isinstance(value, dict) else {}
        bucket = _empty_bucket()
        for field in COUNTER_FIELDS:
            bucket[field] = int(raw.get(field, 0))
        raw_methods = raw.get("methods")
        if isinstance(raw_methods, dict):
            bucket["methods"] = {
                str(method): {
                    outcome: int(counts.get(outcome, 0))
                    for outcome in ("success", "failed", "rate_limited", "local_deferred")
                }
                for method, counts in raw_methods.items()
                if isinstance(counts, dict)
            }
        return bucket
