from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config


class SessionError(RuntimeError):
    pass


OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_terminal_output(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = OSC_RE.sub("", text)
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Apply backspaces instead of leaking terminal redraw control characters.
    rebuilt: list[str] = []
    for char in text:
        if char == "\b":
            if rebuilt and rebuilt[-1] != "\n":
                rebuilt.pop()
        else:
            rebuilt.append(char)
    text = "".join(rebuilt)
    text = CONTROL_RE.sub("", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


@dataclass
class CliSession:
    session_id: str
    cli: str
    cwd: str
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

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CliSession":
        normalized = dict(value)
        normalized.setdefault("turn_count", 0)
        normalized.setdefault("display_name", None)
        normalized.setdefault("archived", False)
        normalized.setdefault("model", None)
        normalized.setdefault("effort", None)
        return cls(**normalized)

    @property
    def label(self) -> str:
        return self.display_name or self.session_id


class SessionManager:
    def __init__(self, config: Config):
        self.config = config
        self.runtime_dir = config.runtime_dir
        self.logs_dir = self.runtime_dir / "logs"
        self.state_path = self.runtime_dir / "state.json"
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.runtime_dir, 0o700)
        os.chmod(self.logs_dir, 0o700)
        self._lock = threading.RLock()
        self._state = self._load_state()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "sessions": {},
            "chats": {},
            "telegram_offset": None,
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
        data.setdefault("message_routes", {})
        data.setdefault("artifacts", {})
        data.setdefault("in_flight", {})
        return data

    def _save_state(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.state_path)

    def _tmux(
        self,
        *args: str,
        check: bool = True,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = ["tmux", "-L", self.config.tmux_socket_name, *args]
        try:
            return subprocess.run(
                command,
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=check,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise SessionError("tmux is not installed or not in PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise SessionError("tmux command timed out") from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", errors="replace").strip()
            raise SessionError(detail or "tmux command failed") from exc

    def _session_from_state(self, session_id: str) -> CliSession | None:
        raw = self._state["sessions"].get(session_id)
        return CliSession.from_dict(raw) if raw else None

    def is_alive(self, session: CliSession) -> bool:
        if session.backend != "tmux":
            return bool(session.external_id)
        result = self._tmux("has-session", "-t", session.tmux_name, check=False)
        return result.returncode == 0

    def create(self, cli: str, cwd: Path, chat_id: int) -> CliSession:
        if cli not in self.config.cli_commands:
            raise SessionError(f"unknown CLI: {cli}")

        with self._lock:
            for _ in range(10):
                session_id = f"{cli}-{secrets.token_hex(2)}"
                if session_id not in self._state["sessions"]:
                    break
            else:
                raise SessionError("could not allocate a unique session ID")

            tmux_name = f"tcg_{session_id.replace('-', '_')}"
            log_path = self.logs_dir / f"{session_id}.log"
            log_path.touch(mode=0o600, exist_ok=False)
            os.chmod(log_path, 0o600)

            try:
                self._tmux("new-session", "-d", "-s", tmux_name, "-c", str(cwd))
                pipe_command = f"cat >> {shlex.quote(str(log_path))}"
                self._tmux("pipe-pane", "-o", "-t", f"{tmux_name}:0.0", pipe_command)
                cli_command = "exec " + shlex.join(self.config.cli_commands[cli])
                self._tmux("send-keys", "-t", f"{tmux_name}:0.0", "-l", cli_command)
                self._tmux("send-keys", "-t", f"{tmux_name}:0.0", "Enter")
            except Exception:
                self._tmux("kill-session", "-t", tmux_name, check=False)
                log_path.unlink(missing_ok=True)
                raise

            session = CliSession(
                session_id=session_id,
                cli=cli,
                cwd=str(cwd),
                tmux_name=tmux_name,
                log_path=str(log_path),
                chat_id=chat_id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._state["sessions"][session_id] = asdict(session)
            self.switch(chat_id, session_id)
            self._save_state()
            return session

    def create_virtual(
        self,
        cli: str,
        cwd: Path,
        chat_id: int,
        backend: str,
        external_id: str,
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
            self.switch(chat_id, session_id)
            self._save_state()
            return session

    def create_headless(self, cli: str, cwd: Path, chat_id: int) -> CliSession:
        return self.create_virtual(
            cli,
            cwd,
            chat_id,
            backend="headless-json",
            external_id=str(uuid.uuid4()),
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
        """给 headless 会话换一个新的 external_id 并重置 turn_count：
        下次 start_turn 会以首轮模式开全新上下文（/clear 用）。"""
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            session.external_id = str(uuid.uuid4())
            session.turn_count = 0
            self._state["sessions"][session_id] = asdict(session)
            self._save_state()
            return str(session.external_id)

    def update_backend(self, session_id: str, backend: str, external_id: str) -> CliSession:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session:
                raise SessionError(f"session not found: {session_id}")
            if session.backend == "tmux" and session.tmux_name:
                self._tmux("kill-session", "-t", session.tmux_name, check=False)
            session.backend = backend
            session.external_id = external_id
            session.tmux_name = ""
            session.log_path = ""
            session.cursor = 0
            self._state["sessions"][session_id] = asdict(session)
            self._save_state()
            return session

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

    def current(self, chat_id: int) -> CliSession | None:
        with self._lock:
            chat = self._state["chats"].get(str(chat_id), {})
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

    def switch(self, chat_id: int, session_id: str) -> CliSession:
        with self._lock:
            session = self._session_from_state(session_id)
            if not session or session.chat_id != chat_id:
                raise SessionError(f"session not found: {session_id}")
            if session.archived:
                raise SessionError(f"session is archived: {session_id}")
            chat = self._state["chats"].setdefault(str(chat_id), {"current": None, "history": []})
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

    def bind_message(self, chat_id: int, message_id: int, session_id: str) -> None:
        with self._lock:
            routes = self._state.setdefault("message_routes", {})
            routes[f"{chat_id}:{message_id}"] = session_id
            if len(routes) > 2000:
                for key in list(routes)[: len(routes) - 2000]:
                    routes.pop(key, None)
            self._save_state()

    def session_for_message(self, chat_id: int, message_id: int) -> CliSession | None:
        with self._lock:
            session_id = self._state.get("message_routes", {}).get(f"{chat_id}:{message_id}")
            session = self._session_from_state(session_id) if session_id else None
            if not session or session.chat_id != chat_id or session.archived:
                return None
            return session

    def register_artifact(self, chat_id: int, session_id: str, path: Path) -> str:
        with self._lock:
            token = secrets.token_hex(6)
            artifacts = self._state.setdefault("artifacts", {})
            artifacts[token] = {
                "chat_id": chat_id,
                "session_id": session_id,
                "path": str(path),
            }
            if len(artifacts) > 500:
                for key in list(artifacts)[: len(artifacts) - 500]:
                    artifacts.pop(key, None)
            self._save_state()
            return token

    def set_in_flight(self, session_id: str, chat_id: int, turn_id: str) -> None:
        """记录一个正在执行的任务；网关重启后用它报告中断，而不是留下"运行中"假象。"""
        with self._lock:
            self._state["in_flight"][session_id] = {
                "chat_id": chat_id,
                "turn_id": turn_id,
            }
            self._save_state()

    def clear_in_flight(self, session_id: str, turn_id: str | None = None) -> bool:
        """清除进行中标记；返回是否确实清除了某个任务。"""
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
        """返回会话当前未完成任务的 turn_id；无则返回 None。"""
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

    def resolve_artifact(self, chat_id: int, token: str) -> tuple[CliSession, Path] | None:
        with self._lock:
            raw = self._state.get("artifacts", {}).get(token)
            if not isinstance(raw, dict) or int(raw.get("chat_id", -1)) != chat_id:
                return None
            session = self._session_from_state(str(raw.get("session_id", "")))
            path = raw.get("path")
            if not session or not isinstance(path, str):
                return None
            return session, Path(path)

    def back(self, chat_id: int) -> CliSession | None:
        with self._lock:
            chat = self._state["chats"].setdefault(str(chat_id), {"current": None, "history": []})
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

    def send_text(self, session: CliSession, text: str) -> None:
        if session.backend != "tmux":
            raise SessionError(f"session backend does not accept tmux input: {session.backend}")
        if not self.is_alive(session):
            raise SessionError(f"session is no longer running: {session.session_id}")
        safe_text = text.replace("\x00", "")
        buffer_name = f"tcg_{secrets.token_hex(4)}"
        self._tmux("load-buffer", "-b", buffer_name, "-", input_data=safe_text.encode("utf-8"))
        self._tmux(
            "paste-buffer",
            "-d",
            "-b",
            buffer_name,
            "-t",
            f"{session.tmux_name}:0.0",
        )
        self._tmux("send-keys", "-t", f"{session.tmux_name}:0.0", "Enter")

    def interrupt(self, session: CliSession) -> None:
        if session.backend != "tmux":
            raise SessionError(f"session backend does not accept tmux interrupts: {session.backend}")
        if not self.is_alive(session):
            raise SessionError(f"session is no longer running: {session.session_id}")
        self._tmux("send-keys", "-t", f"{session.tmux_name}:0.0", "C-c")

    def stop(self, session: CliSession) -> None:
        with self._lock:
            if session.backend == "tmux" and session.tmux_name:
                self._tmux("kill-session", "-t", session.tmux_name, check=False)
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

    def read_new_output(self, session: CliSession) -> str:
        if session.backend != "tmux":
            return ""
        with self._lock:
            current = self._session_from_state(session.session_id)
            if not current:
                return ""
            log_path = Path(current.log_path)
            if not log_path.exists():
                return ""
            size = log_path.stat().st_size
            if current.cursor > size:
                current.cursor = 0
            with log_path.open("rb") as handle:
                handle.seek(current.cursor)
                raw = handle.read(self.config.output_max_bytes)
                current.cursor = handle.tell()
            self._state["sessions"][current.session_id] = asdict(current)
            self._save_state()
        return clean_terminal_output(raw)

    def get_telegram_offset(self) -> int | None:
        with self._lock:
            value = self._state.get("telegram_offset")
            return int(value) if value is not None else None

    def set_telegram_offset(self, offset: int) -> None:
        with self._lock:
            self._state["telegram_offset"] = offset
            self._save_state()
