from __future__ import annotations

import json
import os
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .usage import merge_usage


_ANY_USAGE_MODEL = object()


class SessionError(RuntimeError):
    pass


@dataclass
class CliSession:
    session_id: str
    cli: str
    cwd: str
    # tmux_name, log_path and cursor belonged to the removed tmux backend. They
    # are still written because builds before its removal require tmux_name and
    # log_path when loading state.json; drop them once rollback past that change
    # is no longer needed.
    tmux_name: str
    log_path: str
    chat_id: int
    created_at: str
    backend: str = "tmux"
    external_id: str | None = None
    cursor: int = 0
    turn_count: int = 0
    display_name: str | None = None
    archived: bool = False
    model: str | None = None
    effort: str | None = None
    last_completed_message_id: int | None = None
    last_completed_message_ids: dict[str, int] | None = None
    usage: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CliSession":
        normalized = dict(value)
        normalized.setdefault("turn_count", 0)
        normalized.setdefault("display_name", None)
        normalized.setdefault("archived", False)
        normalized.setdefault("model", None)
        normalized.setdefault("effort", None)
        normalized.setdefault("last_completed_message_id", None)
        normalized.setdefault("last_completed_message_ids", None)
        normalized.setdefault("usage", None)
        return cls(**normalized)

    @property
    def label(self) -> str:
        return self.display_name or self.session_id


