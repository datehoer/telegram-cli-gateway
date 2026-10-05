"""Best-effort guess of which CLI and bot entrance is publishing a page.

The publisher runs inside a CLI turn spawned by telegram-cli-gateway. Claude, Grok
and Pi carry their native session id on the command line; the gateway's state.json
maps that id to a session and the bot entrance it is talking through. Codex runs
as one shared app-server, so it is attributed only when exactly one Codex turn is
in flight. Anything unclear falls back to the default bot.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CLI_NAMES = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "pi": "Pi"}
SESSION_FLAGS = ("--resume", "--session-id", "--session")


def _ancestors(pid: int) -> list[list[str]]:
    chain: list[list[str]] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(chain) < 40:
        seen.add(pid)
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        chain.append([part.decode(errors="replace") for part in argv if part])
        # The command name in stat is parenthesised and may contain spaces.
        pid = int(stat.rsplit(")", 1)[1].split()[1])
    return chain


def _identify(argv: list[str]) -> tuple[str | None, str | None]:
    """(cli, native session id) for one process's argv."""
    if not argv:
        return None, None
    program = Path(argv[0]).name
    if program in {"node", "bun", "deno", "python", "python3"} and len(argv) > 1:
        program = Path(argv[1]).name
    cli = program if program in CLI_NAMES else None
    if cli is None:
        return None, None
    session_id = None
    for index, part in enumerate(argv[:-1]):
        if part in SESSION_FLAGS:
            session_id = argv[index + 1]
    return cli, session_id


def detect(gateway_dir: Path, pid: int | None = None) -> tuple[str | None, str | None]:
    """Return (bot_key, cli display name); either may be None."""
    cli = native_id = None
    for argv in _ancestors(pid or os.getpid()):
        found_cli, found_id = _identify(argv)
        if found_cli:
            cli, native_id = found_cli, found_id
            break
    if cli is None:
        return None, None
    try:
        state: dict[str, Any] = json.loads((gateway_dir / ".runtime" / "state.json").read_text())
    except (OSError, ValueError):
        return None, CLI_NAMES[cli]
    sessions: dict[str, Any] = state.get("sessions") or {}
    in_flight: dict[str, Any] = state.get("in_flight") or {}
    session_id = None
    if native_id:
        session_id = next(
            (sid for sid, item in sessions.items() if item.get("external_id") == native_id), None
        )
    elif cli == "codex":
        running = [sid for sid in in_flight if (sessions.get(sid) or {}).get("cli") == "codex"]
        session_id = running[0] if len(running) == 1 else None
    bot_key = None
    if session_id:
        bot_key = (in_flight.get(session_id) or {}).get("bot_key")
        if bot_key is None:
            for chat_key, chat in (state.get("chats") or {}).items():
                if (chat or {}).get("current") == session_id:
                    bot_key = chat_key.split(":", 1)[0] if ":" in chat_key else "default"
                    break
    return bot_key, CLI_NAMES[cli]
