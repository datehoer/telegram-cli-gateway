"""Bot identities (name and @username) for the page header and the back link."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Settings
from .store import _write_private


class TelegramError(RuntimeError):
    pass


def call(settings: Settings, bot_key: str, method: str, payload: dict[str, Any]) -> Any:
    token = settings.bot_tokens.get(bot_key)
    if not token:
        raise TelegramError(f"no token for bot {bot_key!r}")
    request = urllib.request.Request(
        f"{settings.bot_api_base}/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read())
    except (OSError, urllib.error.URLError, ValueError) as exc:
        # Never echo the URL: it contains the bot token.
        raise TelegramError(f"{method} failed: {type(exc).__name__}") from None
    if not body.get("ok"):
        raise TelegramError(f"{method} failed: {body.get('description', 'unknown error')}")
    return body.get("result")


def bot_identities(settings: Settings, refresh: bool = False) -> dict[str, dict[str, str]]:
    """{bot_key: {"name", "username"}}, cached in .runtime/bots.json."""
    cache = settings.runtime_dir / "bots.json"
    if not refresh:
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if set(cached) == set(settings.bot_tokens):
                return cached
        except (OSError, ValueError):
            pass
    identities: dict[str, dict[str, str]] = {}
    for bot_key in settings.bot_tokens:
        try:
            me = call(settings, bot_key, "getMe", {})
        except TelegramError:
            continue
        identities[bot_key] = {
            "name": str(me.get("first_name") or me.get("username") or ""),
            "username": str(me.get("username") or ""),
        }
    if identities:
        settings.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_private(cache, json.dumps(identities, ensure_ascii=False, indent=1))
    return identities