class SessionManager:
    def __init__(self, config: Config):
        self.config = config
        self.runtime_dir = config.runtime_dir
        self.state_path = self.runtime_dir / "state.json"
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.runtime_dir, 0o700)
        self._lock = threading.RLock()
        self._state = self._load_state()
        self._unsaved = False

    def _empty_state(self) -> dict[str, Any]:
        return {
            "sessions": {},
            "chats": {},
            "telegram_offset": None,
            "telegram_offsets": {},
            "message_routes": {},
            "artifacts": {},
            "in_flight": {},
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"cannot read state file: {exc}") from exc
        if not isinstance(data, dict):
            raise SessionError("state file root must be an object")
        data.setdefault("sessions", {})
        data.setdefault("chats", {})
        data.setdefault("telegram_offset", None)
        data.setdefault("telegram_offsets", {})
        data.setdefault("message_routes", {})
        data.setdefault("artifacts", {})
        data.setdefault("in_flight", {})
        # One-time compatibility for sessions created before the result pointer
        # existed. Only idle legacy sessions are inferred; all new writes carry
        # the field explicitly and therefore never repeat this heuristic.
        for session_id, raw_session in data["sessions"].items():
            if (
                not isinstance(raw_session, dict)
                or "last_completed_message_id" in raw_session
            ):
                continue
            latest_message_id: int | None = None
            if session_id not in data["in_flight"]:
                chat_id = str(raw_session.get("chat_id", ""))
                for route, routed_session_id in data["message_routes"].items():
                    route_chat_id, separator, route_message_id = str(route).rpartition(":")
                    if (
                        routed_session_id == session_id
                        and separator
                        and route_chat_id == chat_id
                        and route_message_id.isdigit()
                    ):
                        candidate = int(route_message_id)
                        latest_message_id = max(latest_message_id or candidate, candidate)
            raw_session["last_completed_message_id"] = latest_message_id
        return data

    def _save_state(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.state_path)
        self._unsaved = False

    def flush(self) -> None:
        """Write usage snapshots that were kept in memory; called on shutdown."""
        with self._lock:
            if self._unsaved:
                self._save_state()

    def _session_from_state(self, session_id: str) -> CliSession | None:
        raw = self._state["sessions"].get(session_id)
        return CliSession.from_dict(raw) if raw else None

    def is_alive(self, session: CliSession) -> bool:
        return bool(session.external_id)

    @staticmethod
    def _entrance_key(chat_id: int, bot_key: str) -> str:
        return str(chat_id) if bot_key == "default" else f"{bot_key}:{chat_id}"

    @classmethod
    def _message_key(cls, chat_id: int, message_id: int, bot_key: str) -> str:
        entrance = cls._entrance_key(chat_id, bot_key)
        return f"{entrance}:{message_id}"

    def create_virtual(
        self,
        cli: str,
        cwd: Path,
        chat_id: int,
        backend: str,
        external_id: str,
        bot_key: str = "default",
    ) -> CliSession:
        if cli not in self.config.cli_commands:
            raise SessionError(f"unknown CLI: {cli}")
        with self._lock:
            for _ in range(10):
                session_id = f"{cli}-{secrets.token_hex(2)}"
                if session_id not in self._state["sessions"]:
                    break
            else:
                raise SessionError("could not allocate a unique session ID")
            session = CliSession(
                session_id=session_id,
                cli=cli,
                cwd=str(cwd),
                tmux_name="",
                log_path="",
                chat_id=chat_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                backend=backend,
                external_id=external_id,
            )
            self._state["sessions"][session_id] = asdict(session)
            self.switch(chat_id, session_id, bot_key)
            self._save_state()
            return session

    def create_headless(
        self, cli: str, cwd: Path, chat_id: int, bot_key: str = "default"
    ) -> CliSession:
        return self.create_virtual(
            cli,
            cwd,
            chat_id,
            backend="headless-json",
            external_id=str(uuid.uuid4()),
            bot_key=bot_key,
        )

    def increment_turn_count(self, session_id: str) -> int:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            session.turn_count += 1
            self._state["sessions"][session_id] = asdict(session)
            self._save_state()
            return session.turn_count

    def rotate_external_id(self, session_id: str) -> str:
        """Give a headless session a new external_id and reset turn_count, so the
        next start_turn opens a fresh context in first-turn mode (used by /clear)."""
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            session.external_id = str(uuid.uuid4())
            session.turn_count = 0
            session.usage = None
            self._state["sessions"][session_id] = asdict(session)
            self._save_state()
            return str(session.external_id)

    def update_backend(self, session_id: str, backend: str, external_id: str) -> CliSession:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            session.backend = backend
            if session.external_id != external_id:
                session.usage = None
            session.external_id = external_id
            session.tmux_name = ""
            session.log_path = ""
            session.cursor = 0
            self._state["sessions"][session_id] = asdict(session)
            self._save_state()
            return session

    def update_usage(
        self, session_id: str, external_id: str | None, usage: dict[str, Any], *,
        only_if_missing: bool = False,
        expected_model: object = _ANY_USAGE_MODEL,
        deferred: bool = False,
    ) -> bool:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session or session.external_id != external_id:
                return False  # Ignore late reports from a cleared/deleted native session.
            if only_if_missing and "context_tokens" in (session.usage or {}):
                return False  # A live report or compaction won the race with history reads.
            if expected_model is not _ANY_USAGE_MODEL and (session.usage or {}).get("model") != expected_model:
                return False  # A window query must not attach the old model's capacity to a new model.
            session.usage = merge_usage(session.usage, usage)
            self._state["sessions"][session_id] = asdict(session)
            if deferred and usage.get("context_tokens", 0) is not None:
                # Live CLI streams report usage on every model request. Keep those
                # refreshes in memory; the next state write (turn end, routing,
                # shutdown) saves them. A compaction invalidation is always written
                # so a restart never shows the pre-compaction occupancy.
                self._unsaved = True
            else:
                self._save_state()
            return True

    def all_sessions(self) -> list[CliSession]:
        with self._lock:
            return [CliSession.from_dict(value) for value in self._state["sessions"].values()]

    def get(self, session_id: str) -> CliSession | None:
        with self._lock:
            return self._session_from_state(session_id)

    def find_by_external_id(self, external_id: str) -> CliSession | None:
        with self._lock:
            for value in self._state["sessions"].values():
                if value.get("external_id") == external_id:
                    return CliSession.from_dict(value)
        return None

    def list_for_chat(
        self, chat_id: int, alive_only: bool = False, include_archived: bool = False
    ) -> list[CliSession]:
        with self._lock:
            sessions = [
                CliSession.from_dict(value)
                for value in self._state["sessions"].values()
                if int(value["chat_id"]) == chat_id
                and (include_archived or not bool(value.get("archived", False)))
            ]
        sessions.sort(key=lambda item: item.created_at, reverse=True)
        if alive_only:
            sessions = [item for item in sessions if self.is_alive(item)]
        return sessions

    def current(self, chat_id: int, bot_key: str = "default") -> CliSession | None:
        with self._lock:
            chat = self._state["chats"].get(self._entrance_key(chat_id, bot_key), {})
            session_id = chat.get("current")
            return self._session_from_state(session_id) if session_id else None

    def current_sessions(self) -> list[CliSession]:
        with self._lock:
            session_ids = {
                chat.get("current") for chat in self._state["chats"].values() if chat.get("current")
            }
            return [
                session
                for session_id in session_ids
                if (session := self._session_from_state(session_id)) is not None
            ]

    def resolve(self, chat_id: int, target: str) -> CliSession | None:
        target = target.strip().lower()
        sessions = self.list_for_chat(chat_id, alive_only=True)
        for session in sessions:
            if session.session_id.lower() == target:
                return session
        cli_matches = [session for session in sessions if session.cli == target]
        return cli_matches[0] if cli_matches else None

    def switch(
        self, chat_id: int, session_id: str, bot_key: str = "default"
    ) -> CliSession:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session or session.chat_id != chat_id:
                raise SessionError(f"session not found: {session_id}")
            if session.archived:
                raise SessionError(f"session is archived: {session_id}")
            entrance = self._entrance_key(chat_id, bot_key)
            chat = self._state["chats"].setdefault(entrance, {"current": None, "history": []})
            previous = chat.get("current")
            if previous and previous != session_id:
                history = chat.setdefault("history", [])
                history.append(previous)
                del history[:-20]
            chat["current"] = session_id
            self._save_state()
            return session

    def rename(self, session_id: str, display_name: str | None) -> CliSession:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            cleaned = " ".join((display_name or "").split()).strip()
            if len(cleaned) > 40:
                raise SessionError("session name must be 40 characters or fewer")
            session.display_name = cleaned or None
            self._state["sessions"][session_id] = asdict(session)
            self._save_state()
            return session

    def set_model(self, session_id: str, model: str | None) -> CliSession:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            cleaned = model.strip() if model else None
            session.model = cleaned or None
            self._state["sessions"][session_id] = asdict(session)
            self._save_state()
            return session

    def set_effort(self, session_id: str, effort: str | None) -> CliSession:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            cleaned = effort.strip() if effort else None
            session.effort = cleaned or None
            self._state["sessions"][session_id] = asdict(session)
            self._save_state()
            return session

    def set_archived(self, session_id: str, archived: bool) -> CliSession:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            session.archived = archived
            self._state["sessions"][session_id] = asdict(session)
            if archived:
                for chat in self._state["chats"].values():
                    chat["history"] = [
                        item for item in chat.get("history", []) if item != session_id
                    ]
                    if chat.get("current") == session_id:
                        chat["current"] = None
            self._save_state()
            return session

    def bind_message(
        self, chat_id: int, message_id: int, session_id: str, bot_key: str = "default"
    ) -> None:
        with self._lock:
            routes = self._state.setdefault("message_routes", {})
            routes[self._message_key(chat_id, message_id, bot_key)] = session_id
            if len(routes) > 2000:
                for key in list(routes)[: len(routes) - 2000]:
                    routes.pop(key, None)
            self._save_state()

    def set_last_completed_message(
        self, session_id: str, message_id: int | None, bot_key: str = "default"
    ) -> CliSession:
        """Persist only Telegram's pointer to the last successful result."""
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            session.last_completed_message_id = (
                message_id if isinstance(message_id, int) and message_id > 0 else None
            ) if bot_key == "default" else session.last_completed_message_id
            pointers = dict(session.last_completed_message_ids or {})
            if isinstance(message_id, int) and message_id > 0:
                pointers[bot_key] = message_id
            else:
                pointers.pop(bot_key, None)
            session.last_completed_message_ids = pointers or None
            self._state["sessions"][session_id] = asdict(session)
            self._save_state()
            return session

    def get_last_completed_message(
        self, session_id: str, bot_key: str = "default"
    ) -> int | None:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                return None
            pointers = session.last_completed_message_ids or {}
            if bot_key in pointers:
                return pointers[bot_key]
            return session.last_completed_message_id if bot_key == "default" else None

    def clear_last_completed_messages(self, session_id: str) -> CliSession:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            session.last_completed_message_id = None
            session.last_completed_message_ids = None
            self._state["sessions"][session_id] = asdict(session)
            self._save_state()
            return session

    def session_for_message(
        self, chat_id: int, message_id: int, bot_key: str = "default"
    ) -> CliSession | None:
        with self._lock:
            route = self._message_key(chat_id, message_id, bot_key)
            session_id = self._state.get("message_routes", {}).get(route)
            session = self._session_from_state(session_id) if session_id else None
            if not session or session.chat_id != chat_id or session.archived:
                return None
            return session

    def command_route_for_message(
        self, chat_id: int, message_id: int, bot_key: str = "default"
    ) -> str | None:
        """Return the turn_id behind a direct command result card.

        Command cards are routed through ``command_routes``, apart from CLI session
        cards: replying to a command card must not send text into the current CLI
        session. Entries are bounded and removed when the command ends.
        """
        with self._lock:
            route = self._message_key(chat_id, message_id, bot_key)
            turn_id = self._state.get("command_routes", {}).get(route)
            return str(turn_id) if turn_id else None

    def bind_command_message(
        self, chat_id: int, message_id: int, turn_id: str, bot_key: str = "default"
    ) -> None:
        with self._lock:
            routes = self._state.setdefault("command_routes", {})
            routes[self._message_key(chat_id, message_id, bot_key)] = turn_id
            if len(routes) > 2000:
                for key in list(routes)[: len(routes) - 2000]:
                    routes.pop(key, None)
            self._save_state()

    def unbind_command_messages(
        self, chat_id: int, bot_key: str, message_ids: list[int]
    ) -> None:
        with self._lock:
            routes = self._state.get("command_routes", {})
            if not routes:
                return
            for message_id in message_ids:
                routes.pop(self._message_key(chat_id, message_id, bot_key), None)
            self._save_state()

    def register_artifact(
        self,
        chat_id: int,
        session_id: str,
        path: Path,
        bot_key: str = "default",
        *,
        only_path: bool = False,
    ) -> str:
        with self._lock:
            token = secrets.token_hex(6)
            artifacts = self._state.setdefault("artifacts", {})
            artifacts[token] = {
                "chat_id": chat_id,
                "session_id": session_id,
                "path": str(path),
                "bot_key": bot_key,
                # An artifact over the send limit can only be returned as a path.
                "only_path": bool(only_path),
            }
            if len(artifacts) > 500:
                for key in list(artifacts)[: len(artifacts) - 500]:
                    artifacts.pop(key, None)
            self._save_state()
            return token

    def set_in_flight(
        self,
        session_id: str,
        chat_id: int,
        turn_id: str,
        bot_key: str = "default",
    ) -> None:
        """Record a running task so a gateway restart reports the interruption
        instead of leaving a stale "running" state."""
        with self._lock:
            self._state["in_flight"][session_id] = {
                "chat_id": chat_id,
                "turn_id": turn_id,
                "bot_key": bot_key,
            }
            self._save_state()

    def clear_in_flight(self, session_id: str, turn_id: str | None = None) -> bool:
        """Clear the in-flight marker; return whether a task was actually cleared."""
        with self._lock:
            current = self._state["in_flight"].get(session_id)
            if not current:
                return False
            if turn_id is not None and str(current.get("turn_id")) != turn_id:
                return False
            self._state["in_flight"].pop(session_id, None)
            self._save_state()
            return True

    def get_in_flight(self, session_id: str) -> str | None:
        """Return the turn_id of the session's unfinished task, or None."""
        with self._lock:
            current = self._state["in_flight"].get(session_id)
            return str(current["turn_id"]) if isinstance(current, dict) and current.get("turn_id") else None

    def stale_in_flight(self) -> list[tuple[str, int, str]]:
        with self._lock:
            return [
                (session_id, int(value["chat_id"]), str(value["turn_id"]))
                for session_id, value in self._state["in_flight"].items()
                if isinstance(value, dict) and "turn_id" in value
            ]

    def stale_in_flight_routes(self) -> list[tuple[str, int, str, str]]:
        with self._lock:
            return [
                (
                    session_id,
                    int(value["chat_id"]),
                    str(value["turn_id"]),
                    str(value.get("bot_key", "default")),
                )
                for session_id, value in self._state["in_flight"].items()
                if isinstance(value, dict) and "turn_id" in value
            ]

    def resolve_artifact(
        self, chat_id: int, token: str, bot_key: str = "default"
    ) -> tuple[CliSession | None, Path] | None:
        """Look up a local artifact by token and check it belongs to this chat and bot.

        The session may be None: direct command artifacts hang off a turn_id with no
        CLI session. chat_id + bot_key already establish ownership.
        """
        with self._lock:
            raw = self._state.get("artifacts", {}).get(token)
            if not isinstance(raw, dict) or int(raw.get("chat_id", -1)) != chat_id:
                return None
            if str(raw.get("bot_key", "default")) != bot_key:
                return None
            session = self._session_from_state(str(raw.get("session_id", "")))
            path = raw.get("path")
            if not isinstance(path, str):
                return None
            return session, Path(path)

    def back(self, chat_id: int, bot_key: str = "default") -> CliSession | None:
        with self._lock:
            entrance = self._entrance_key(chat_id, bot_key)
            chat = self._state["chats"].setdefault(entrance, {"current": None, "history": []})
            history = chat.setdefault("history", [])
            while history:
                candidate = history.pop()
                session = self._session_from_state(candidate)
                if session and self.is_alive(session):
                    current = chat.get("current")
                    if current and current != candidate:
                        history.insert(0, current)
                        del history[:-20]
                    chat["current"] = candidate
                    self._save_state()
                    return session
            self._save_state()
            return None

    def stop(self, session: CliSession) -> None:
        with self._lock:
            self._state["sessions"].pop(session.session_id, None)
            self._state["in_flight"].pop(session.session_id, None)
            self._state["message_routes"] = {
                key: value
                for key, value in self._state.get("message_routes", {}).items()
                if value != session.session_id
            }
            self._state["artifacts"] = {
                key: value
                for key, value in self._state.get("artifacts", {}).items()
                if not isinstance(value, dict) or value.get("session_id") != session.session_id
            }
            for chat in self._state["chats"].values():
                chat["history"] = [
                    item for item in chat.get("history", []) if item != session.session_id
                ]
                if chat.get("current") == session.session_id:
                    chat["current"] = None
            self._save_state()

    def get_telegram_offset(self, bot_key: str = "default") -> int | None:
        with self._lock:
            offsets = self._state.setdefault("telegram_offsets", {})
            value = offsets.get(bot_key)
            if value is None and bot_key == "default":
                value = self._state.get("telegram_offset")
            return int(value) if value is not None else None

    def set_telegram_offset(self, offset: int, bot_key: str = "default") -> None:
        with self._lock:
            self._state.setdefault("telegram_offsets", {})[bot_key] = offset
            if bot_key == "default":
                self._state["telegram_offset"] = offset
            self._save_state()
