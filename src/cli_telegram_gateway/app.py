from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import mimetypes
import re
import shlex
import signal
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

from .attachments import Attachment
from .claude_usage import read_claude_usage
from .codex_backend import CodexAppServer, CodexBackendError
from .commands import (
    CommandError,
    DirectCommand,
    DirectCommandRun,
    DirectCommandRunner,
    load_direct_commands,
)
from .config import Config, ConfigError
from .formatting import harden_rich_markdown, split_markdown, split_rich_markdown, stream_segment
from .headless_backend import HeadlessBackend, HeadlessBackendError
from .i18n import N_, join_items, set_language, tr
from .models import clear_listing_cache, list_efforts, list_models
from .sessions import CliSession, SessionError, SessionManager
from .telegram import TelegramClient, TelegramError
from .usage import codex_usage, context_lines, quota_lines, usage_lines


LOGGER = logging.getLogger("telegram-cli-gateway")
MAX_PENDING_INPUTS_PER_SESSION = 20
# Claude confirms a message within milliseconds mid-turn and in about 1.3 s while it
# still loads a 28 MB session (measured with Claude Code 2.1.289).
HEADLESS_STEER_CONFIRM_SECONDS = 30.0
MAX_PUBLISH_RETRY_SECONDS = 30.0
STREAM_SEGMENT_UNITS = 1500
MAX_AUTO_SENT_ARTIFACTS = 3
PHOTO_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # Telegram sendPhoto limit

# Incoming files land in different fields depending on how the client sent them
# (WAV/MP3 are often audio); all of them go to the CLI. Values are the type to use
# when mime_type is missing: video_note has no such field and voice is almost always
# OGG/Opus. A GIF animation also carries a document for old clients, so document comes
# first. Stickers are not forwarded.
TELEGRAM_FILE_FIELDS = {
    "document": "application/octet-stream",
    "audio": "application/octet-stream",
    "video": "video/mp4",
    "animation": "video/mp4",
    "voice": "audio/ogg",
    "video_note": "video/mp4",
}

# Clients split text over 4096 UTF-16 units into several messages with no grouping
# marker such as media_group_id. Telegram Desktop breaks between 2048 and 4096 at a
# paragraph, newline or space; Android cuts hard at 4096. The server strips whitespace
# around each part, and parts often arrive in separate getUpdates batches. They are
# joined back into one input, or the later parts would become a steer or a queued turn.
TELEGRAM_TEXT_MAX_CHARS = 4096
TEXT_FRAGMENT_MIN_CHARS = 2000
TEXT_FRAGMENT_MAX_PARTS = 24
# The joined text reaches claude/grok/pi through argv; Linux caps one argument at 128 KiB.
TEXT_FRAGMENT_MAX_TOTAL_BYTES = 100_000
# When a batch ends like an unfinished part, the poller waits for more parts and dispatches
# after this much quiet or the total cap.
TEXT_FRAGMENT_POLL_SECONDS = 0.25
TEXT_FRAGMENT_QUIET_SECONDS = 1.5
TEXT_FRAGMENT_MAX_WAIT_SECONDS = 10.0

# Session state for display, driven by events; the busy check stays authoritative for logic.
STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_BLOCKED = "blocked"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"

STATE_LABELS = {
    STATE_WORKING: N_("🟡 Running"),
    STATE_BLOCKED: N_("🔴 Waiting"),
    STATE_FAILED: N_("⚫ Last run failed"),
    STATE_INTERRUPTED: N_("🔴 Interrupted, awaiting recovery"),
    STATE_IDLE: N_("🟢 Idle"),
}

# Sent to the CLI as new user input on recovery (headless backends resume by turn_count).
RESUME_PROMPT = (
    "[Task recovery] Your previous task did not finish because the gateway restarted or "
    "the process was interrupted. Review the earlier context first, then continue the "
    "unfinished work. If the task actually finished, state the result directly and do not "
    "repeat steps that already ran or may have side effects."
)

HELP_TEXT = N_("""Remote CLI gateway

/new [cli] [dir]       New session; without arguments, pick a CLI
/use <session-id|cli>  Switch; creates a session if that CLI has none
/back                  Return to the previous session
/sessions              List sessions
/tasks                 Show running tasks and queues
/tgstats               Show Telegram write and rate-limit stats
/where                 Show the current session and directory
/status                Show CLI status, context usage and account quota
/context               Show context usage and space left
/interrupt             Interrupt the current task
/resume [session]      Continue the last interrupted task
/cancel [session]      Give up the last interrupted task
/compact               Compact the current session's context (Codex, Pi)
/clear                 Start a new conversation and clear the context
/reload [current|all]  Reload the current session's CLI or all CLIs
/stop [session-id|cli] Stop a session
/rename [session] <name>  Rename a session
/archive [session]     Archive a session
/delete [session]      Delete a session (asks to confirm)
/model [name]          Show or switch the current session's model
/effort [level]        Show or switch the reasoning effort
/send <text>           Send text to the current CLI explicitly
/commands              List direct command extensions
/help                  Show this help

CLIs enabled for this bot: {clis}.
Plain text, files and images go straight to the current session. All CLIs run without approval prompts.
{extensions}""")

BOT_COMMANDS = (
    ("new", N_("Create a CLI session")),
    ("use", N_("Switch CLI or session")),
    ("back", N_("Return to the previous session")),
    ("sessions", N_("List sessions")),
    ("tasks", N_("Show the task queue")),
    ("tgstats", N_("Show Telegram API stats")),
    ("where", N_("Show the current session")),
    ("status", N_("Show CLI status and quota")),
    ("context", N_("Show context usage")),
    ("interrupt", N_("Interrupt the current task")),
    ("resume", N_("Continue the last interrupted task")),
    ("cancel", N_("Give up the last interrupted task")),
    ("compact", N_("Compact the session context")),
    ("clear", N_("Start a new conversation")),
    ("reload", N_("Reload CLI backends")),
    ("stop", N_("Stop a session")),
    ("rename", N_("Rename a session")),
    ("archive", N_("Archive a session")),
    ("delete", N_("Delete a session")),
    ("model", N_("Switch the session model")),
    ("effort", N_("Switch the reasoning effort")),
    ("commands", N_("List direct command extensions")),
    ("help", N_("Show help")),
)

BUILTIN_COMMANDS = frozenset(name for name, _description in BOT_COMMANDS)


@dataclass
class AssistantMessage:
    phase: str | None = None
    parts: list[str] = field(default_factory=list)
    completed: bool = False


@dataclass
class TurnView:
    session: CliSession
    turn_id: str
    bot_key: str = "default"
    started_at: float = field(default_factory=time.monotonic)
    parts: list[str] = field(default_factory=list)
    assistant_messages: dict[str, AssistantMessage] = field(default_factory=dict)
    progress_offset: int = 0
    progress_page: int = 1
    progress_closed: bool = False
    steer_notices: deque[tuple[int, int]] = field(default_factory=deque)
    revision: int = 0
    command: str = ""
    command_output: str = ""
    status: str = "running"
    error: str = ""
    notice: str = ""
    message_id: int | None = None
    message_ids: list[int] = field(default_factory=list)
    last_edit: float = 0.0
    dirty: bool = True
    published: bool = False
    rich_mode: bool = True
    artifacts: list[Path] = field(default_factory=list)
    artifact_tokens: dict[str, str] = field(default_factory=dict)
    auto_sent: list[str] = field(default_factory=list)
    steered: int = 0
    publish_failures: int = 0
    next_publish_at: float = 0.0
    resumable: bool = False
    live: bool = False
    stale_message_ids: list[int] = field(default_factory=list)
    # Frozen at turn start. /model and /effort apply to the next turn, so a
    # later change must not relabel this card.
    model_label: str = ""
    effort_label: str = ""


@dataclass(frozen=True)
class PendingInput:
    chat_id: int
    text: str
    attachments: tuple[Attachment, ...] = ()
    bot_key: str = "default"


def _command_help_section(commands: tuple[DirectCommand, ...]) -> str:
    if not commands:
        return tr("No direct command extensions are installed. Put an extension directory in extensions/ or set COMMAND_DIR.")
    lines = [tr("Direct command extensions"), ""]
    lines.extend(command.help_line() for command in commands)
    lines.append("")
    lines.append(tr("These commands run on this machine without calling an AI or entering session context."))
    return "\n".join(lines)


def _utf16_len(text: str) -> int:
    """Telegram measures text in UTF-16 code units; emoji and similar count as two."""
    return len(text.encode("utf-16-le")) // 2


def _file_field(message: dict[str, Any]) -> str | None:
    """The message field that carries a single file; photos (a list of sizes) are handled apart."""
    return next(
        (field for field in TELEGRAM_FILE_FIELDS if isinstance(message.get(field), dict)),
        None,
    )


def _fragment_separator(previous: str, following: str, multiline: bool) -> str:
    """Restore the whitespace the server stripped at a split point.

    A part of exactly 4096 units is a hard cut (Android) that may fall mid-word, so it is
    joined as is. Other parts were split at whitespace by the client (Desktop prefers
    paragraphs and newlines): multi-line text gets a newline back, single-line text a space.
    """
    if previous[-1:].isspace() or following[:1].isspace():
        return ""
    if _utf16_len(previous) >= TELEGRAM_TEXT_MAX_CHARS:
        return ""
    return "\n" if multiline else " "


class GatewayApp:
    def __init__(self, config: Config):
        self.config = config
        set_language(config.language)
        self._telegrams: dict[str, TelegramClient] = {}
        for bot_key, token in config.telegram_bots:
            metrics_name = (
                "telegram-metrics.json"
                if bot_key == "default"
                else f"telegram-metrics-{bot_key}.json"
            )
            self._telegrams[bot_key] = TelegramClient(
                token,
                config.runtime_dir / metrics_name,
                local_api_url=config.telegram_local_api_url,
            )
        self._default_telegram = next(iter(self._telegrams.values()))
        self.sessions = SessionManager(config)
        self.codex = CodexAppServer(
            config.cli_commands["codex"],
            self._on_codex_notification,
            self._on_codex_server_request,
            self._on_codex_disconnect,
        )
        self.headless = HeadlessBackend(config.cli_commands, self._on_headless_event)
        self.stop_event = threading.Event()
        self._lock_handle = None
        self._state_lock = threading.RLock()
        self._publish_lock = threading.RLock()
        self._dispatch_lock = threading.Lock()
        self._backend_reload_lock = threading.RLock()
        self._bot_local = threading.local()
        self._codex_active_turns: dict[str, str] = {}
        self._turn_claims: set[str] = set()
        self._drain_threads: dict[str, int] = {}
        self._pending_codex_compactions: dict[str, str] = {}
        self._codex_compaction_turns: dict[tuple[str, str], str] = {}
        self._turns: dict[tuple[str, str], TurnView] = {}
        self.commands = load_direct_commands(
            config,
            reserved=BUILTIN_COMMANDS,
            warn=lambda message: LOGGER.warning("%s", message),
        )
        self.direct_commands = DirectCommandRunner(config, self.commands)
        # CLI output can precede the start RPC response. Buffer it until the
        # turn ID, reply route and recovery record have all been registered.
        self._starting_events: dict[str, list[tuple[str, tuple[Any, ...]]]] = {}
        self._finished_turns: deque[tuple[str, str]] = deque(maxlen=1000)
        self._queues: dict[str, deque[PendingInput]] = {}
        self._session_states: dict[str, str] = {}
        self._interrupted_sessions: set[str] = set()
        self._last_completed_results: dict[str, str] = {}
        # Artifact tokens whose upload is running, so a repeated tap does not send twice.
        self._artifact_uploads: set[str] = set()

    def _active_bot_key(self) -> str:
        bot_key = getattr(self._bot_local, "bot_key", "default")
        return bot_key if bot_key in self._telegrams else "default"

    def _active_user_id(self) -> int:
        return int(getattr(self._bot_local, "user_id", 0) or 0)

    @contextmanager
    def _bot_scope(self, bot_key: str):
        previous = getattr(self._bot_local, "bot_key", None)
        self._bot_local.bot_key = bot_key
        try:
            yield
        finally:
            if previous is None:
                self._bot_local.__dict__.pop("bot_key", None)
            else:
                self._bot_local.bot_key = previous

    def _telegram(self, bot_key: str | None = None) -> TelegramClient:
        return self._telegrams[bot_key or self._active_bot_key()]

    @property
    def telegram(self) -> TelegramClient:
        """Current entrance client; retained for focused tests and helpers."""
        return self._telegram()

    def _run_in_background(self, name: str, target: Callable[..., None], *args: Any) -> None:
        """Run a slow, self-contained action off the update dispatch thread.

        Updates from every Bot entrance are dispatched one at a time, so an upload,
        a native status probe, a model listing or a Pi compaction run inline would
        hold up /interrupt and every other chat. The worker keeps the Bot scope.
        """
        bot_key = self._active_bot_key()

        def run() -> None:
            with self._bot_scope(bot_key):
                try:
                    target(*args)
                except Exception:
                    LOGGER.exception("background action %s failed", name)

        threading.Thread(target=run, name=name, daemon=True).start()

    def _current_session(self, chat_id: int, bot_key: str | None = None) -> CliSession | None:
        return self.sessions.current(chat_id, bot_key or self._active_bot_key())

    def _effective_model(self, session: CliSession) -> str | None:
        return session.model or self.config.default_model_for(session.cli)

    def _effective_effort(self, session: CliSession) -> str | None:
        return session.effort or self.config.default_effort_for(session.cli)

    def _model_display(self, session: CliSession) -> str:
        if session.model:
            return session.model
        configured = self.config.default_model_for(session.cli)
        return tr("{value} (gateway default)", value=configured) if configured else tr("CLI default")

    def _effort_display(self, session: CliSession) -> str:
        if session.effort:
            return session.effort
        configured = self.config.default_effort_for(session.cli)
        return tr("{value} (gateway default)", value=configured) if configured else tr("CLI default")

    def _model_header_label(self, session: CliSession) -> str:
        if session.model:
            return session.model
        configured = self.config.default_model_for(session.cli)
        return f"{configured} (gateway default)" if configured else "CLI default"

    def _effort_header_label(self, session: CliSession) -> str:
        if session.effort:
            return session.effort
        configured = self.config.default_effort_for(session.cli)
        return f"{configured} (gateway default)" if configured else "CLI default"

    def _switch_session(
        self, chat_id: int, session_id: str, bot_key: str | None = None
    ) -> CliSession:
        with self._publish_lock:
            return self.sessions.switch(chat_id, session_id, bot_key or self._active_bot_key())

    def _back_session(self, chat_id: int) -> CliSession | None:
        with self._publish_lock:
            return self.sessions.back(chat_id, self._active_bot_key())

    def _acquire_singleton_lock(self) -> None:
        lock_path = self.config.runtime_dir / "gateway.lock"
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_handle = lock_path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_handle.close()
            self._lock_handle = None
            raise RuntimeError("another telegram-cli-gateway process is already running") from exc

    def _send(self, chat_id: int, text: str, bot_key: str | None = None) -> None:
        try:
            self._telegram(bot_key).send_message(chat_id, text)
        except TelegramError:
            LOGGER.exception("could not send Telegram message to chat %s", chat_id)

    def _telegram_stats_text(self) -> str:
        snapshot = self._telegram().metrics_snapshot()
        today = snapshot["today"]
        recent = snapshot["last_7_days"]
        methods = today.get("methods", {})
        ranked_methods = sorted(
            (
                (
                    method,
                    sum(int(count) for count in counts.values()),
                )
                for method, counts in methods.items()
            ),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        lines = [
            tr("Telegram write stats (UTC {day})", day=snapshot["utc_day"]),
            tr(
                "Successful writes today: {new} new messages · {edits} edits · {other} other",
                new=today["new_messages"], edits=today["message_edits"], other=today["other_writes"],
            ),
            tr(
                "Requests today: {ok} succeeded · {failed} failed · {limited} rate-limited (429) · {deferred} deferred locally",
                ok=today["successful_requests"], failed=today["failed_requests"],
                limited=today["rate_limited"], deferred=today["local_deferrals"],
            ),
            tr(
                "Successful writes, last 7 days: {new} new messages · {edits} edits · {other} other · {limited} rate-limited (429)",
                new=recent["new_messages"], edits=recent["message_edits"],
                other=recent["other_writes"], limited=recent["rate_limited"],
            ),
            tr("Current flood wait: {seconds} s", seconds=snapshot["flood_wait_seconds"]),
        ]
        if ranked_methods:
            lines.append(tr(
                "Methods today: {methods}",
                methods=" · ".join(f"{method} {count}" for method, count in ranked_methods),
            ))
        lines.append(tr("Counts only the gateway's own write calls; this is not Telegram's official daily quota."))
        return "\n".join(lines)

    def _current_or_reply(self, chat_id: int) -> CliSession | None:
        session = self._current_session(chat_id)
        if not session:
            self._send(chat_id, tr("No active session. Use /new to pick a CLI and create one."))
            return None
        if session.cli not in self.config.enabled_clis:
            self._send(chat_id, tr("{cli} is not enabled for this bot; create a session with an enabled CLI.", cli=session.cli))
            return None
        if not self.sessions.is_alive(session):
            self._send(chat_id, tr("Session {session} has exited; create or switch to another session.", session=session.session_id))
            return None
        return session

    def _new_session(self, chat_id: int, cli: str, requested_cwd: str | None) -> None:
        cli = cli.lower()
        if cli not in self.config.enabled_clis:
            self._send(chat_id, tr(
                "{cli} is not enabled for this bot. Available: {clis}",
                cli=cli, clis=join_items(self.config.enabled_clis),
            ))
            return
        try:
            cwd = self.config.resolve_workdir(requested_cwd)
            if cli == "codex":
                thread_id = self.codex.start_thread(
                    str(cwd), model=self.config.default_model_for(cli)
                )
                session = self.sessions.create_virtual(
                    cli,
                    cwd,
                    chat_id,
                    backend="codex-app-server",
                    external_id=thread_id,
                    bot_key=self._active_bot_key(),
                )
            else:
                session = self.sessions.create_headless(
                    cli, cwd, chat_id, self._active_bot_key()
                )
        except (CodexBackendError, ConfigError, SessionError) as exc:
            self._send(chat_id, tr("Could not create the session: {error}", error=exc))
            return
        self._send(
            chat_id,
            tr(
                "Created and switched to {session}\nCLI: {cli}\nDirectory: {cwd}\nModel: {model}"
                "\nReasoning effort: {effort}\nPermissions: no approval prompts",
                session=session.session_id, cli=session.cli, cwd=session.cwd,
                model=self._model_display(session), effort=self._effort_display(session),
            ),
        )

    def _send_new_session_picker(self, chat_id: int) -> None:
        buttons = [
            [
                {"text": cli, "callback_data": f"new:{cli}"}
                for cli in self.config.enabled_clis[index : index + 2]
            ]
            for index in range(0, len(self.config.enabled_clis), 2)
        ]
        try:
            self.telegram.send_message(
                chat_id,
                tr("Pick a CLI for the new session:\nDefault directory: {cwd}", cwd=self.config.default_workdir),
                reply_markup={"inline_keyboard": buttons},
            )
        except TelegramError:
            LOGGER.exception("could not send new-session picker to chat %s", chat_id)

    def _send_reload_picker(self, chat_id: int) -> None:
        current = self._current_session(chat_id)
        buttons: list[list[dict[str, str]]] = []
        if current and current.cli in self.config.enabled_clis:
            buttons.append(
                [{
                    "text": tr("Current session ({cli})", cli=current.cli),
                    "callback_data": f"reload:{current.session_id}",
                }]
            )
        buttons.append([{"text": tr("All CLIs"), "callback_data": "reload:all"}])
        detail = (
            tr("Current: {label} · {cli}", label=current.label, cli=current.cli) if current
            else tr("No active session.")
        ) + "\n"
        try:
            self.telegram.send_message(
                chat_id,
                tr("Choose what to reload:") + "\n"
                + detail
                + tr(
                    "Running tasks are not interrupted. Codex is a shared backend, so reloading one "
                    "Codex session resumes every Codex session. This does not reread .env or the gateway source."
                ),
                reply_markup={"inline_keyboard": buttons},
            )
        except TelegramError:
            LOGGER.exception("could not send reload picker to chat %s", chat_id)

    def _parse_args(self, chat_id: int, raw_args: str) -> list[str] | None:
        try:
            return shlex.split(raw_args)
        except ValueError as exc:
            self._send(chat_id, tr("Invalid arguments: {error}", error=exc))
            return None

    def _handle_command(self, chat_id: int, command: str, raw_args: str) -> None:
        if command in ("start", "help"):
            self._send(
                chat_id,
                tr(
                    HELP_TEXT,
                    clis=join_items(self.config.enabled_clis),
                    extensions=_command_help_section(tuple(self.commands.values())),
                ),
            )
            return
        if command == "new":
            args = self._parse_args(chat_id, raw_args)
            if args is None:
                return
            if not args:
                self._send_new_session_picker(chat_id)
                return
            if len(args) > 2:
                self._send(chat_id, tr("Usage: /new <{clis}> [dir]", clis="|".join(self.config.enabled_clis)))
                return
            self._new_session(chat_id, args[0], args[1] if len(args) == 2 else None)
            return
        if command == "use":
            target = raw_args.strip().lower()
            if not target:
                self._send(chat_id, tr("Usage: /use <session-id|{clis}>", clis="|".join(self.config.enabled_clis)))
                return
            session = self.sessions.resolve(chat_id, target)
            if session and session.cli in self.config.enabled_clis:
                self._switch_session(chat_id, session.session_id)
                self._send(chat_id, tr("Switched to {session}\nDirectory: {cwd}", session=session.session_id, cwd=session.cwd))
                self._surface_switched_session(session)
            elif target in self.config.enabled_clis:
                current = self._current_session(chat_id)
                self._new_session(chat_id, target, current.cwd if current else None)
            else:
                self._send(chat_id, tr("Session not found: {target}", target=target))
            return
        if command == "back":
            session = self._back_session(chat_id)
            self._send(
                chat_id,
                tr("Back to {session}\nDirectory: {cwd}", session=session.session_id, cwd=session.cwd)
                if session
                else tr("No previous active session to return to."),
            )
            if session:
                self._surface_switched_session(session)
            return
        if command == "sessions":
            self._send_sessions(chat_id, include_archived=raw_args.strip().lower() == "all")
            return
        if command == "tasks":
            self._send_tasks(chat_id)
            return
        if command == "tgstats":
            self._send(chat_id, self._telegram_stats_text())
            return
        if command in {"status", "context"}:
            session = self._current_or_reply(chat_id)
            if session:
                self._run_in_background(
                    f"diagnostics-{session.session_id}",
                    self._send_session_diagnostics,
                    chat_id,
                    session,
                    command == "status",
                )
            return
        if command == "where":
            session = self._current_or_reply(chat_id)
            if session:
                self._send(
                    chat_id,
                    tr(
                        "Current: {session}\nCLI: {cli}\nDirectory: {cwd}\nModel: {model}"
                        "\nReasoning effort: {effort}\nPermissions: no approval prompts",
                        session=session.session_id, cli=session.cli, cwd=session.cwd,
                        model=self._model_display(session), effort=self._effort_display(session),
                    ),
                )
            return
        if command == "interrupt":
            session = self._current_or_reply(chat_id)
            if not session:
                return
            try:
                interrupted = self._interrupt_session(session)
            except (CodexBackendError, SessionError) as exc:
                self._send(chat_id, tr("Could not interrupt: {error}", error=exc))
                return
            self._send(
                chat_id,
                tr("Interrupted {session}.", session=session.session_id) if interrupted else tr("No task is running."),
            )
            return
        if command == "resume":
            target = raw_args.strip()
            session = self.sessions.resolve(chat_id, target) if target else self._current_session(chat_id)
            if not session:
                self._send(chat_id, tr("No session to resume was found."))
                return
            if self._session_is_busy(session):
                self._send(chat_id, tr("{session} is running; there is nothing to resume.", session=session.label))
                return
            if not self.sessions.get_in_flight(session.session_id):
                self._send(chat_id, tr("{session} has no task to resume.", session=session.label))
                return
            self._send(chat_id, tr("Resuming {session}'s task…", session=session.label))
            self._start_session_turn(chat_id, session, RESUME_PROMPT)
            return
        if command == "cancel":
            target = raw_args.strip()
            session = self.sessions.resolve(chat_id, target) if target else self._current_session(chat_id)
            if not session:
                self._send(chat_id, tr("Session not found."))
                return
            if self._session_is_busy(session):
                self._send(chat_id, tr("{session} is running; use /interrupt to stop it.", session=session.label))
                return
            cleared = self.sessions.clear_in_flight(session.session_id)
            self._set_session_state(session.session_id, STATE_IDLE)
            self._send(
                chat_id,
                tr("Gave up {session}'s interrupted task.", session=session.label) if cleared
                else tr("{session} has no task to resume.", session=session.label),
            )
            return
        if command == "compact":
            session = self._current_or_reply(chat_id)
            if not session:
                return
            try:
                self._compact_session(chat_id, session)
            except (CodexBackendError, HeadlessBackendError, SessionError) as exc:
                self._send(chat_id, tr("Compaction failed: {error}", error=exc))
            return
        if command == "clear":
            session = self._current_or_reply(chat_id)
            if not session:
                return
            try:
                self._clear_session(chat_id, session)
            except (CodexBackendError, SessionError) as exc:
                self._send(chat_id, tr("Could not clear: {error}", error=exc))
            return
        if command == "reload":
            target = raw_args.strip().lower()
            if not target:
                self._send_reload_picker(chat_id)
                return
            if target not in {"current", "all"}:
                self._send(chat_id, tr("Usage: /reload [current|all]"))
                return
            try:
                result = self._reload_cli_backends(chat_id, target)
            except (CodexBackendError, SessionError) as exc:
                result = tr("Reload failed: {error}", error=exc)
            self._send(chat_id, result)
            return
        if command == "model":
            session = self._current_or_reply(chat_id)
            if not session:
                return
            arg = raw_args.strip()
            if arg:
                if arg.lower() in {"default", "reset", "clear"}:
                    value = None
                else:
                    value = arg
                self.sessions.set_model(session.session_id, value)
                refreshed = self.sessions.get(session.session_id) or session
                self._send(
                    chat_id,
                    tr(
                        "{session} model set to {model} (applies from the next task).",
                        session=session.label, model=self._model_display(refreshed),
                    ),
                )
            else:
                self._run_in_background(
                    f"model-picker-{session.session_id}", self._send_model_picker, chat_id, session
                )
            return
        if command == "effort":
            session = self._current_or_reply(chat_id)
            if not session:
                return
            arg = raw_args.strip()
            if arg:
                if arg.lower() in {"default", "reset", "clear"}:
                    value = None
                else:
                    value = arg.lower()
                self.sessions.set_effort(session.session_id, value)
                refreshed = self.sessions.get(session.session_id) or session
                self._send(
                    chat_id,
                    tr(
                        "{session} reasoning effort set to {effort} (applies from the next task).",
                        session=session.label, effort=self._effort_display(refreshed),
                    ),
                )
            else:
                self._send_effort_picker(chat_id, session)
            return
        if command == "stop":
            target = raw_args.strip()
            session = self.sessions.resolve(chat_id, target) if target else self._current_session(chat_id)
            if not session:
                self._send(chat_id, tr("No session to stop was found."))
                return
            try:
                self._retire_session(session, delete=True)
            except (CodexBackendError, SessionError):
                LOGGER.exception("could not interrupt %s before stopping", session.session_id)
                self._send(chat_id, tr("Could not stop the session; its record was kept. Try again later."))
                return
            self._send(chat_id, tr("Stopped {session}.", session=session.session_id))
            return
        if command == "rename":
            args = self._parse_args(chat_id, raw_args)
            if args is None:
                return
            current = self._current_session(chat_id)
            if not args or not current:
                self._send(chat_id, tr("Usage: /rename [session-id] <new name>"))
                return
            explicit = self.sessions.get(args[0]) if len(args) > 1 else None
            session = explicit if explicit and explicit.chat_id == chat_id else current
            name_parts = args[1:] if session is explicit else args
            try:
                renamed = self.sessions.rename(session.session_id, " ".join(name_parts))
            except SessionError as exc:
                self._send(chat_id, tr("Rename failed: {error}", error=exc))
                return
            self._send(chat_id, tr("Renamed {session} to “{name}”.", session=renamed.session_id, name=renamed.label))
            return
        if command == "archive":
            target = raw_args.strip()
            session = self.sessions.resolve(chat_id, target) if target else self._current_session(chat_id)
            if not session:
                self._send(chat_id, tr("No session to archive was found."))
                return
            self._archive_session(chat_id, session)
            return
        if command == "delete":
            target = raw_args.strip()
            session = self._session_for_management(chat_id, target)
            if not session:
                self._send(chat_id, tr("No session to delete was found."))
                return
            self._send_delete_confirmation(chat_id, session)
            return
        if command == "send":
            if not raw_args:
                self._send(chat_id, tr("Usage: /send <text for the current CLI>"))
                return
            self._send_to_current(chat_id, raw_args)
            return
        if command == "commands":
            self._send(chat_id, self._commands_view())
            return
        if command in self.commands:
            self._start_direct_command(chat_id, command, raw_args)
            return
        self._send(chat_id, tr("Unknown command. Use /help to see the available commands."))

    def _commands_view(self) -> str:
        if not self.commands:
            return tr(
                "No direct command extensions are installed.\n"
                "Put an extension directory in extensions/ or set COMMAND_DIR."
            )
        lines = [tr("Direct command extensions (no AI)"), ""]
        for command in sorted(self.commands.values(), key=lambda item: item.command):
            detail = command.display_description() or command.display_name()
            lines.append(f"/{command.command} · {command.display_name()}")
            lines.append(tr("  Usage: {usage}", usage=command.usage_text()))
            lines.append(tr("  About: {detail}", detail=detail))
            lines.append(tr("  Directory: {cwd}", cwd=command.cwd))
            lines.append(tr("  Manifest: {path}", path=command.manifest_path))
        return "\n".join(lines)

    def _start_direct_command(self, chat_id: int, command_name: str, raw_args: str) -> None:
        # Direct commands skip shell tokenizing: the extension parses the raw text itself
        # (argv[0] is in TG_ARGV0; every other non-empty line is one argument). shlex fails
        # with "No closing quotation" on quotes inside cookies, JSON or r'' strings, which
        # are data here, not syntax.
        args = [line.strip() for line in raw_args.strip().splitlines()]
        args = [line for line in args if line]
        try:
            run = self.direct_commands.start(
                chat_id,
                command_name,
                args,
                raw_args=raw_args,
                bot_key=self._active_bot_key(),
                user_id=self._active_user_id(),
            )
        except CommandError as exc:
            self._send(chat_id, str(exc))
            return
        LOGGER.info(
            "started direct command /%s for chat %s (%d args)",
            command_name,
            chat_id,
            len(args),
        )
        self._refresh_direct_command_card(run)

    def _session_for_management(self, chat_id: int, target: str) -> CliSession | None:
        if not target:
            return self._current_session(chat_id)
        session = self.sessions.get(target)
        if session and session.chat_id == chat_id:
            return session
        return self.sessions.resolve(chat_id, target)

    def _send_tasks(self, chat_id: int) -> None:
        text, markup = self._tasks_view(chat_id)
        try:
            self.telegram.send_message(
                chat_id,
                text,
                reply_markup=markup if markup["inline_keyboard"] else None,
            )
        except TelegramError:
            LOGGER.exception("could not send task controls to chat %s", chat_id)

    def _tasks_view(self, chat_id: int) -> tuple[str, dict[str, Any]]:
        lines = [tr("Tasks:")]
        buttons: list[list[dict[str, str]]] = []
        found = False
        for session in self.sessions.list_for_chat(chat_id):
            busy = self._session_is_busy(session)
            with self._state_lock:
                queued = len(self._queues.get(session.session_id, ()))
            if busy or queued:
                found = True
                lines.append(
                    tr(
                        "• {session} ({id}): {status}",
                        session=session.label, id=session.session_id, status=self._session_status_text(session),
                    )
                )
                if busy:
                    row = [{
                        "text": tr("Interrupt {session}", session=session.label),
                        "callback_data": f"taskinterrupt:{session.session_id}",
                    }]
                    if queued:
                        row.append({
                            "text": tr("Interrupt and clear the queue"),
                            "callback_data": f"stopqueue:{session.session_id}",
                        })
                    buttons.append(row)
                if queued:
                    buttons.append([{
                        "text": tr("Clear {count} queued for {session}", count=queued, session=session.label),
                        "callback_data": f"clearqueue:{session.session_id}",
                    }])
        text = "\n".join(lines) if found else tr("No tasks are running or queued.")
        return text, {"inline_keyboard": buttons}

    def _clear_queue(self, session_id: str) -> int:
        with self._state_lock:
            pending = self._queues.pop(session_id, None)
            return len(pending) if pending else 0

    def _archive_session(self, chat_id: int, session: CliSession) -> None:
        try:
            self._retire_session(session, delete=False)
        except (CodexBackendError, SessionError):
            LOGGER.exception("could not interrupt %s before archiving", session.session_id)
            self._send(chat_id, tr("Could not archive the session; its record was kept. Try again later."))
            return
        self._send(chat_id, tr("Archived {session}. Use /sessions all to view or restore it.", session=session.label))

    def _retire_session(self, session: CliSession, *, delete: bool) -> None:
        # A queue worker may already be waiting to start. Serialize with startup,
        # and clear waiting work before interruption can trigger its completion.
        with self._backend_reload_lock:
            self._clear_queue(session.session_id)
            self._interrupt_session(session)
            with self._publish_lock:
                with self._state_lock:
                    if delete:
                        self.sessions.stop(session)
                        self._last_completed_results.pop(session.session_id, None)
                        for key in list(self._turns):
                            if key[0] == session.session_id:
                                self._turns.pop(key, None)
                        self._turn_claims.discard(session.session_id)
                        self._interrupted_sessions.discard(session.session_id)
                        if session.external_id:
                            self._codex_active_turns.pop(session.external_id, None)
                    else:
                        self.sessions.set_archived(session.session_id, True)
                    self._session_states.pop(session.session_id, None)

    def _send_delete_confirmation(self, chat_id: int, session: CliSession) -> None:
        markup = {
            "inline_keyboard": [[
                {"text": tr("Delete"), "callback_data": f"delete!:{session.session_id}"},
                {"text": tr("Cancel"), "callback_data": f"cancel:{session.session_id}"},
            ]]
        }
        try:
            self.telegram.send_message(
                chat_id,
                tr("Permanently delete session {session} ({id})?", session=session.label, id=session.session_id),
                reply_markup=markup,
            )
        except TelegramError:
            LOGGER.exception("could not send delete confirmation")

    def _session_is_busy(self, session: CliSession) -> bool:
        with self._state_lock:
            return self._session_is_busy_locked(session)

    def _session_is_busy_locked(self, session: CliSession) -> bool:
        if session.session_id in self._turn_claims:
            return True
        if session.backend == "codex-app-server" and session.external_id:
            return bool(
                self._codex_active_turns.get(session.external_id)
                or session.external_id in self._pending_codex_compactions
            )
        return self.headless.is_active(session.session_id)

    def _set_session_state(self, session_id: str, state: str) -> None:
        with self._state_lock:
            self._session_states[session_id] = state

    def _session_status_text(self, session: CliSession) -> str:
        """Session state for display (running, idle, last run failed, waiting) plus the queue length."""
        with self._state_lock:
            state = self._session_states.get(session.session_id)
            queued = len(self._queues.get(session.session_id, ()))
        if state is None:
            # Older or uninitialized session: fall back to the live busy check.
            state = STATE_WORKING if self._session_is_busy(session) else STATE_IDLE
        text = tr(STATE_LABELS.get(state, STATE_LABELS[STATE_IDLE]))
        if queued:
            text += tr(" · {count} queued", count=queued)
        return text

    def _send_session_diagnostics(self, chat_id: int, session: CliSession, full: bool) -> None:
        self._send(chat_id, self._session_diagnostics_text(session, full=full))

    def _session_diagnostics_text(self, session: CliSession, *, full: bool) -> str:
        session = self.sessions.get(session.session_id) or session
        claude_quota: list[str] = []
        claude_context_unavailable = False
        if (
            session.cli == "claude" and session.backend == "headless-json" and session.external_id
            and "context_tokens" not in (session.usage or {})
        ):
            snapshot = read_claude_usage(session.external_id, session.cwd)
            if snapshot:
                self.sessions.update_usage(
                    session.session_id, session.external_id, snapshot, only_if_missing=True,
                )
            session = self.sessions.get(session.session_id) or session
        if session.cli == "claude" and session.backend == "headless-json":
            usage_model = (session.usage or {}).get("model")
            try:
                diagnostics = self.headless.read_claude_diagnostics(
                    session, model=usage_model or session.model, include_quota=full,
                )
                context = diagnostics.get("context") or {}
                if context and (not usage_model or context.get("model") == usage_model):
                    if not self.sessions.update_usage(
                        session.session_id, session.external_id, context, expected_model=usage_model,
                    ):
                        claude_context_unavailable = True
                else:
                    claude_context_unavailable = True
                claude_quota = diagnostics.get("quota_lines") or []
            except HeadlessBackendError:
                claude_context_unavailable = True
                claude_quota = [tr("Account quota: temporarily unavailable; try again later.")]
            session = self.sessions.get(session.session_id) or session
        lines = [tr("{session} ({id}) · {cli}", session=session.label, id=session.session_id, cli=session.cli)]
        if full:
            lines.extend([
                tr("Status: {status}", status=self._session_status_text(session)),
                tr("Directory: {cwd}", cwd=session.cwd),
                tr("Model: {model}", model=self._model_display(session)),
                tr("Reasoning effort: {effort}", effort=self._effort_display(session)),
            ])
            with self._state_lock:
                view = next((
                    view for (session_id, _turn_id), view in self._turns.items()
                    if session_id == session.session_id and view.status == "running"
                ), None)
                if view:
                    lines.append(tr(
                        "Current task: running for {seconds} s",
                        seconds=max(0, int(time.monotonic() - view.started_at)),
                    ))
                    if (
                        view.model_label != self._model_header_label(session)
                        or view.effort_label != self._effort_header_label(session)
                    ):
                        lines.append(tr(
                            "Current task model: {model} · reasoning effort: {effort}",
                            model=view.model_label, effort=view.effort_label,
                        ))
        lines.extend(context_lines(session.usage))
        if claude_context_unavailable:
            lines.append(tr("The native Claude context query is unavailable right now; showing saved data."))
        if full:
            lines.extend(usage_lines(session.usage))
            if session.backend == "codex-app-server":
                try:
                    lines.extend(quota_lines(self.codex.read_rate_limits()))
                except CodexBackendError:
                    lines.append(tr("Account quota: temporarily unavailable; try again later."))
            elif session.cli == "claude" and session.backend == "headless-json":
                lines.extend(claude_quota or [tr("Account quota: Claude returned no quota data.")])
            else:
                lines.append(tr("Account quota: {cli} offers no quota query through this integration.", cli=session.cli))
        return "\n".join(lines)

    def _surface_switched_session(self, session: CliSession) -> None:
        with self._publish_lock:
            self._surface_switched_session_locked(session)

    def _surface_switched_session_locked(self, session: CliSession) -> None:
        """When switching back to a session, put its most useful current state at the bottom of the chat."""
        bot_key = self._active_bot_key()
        with self._state_lock:
            pending_views = [
                view
                for (session_id, _turn_id), view in self._turns.items()
                if session_id == session.session_id
            ]
            source_view = next(
                (
                    view
                    for view in pending_views
                    if view.status == "running" or view.bot_key != bot_key
                ),
                None,
            )
            if source_view and source_view.status == "running" and source_view.bot_key == bot_key:
                # The status pump re-pins the running state for the bot that started it.
                return
            if source_view and source_view.bot_key != bot_key:
                rendered, _answer = self._render_turn(source_view, time.monotonic())
            else:
                rendered = None
            for view in pending_views:
                if view.bot_key != bot_key:
                    continue
                # The CLI finished but its final state is unpublished: force a new card at the bottom.
                self._abandon_view_cards(view)
                view.dirty = True
        if rendered is not None:
            try:
                message_id = self._telegram(bot_key).send_rich_markdown(
                    session.chat_id, rendered
                )
                if message_id is not None:
                    self.sessions.bind_message(
                        session.chat_id, message_id, session.session_id, bot_key
                    )
                    if source_view and source_view.status == "completed":
                        self.sessions.set_last_completed_message(
                            session.session_id, message_id, bot_key
                        )
                        self._last_completed_results[session.session_id] = rendered
            except TelegramError as exc:
                LOGGER.warning(
                    "could not surface running state for %s on bot %s: %s",
                    session.session_id,
                    bot_key,
                    exc,
                )
            return
        if pending_views or self._session_is_busy(session):
            return

        source_message_id = self.sessions.get_last_completed_message(
            session.session_id, bot_key
        )
        cached_result = self._last_completed_results.get(session.session_id)
        # A saved message pointer may reference only the first chunk (or a
        # truncated background card). Use the full cached result when available.
        if source_message_id is not None and (
            not cached_result or len(split_markdown(cached_result)) == 1
        ):
            try:
                copied_id = self._telegram(bot_key).copy_message(
                    session.chat_id, session.chat_id, source_message_id
                )
            except TelegramError as exc:
                LOGGER.warning(
                    "could not surface last completed message for %s: %s",
                    session.session_id,
                    exc,
                )
            else:
                if copied_id is not None:
                    self.sessions.bind_message(
                        session.chat_id, copied_id, session.session_id, bot_key
                    )
                    self.sessions.set_last_completed_message(
                        session.session_id, copied_id, bot_key
                    )
                    return

        if cached_result:
            try:
                copied_id = self._telegram(bot_key).send_rich_markdown(
                    session.chat_id, cached_result
                )
            except TelegramError as exc:
                LOGGER.warning(
                    "could not surface cached result for %s on bot %s: %s",
                    session.session_id,
                    bot_key,
                    exc,
                )
            else:
                if copied_id is not None:
                    self.sessions.bind_message(
                        session.chat_id, copied_id, session.session_id, bot_key
                    )
                    self.sessions.set_last_completed_message(
                        session.session_id, copied_id, bot_key
                    )
                    return

        self._send(session.chat_id, tr("[{session}] · How can I help?", session=session.session_id))

    def _sessions_view(
        self, chat_id: int, include_archived: bool = False
    ) -> tuple[str, dict[str, Any]]:
        current = self._current_session(chat_id)
        sessions = self.sessions.list_for_chat(chat_id, include_archived=include_archived)
        lines = [tr("Sessions (tap a button to switch):")]
        buttons: list[list[dict[str, str]]] = []
        for session in sessions:
            selected = bool(current and current.session_id == session.session_id)
            marker = "▶" if selected else " "
            if session.archived:
                status = tr("Archived")
            else:
                status = self._session_status_text(session)
                if not self.sessions.is_alive(session):
                    status += tr(" · exited")
            identity = session.label
            if session.display_name:
                identity += f" ({session.session_id})"
            lines.append(f"{marker} {identity} · {status}\n    {session.cwd}")
            if session.archived:
                buttons.append([
                    {"text": tr("Restore {session}", session=session.label), "callback_data": f"restore:{session.session_id}"},
                    {"text": tr("Delete…"), "callback_data": f"delete?:{session.session_id}"},
                ])
            elif self.sessions.is_alive(session):
                label = f"✓ {session.label}" if selected else session.label
                row = [{"text": label, "callback_data": f"use:{session.session_id}"}]
                if self._session_is_busy(session):
                    row.append({"text": tr("Interrupt"), "callback_data": f"interrupt:{session.session_id}"})
                buttons.append(row)
                buttons.append([
                    {"text": tr("Archive"), "callback_data": f"archive:{session.session_id}"},
                    {"text": tr("Delete…"), "callback_data": f"delete?:{session.session_id}"},
                ])
        return "\n".join(lines), {"inline_keyboard": buttons}

    def _send_sessions(self, chat_id: int, include_archived: bool = False) -> None:
        sessions = self.sessions.list_for_chat(chat_id, include_archived=include_archived)
        if not sessions:
            self._send(chat_id, tr("There are no sessions yet."))
            return
        text, markup = self._sessions_view(chat_id, include_archived=include_archived)
        try:
            self.telegram.send_message(chat_id, text, reply_markup=markup)
        except TelegramError:
            LOGGER.exception("could not send session keyboard to chat %s", chat_id)

    def _model_view(self, session: CliSession) -> tuple[str, dict[str, Any]]:
        models = list_models(session.cli, self.config.cli_commands[session.cli])
        current = self._effective_model(session)
        lines = [
            tr("{session} · current model: {model}", session=session.label, model=self._model_display(session)),
            tr("Tap to switch (applies from the next task):"),
        ]
        buttons: list[list[dict[str, str]]] = []
        for model in models:
            marker = "✓ " if model == current else ""
            buttons.append([{
                "text": f"{marker}{model}"[:64],
                "callback_data": f"model:{session.session_id}:{self._model_choice_id(model)}",
            }])
        reset_label = tr("Restore gateway default") if self.config.default_model_for(session.cli) else tr("Restore default")
        buttons.append([{"text": reset_label, "callback_data": f"modelclear:{session.session_id}"}])
        if not models:
            lines.append(tr("This CLI cannot list its models; use /model <name> directly."))
        return "\n".join(lines), {"inline_keyboard": buttons}

    @staticmethod
    def _model_choice_id(model: str) -> str:
        # Catalog ordering can change between rendering and clicking a button.
        return hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]

    def _effort_view(self, session: CliSession) -> tuple[str, dict[str, Any]]:
        efforts = list_efforts(session.cli)
        current = self._effective_effort(session)
        lines = [
            tr("{session} · current reasoning effort: {effort}", session=session.label, effort=self._effort_display(session)),
            tr("Tap to switch (applies from the next task):"),
        ]
        buttons: list[list[dict[str, str]]] = []
        for index, effort in enumerate(efforts):
            marker = "✓ " if effort == current else ""
            buttons.append([{
                "text": f"{marker}{effort}",
                "callback_data": f"effort:{session.session_id}:{index}",
            }])
        reset_label = tr("Restore gateway default") if self.config.default_effort_for(session.cli) else tr("Restore default")
        buttons.append([{"text": reset_label, "callback_data": f"effortclear:{session.session_id}"}])
        if not efforts:
            lines.append(tr("This CLI does not support setting the reasoning effort."))
        return "\n".join(lines), {"inline_keyboard": buttons}

    def _send_model_picker(self, chat_id: int, session: CliSession) -> None:
        text, markup = self._model_view(session)
        try:
            self.telegram.send_message(chat_id, text, reply_markup=markup)
        except TelegramError:
            LOGGER.exception("could not send model picker to chat %s", chat_id)

    def _send_effort_picker(self, chat_id: int, session: CliSession) -> None:
        text, markup = self._effort_view(session)
        try:
            self.telegram.send_message(chat_id, text, reply_markup=markup)
        except TelegramError:
            LOGGER.exception("could not send effort picker to chat %s", chat_id)

    def _handle_callback_query(self, callback: dict[str, Any]) -> None:
        query_id = callback.get("id")
        sender = callback.get("from")
        message = callback.get("message")
        data = callback.get("data")
        user_id = sender.get("id") if isinstance(sender, dict) else None
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        chat_type = chat.get("type") if isinstance(chat, dict) else None
        message_id = message.get("message_id") if isinstance(message, dict) else None
        if not isinstance(query_id, str):
            return
        if (
            not isinstance(user_id, int)
            or user_id not in self.config.allowed_user_ids
            or not isinstance(chat_id, int)
            or (chat_type != "private" and not self.config.allow_groups)
        ):
            try:
                self.telegram.answer_callback_query(query_id, tr("Not allowed"))
            except TelegramError:
                pass
            return
        if not isinstance(data, str) or ":" not in data:
            try:
                self.telegram.answer_callback_query(query_id, tr("Unknown action"))
            except TelegramError:
                pass
            return
        action, target = data.split(":", 1)
        if action == "new":
            if target not in self.config.enabled_clis:
                try:
                    self.telegram.answer_callback_query(query_id, tr("That CLI is no longer available; use /new again"))
                except TelegramError:
                    pass
                return
            try:
                self.telegram.answer_callback_query(query_id, tr("Creating {cli}", cli=target))
            except TelegramError:
                LOGGER.exception("could not answer new-session callback for chat %s", chat_id)
            if isinstance(message_id, int):
                try:
                    self.telegram.edit_message(chat_id, message_id, tr("Picked {cli}; creating…", cli=target))
                except TelegramError:
                    LOGGER.exception("could not close new-session picker for chat %s", chat_id)
            self._new_session(chat_id, target, None)
            return

        if action == "reload":
            try:
                self.telegram.answer_callback_query(query_id, tr("Reloading"))
            except TelegramError:
                LOGGER.exception("could not answer reload callback for chat %s", chat_id)
            try:
                result = self._reload_cli_backends(chat_id, target)
            except (CodexBackendError, SessionError) as exc:
                result = tr("Reload failed: {error}", error=exc)
            try:
                if isinstance(message_id, int):
                    self.telegram.edit_message(chat_id, message_id, result)
                else:
                    self._send(chat_id, result)
            except TelegramError:
                LOGGER.exception("could not finish reload callback for chat %s", chat_id)
            return

        if action in {"file", "photo", "video"}:
            resolved = self.sessions.resolve_artifact(
                chat_id, target, self._active_bot_key()
            )
            if not resolved:
                self.telegram.answer_callback_query(query_id, tr("This file is no longer available"))
                return
            _session, path = resolved
            try:
                path = self._validated_local_file(path)
            except (OSError, ValueError) as exc:
                LOGGER.warning("artifact send failed: %s", exc)
                self._send(chat_id, tr("Could not send the file: {error}", error=exc))
                return
            with self._state_lock:
                uploading = target in self._artifact_uploads
                self._artifact_uploads.add(target)
            if uploading:
                try:
                    self.telegram.answer_callback_query(query_id, tr("Already sending; please wait"))
                except TelegramError:
                    pass
                return
            try:
                self.telegram.answer_callback_query(query_id, tr("Sending"))
            except TelegramError as exc:
                with self._state_lock:
                    self._artifact_uploads.discard(target)
                LOGGER.warning("artifact send failed: %s", exc)
                self._send(chat_id, tr("Could not send the file: {error}", error=exc))
                return
            self._run_in_background(
                f"artifact-{target}", self._send_artifact_file, chat_id, target, path, action
            )
            return

        if action == "sendpath":
            # An artifact over the Bot API upload limit cannot be sent as a file; return its local path.
            resolved = self.sessions.resolve_artifact(chat_id, target, self._active_bot_key())
            if not resolved:
                self.telegram.answer_callback_query(query_id, tr("This path is no longer available"))
                return
            try:
                self.telegram.answer_callback_query(query_id, tr("Path sent"))
            except TelegramError:
                pass
            path = resolved[1]
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            self._send(
                chat_id,
                tr(
                    "The artifact exceeds Telegram's send limit, so here is its local path:\n`{path}`\n"
                    "Size {size:.1f} MiB / limit {limit:.0f} MiB",
                    path=path, size=size / (1024 * 1024),
                    limit=self.config.telegram_max_file_bytes / (1024 * 1024),
                ),
            )
            return

        if action in {"model", "modelclear", "effort", "effortclear"}:
            if action in {"model", "effort"}:
                session_id, separator, index_text = target.partition(":")
                index = int(index_text) if separator and index_text.isdigit() else -1
            else:
                session_id, index = target, -1
            session = self.sessions.get(session_id)
            if not session or session.chat_id != chat_id:
                self.telegram.answer_callback_query(query_id, tr("Session expired"))
                return
            if action == "modelclear":
                self.sessions.set_model(session_id, None)
                default_model = self.config.default_model_for(session.cli)
                notice = (
                    tr("Restored the gateway default model {model}", model=default_model) if default_model
                    else tr("Restored the default model")
                )
            elif action == "effortclear":
                self.sessions.set_effort(session_id, None)
                default_effort = self.config.default_effort_for(session.cli)
                notice = (
                    tr("Restored the gateway default reasoning effort {effort}", effort=default_effort)
                    if default_effort
                    else tr("Restored the default reasoning effort")
                )
            elif action == "model":
                models = list_models(session.cli, self.config.cli_commands[session.cli])
                selected = [model for model in models if self._model_choice_id(model) == index_text]
                if len(selected) == 1:
                    value = selected[0]
                    self.sessions.set_model(session_id, value)
                    notice = tr("Model set to {model}", model=value)
                else:
                    notice = tr("That choice expired; use /model again")
            else:
                efforts = list_efforts(session.cli)
                if 0 <= index < len(efforts):
                    value = efforts[index]
                    self.sessions.set_effort(session_id, value)
                    notice = tr("Reasoning effort set to {effort}", effort=value)
                else:
                    notice = tr("That choice expired; use /effort again")
            try:
                self.telegram.answer_callback_query(query_id, notice)
                if isinstance(message_id, int):
                    refreshed = self.sessions.get(session_id)
                    if refreshed:
                        text, markup = (
                            self._model_view(refreshed)
                            if action in {"model", "modelclear"}
                            else self._effort_view(refreshed)
                        )
                        self.telegram.edit_message(chat_id, message_id, text, reply_markup=markup)
            except TelegramError:
                LOGGER.exception("could not update model/effort picker for chat %s", chat_id)
            return

        session = self.sessions.get(target)
        if not session or session.chat_id != chat_id:
            self.telegram.answer_callback_query(query_id, tr("Session expired"))
            return
        try:
            if action in {"taskinterrupt", "clearqueue", "stopqueue"}:
                if action == "taskinterrupt":
                    interrupted = self._interrupt_session(session)
                    notice = tr("Interrupted the current task") if interrupted else tr("No task is running")
                elif action == "clearqueue":
                    cleared = self._clear_queue(session.session_id)
                    notice = tr("Cleared {count} queued messages", count=cleared) if cleared else tr("The queue is already empty")
                else:
                    # Clear first so the terminal callback cannot start the next queued turn.
                    cleared = self._clear_queue(session.session_id)
                    interrupted = self._interrupt_session(session)
                    if interrupted:
                        notice = tr("Interrupted and cleared {count} queued messages", count=cleared)
                    else:
                        notice = tr("Cleared {count} queued messages", count=cleared) if cleared else tr("No task is running")
                self.telegram.answer_callback_query(query_id, notice)
                if isinstance(message_id, int):
                    text, markup = self._tasks_view(chat_id)
                    self.telegram.edit_message(chat_id, message_id, text, reply_markup=markup)
                return

            refresh = True
            include_archived = False
            switched = False
            if action == "use":
                self._switch_session(chat_id, session.session_id)
                switched = True
                notice = tr("Switched to {session}", session=session.label)
            elif action == "interrupt":
                interrupted = self._interrupt_session(session)
                notice = tr("Interrupted") if interrupted else tr("No task is running")
            elif action == "archive":
                self._retire_session(session, delete=False)
                notice = tr("Archived")
            elif action == "restore":
                self.sessions.set_archived(session.session_id, False)
                notice = tr("Restored")
                include_archived = True
            elif action == "delete?":
                self.telegram.answer_callback_query(query_id, tr("Please confirm"))
                self._send_delete_confirmation(chat_id, session)
                return
            elif action == "delete!":
                self._retire_session(session, delete=True)
                notice = tr("Deleted")
                refresh = False
                if isinstance(message_id, int):
                    self.telegram.edit_message(
                        chat_id, message_id, tr("Deleted {session} ({id}).", session=session.label, id=session.session_id)
                    )
            elif action == "cancel":
                notice = tr("Cancelled")
                refresh = False
                if isinstance(message_id, int):
                    self.telegram.edit_message(chat_id, message_id, tr("Deletion cancelled."))
            else:
                self.telegram.answer_callback_query(query_id, tr("Unknown action"))
                return
            self.telegram.answer_callback_query(query_id, notice)
            if refresh and isinstance(message_id, int):
                text, markup = self._sessions_view(chat_id, include_archived=include_archived)
                self.telegram.edit_message(chat_id, message_id, text, reply_markup=markup)
            if switched:
                self._surface_switched_session(session)
        except TelegramError:
            LOGGER.exception("could not update session keyboard for chat %s", chat_id)
        except (CodexBackendError, SessionError) as exc:
            try:
                self.telegram.answer_callback_query(query_id, str(exc))
            except TelegramError:
                pass

    def _send_artifact_file(self, chat_id: int, token: str, path: Path, action: str) -> None:
        try:
            try:
                if path.suffix.lower() == ".mp4":
                    self.telegram.send_local_file(chat_id, path, as_video=True)
                else:
                    self.telegram.send_local_file(chat_id, path, as_photo=action == "photo")
            except TelegramError as exc:
                if (
                    action != "photo"
                    or path.suffix.lower() == ".mp4"
                    or exc.retry_after is not None
                    or not exc.fallback_allowed
                ):
                    raise
                self.telegram.send_local_file(chat_id, path, as_photo=False)
        except (OSError, ValueError, TelegramError) as exc:
            LOGGER.warning("artifact send failed: %s", exc)
            self._send(chat_id, tr("Could not send the file: {error}", error=exc))
        finally:
            with self._state_lock:
                self._artifact_uploads.discard(token)

    def _interrupt_session(self, session: CliSession) -> bool:
        if not self.sessions.get_in_flight(session.session_id):
            # With the session idle, /interrupt falls back to the direct command running in this chat.
            self.direct_commands.interrupt_chat(session.chat_id, self._active_bot_key())
        return self._interrupt_session_turn(session)

    def _interrupt_session_turn(self, session: CliSession) -> bool:
        if session.backend == "codex-app-server" and session.external_id:
            with self._state_lock:
                turn_id = self._codex_active_turns.get(session.external_id)
            if not turn_id or turn_id == "starting":
                return False
            # The terminal notification may arrive before interrupt() returns.
            self._interrupted_sessions.add(session.session_id)
            try:
                self.codex.interrupt(session.external_id, turn_id)
            except Exception:
                self._interrupted_sessions.discard(session.session_id)
                raise
            self.sessions.clear_in_flight(session.session_id, turn_id)
            return True
        if session.backend == "headless-json":
            turn_id = self.sessions.get_in_flight(session.session_id)
            self._interrupted_sessions.add(session.session_id)
            try:
                interrupted = self.headless.interrupt(session.session_id)
            except Exception:
                self._interrupted_sessions.discard(session.session_id)
                raise
            if interrupted:
                # Never clear a queued successor's recovery record or state.
                if turn_id:
                    self.sessions.clear_in_flight(session.session_id, turn_id)
            else:
                self._interrupted_sessions.discard(session.session_id)
            return interrupted
        return False

    def _reload_cli_backends(self, chat_id: int, target: str) -> str:
        """Reload resident backends without changing native session IDs.

        Codex owns one app-server shared by every Codex thread, so selecting one
        Codex session necessarily restarts that shared process and then resumes
        every saved thread. Claude, Grok and Pi are spawned once per turn; when
        idle there is no resident process to restart, and their next turn already
        resolves the configured command to the currently installed executable.
        """
        focus: CliSession | None = None
        if target == "all":
            target_clis = set(self.config.enabled_clis)
        else:
            focus = (
                self._current_session(chat_id)
                if target == "current"
                else self.sessions.get(target)
            )
            if (
                not focus
                or focus.chat_id != chat_id
                or focus.archived
                or focus.cli not in self.config.enabled_clis
            ):
                raise SessionError(tr("The session expired; use /reload again"))
            target_clis = {focus.cli}

        with self._backend_reload_lock:
            all_sessions = self.sessions.all_sessions()
            if focus and focus.cli != "codex":
                candidates = [focus]
            else:
                candidates = [
                    session
                    for session in all_sessions
                    if session.cli in target_clis and not session.archived
                ]
            busy = [
                session.label
                for session in candidates
                if self._session_is_busy(session)
            ]
            if "codex" in target_clis:
                with self._state_lock:
                    codex_operation_pending = bool(
                        self._pending_codex_compactions or self._codex_compaction_turns
                    )
                if codex_operation_pending and not busy:
                    busy.append(tr("Codex context compaction"))
            if busy:
                shown = join_items(busy[:5]) + (tr(" etc.") if len(busy) > 5 else "")
                raise SessionError(tr("Tasks are still running: {tasks}; wait for them to finish and retry", tasks=shown))

            clear_listing_cache()
            lines: list[str] = []
            if "codex" in target_clis:
                self.codex.close()
                try:
                    self.codex.start()
                except CodexBackendError:
                    # initialize may fail after the process has started. Do not
                    # leave a half-initialized app-server behind.
                    self.codex.close()
                    raise
                codex_sessions = [
                    session
                    for session in all_sessions
                    if session.cli == "codex"
                    and session.backend == "codex-app-server"
                    and session.external_id
                ]
                resumed: list[str] = []
                failed: list[str] = []
                for session in codex_sessions:
                    try:
                        self.codex.resume_thread(
                            session.external_id or "", model=self._effective_model(session)
                        )
                    except CodexBackendError:
                        LOGGER.exception(
                            "could not resume Codex thread after reload: %s",
                            session.session_id,
                        )
                        failed.append(session.label)
                    else:
                        resumed.append(session.label)
                lines.append(tr("Codex backend restarted; resumed {count} sessions.", count=len(resumed)))
                if failed:
                    lines.append(tr(
                        "Could not resume: {sessions}",
                        sessions=join_items(failed[:5]) + (tr(" etc.") if len(failed) > 5 else ""),
                    ))

            headless_clis = [
                cli
                for cli in self.config.enabled_clis
                if cli in target_clis and cli != "codex"
            ]
            if headless_clis:
                lines.append(tr(
                    "{clis}: a new process starts for each task, so the next task uses the installed version.",
                    clis=join_items(headless_clis),
                ))
            return "\n".join(lines) or tr("There is no CLI to reload.")

    def _compact_session(self, chat_id: int, session: CliSession) -> None:
        """Compact the current session's context: Codex through native thread/compact/start,
        Pi through a one-shot RPC.

        A busy session is refused (compaction needs an idle context). Grok and Claude have no
        manual compaction in this mode, so the reply explains that instead of pretending.
        """
        if self._session_is_busy(session):
            self._send(chat_id, tr("{session} is running; /interrupt it before compacting.", session=session.label))
            return
        if session.backend == "codex-app-server" and session.external_id:
            thread_id = session.external_id
            with self._state_lock:
                # Manual compaction emits an internal turn on the app-server reader
                # thread, which has no Telegram Bot thread-local scope. Remember the
                # command origin before the request so that turn is not mistaken for
                # a normal answer routed through the default Bot.
                self._pending_codex_compactions[thread_id] = self._active_bot_key()
            try:
                self.codex.compact_thread(thread_id)
            except Exception:
                with self._state_lock:
                    self._pending_codex_compactions.pop(thread_id, None)
                raise
            self._send(chat_id, tr("Started context compaction for {session}.", session=session.label))
            return
        if session.cli == "pi":
            with self._state_lock:
                # Claim the session so input sent during compaction is queued and
                # started once the compaction process exits.
                busy = self._session_is_busy_locked(session)
                if not busy:
                    self._turn_claims.add(session.session_id)
            if busy:
                self._send(chat_id, tr("{session} is running; /interrupt it before compacting.", session=session.label))
                return
            self._send(chat_id, tr("Compacting {session}'s context…", session=session.label))
            self._run_in_background(
                f"compact-{session.session_id}", self._run_pi_compaction, chat_id, session
            )
            return
        if session.cli == "claude":
            self._send(
                chat_id,
                tr(
                    "{session} uses Claude's automatic compaction (--autocompact); manual /compact "
                    "exists only in interactive mode, not headless.",
                    session=session.label,
                ),
            )
            return
        self._send(
            chat_id,
            tr(
                "{session} (Grok) has no manual compaction; Grok compacts automatically near 85% "
                "context. Use /clear to start over.",
                session=session.label,
            ),
        )

    def _run_pi_compaction(self, chat_id: int, session: CliSession) -> None:
        try:
            result = self.headless.compact_session(session)
        except HeadlessBackendError as exc:
            with self._state_lock:
                interrupted = session.session_id in self._interrupted_sessions
                self._interrupted_sessions.discard(session.session_id)
            self._send(chat_id, tr("Compaction interrupted.") if interrupted else tr("Compaction failed: {error}", error=exc))
        else:
            self._send(chat_id, tr("{session}: {result}", session=session.label, result=result))
        finally:
            self._finish_turn(session)

    def _clear_session(self, chat_id: int, session: CliSession) -> None:
        with self._backend_reload_lock, self._publish_lock:
            self._clear_session_locked(chat_id, session)

    def _clear_session_locked(self, chat_id: int, session: CliSession) -> None:
        """Start a new conversation in the current session: the gateway record stays, the
        CLI context starts from zero.

        Codex gets a new thread and the old one is archived; headless sessions get a new
        external_id and turn_count resets (the next turn runs in first-turn mode). A busy
        session is refused.
        """
        if self._session_is_busy(session):
            self._send(chat_id, tr("{session} is running; /interrupt it before /clear.", session=session.label))
            return
        if session.backend == "codex-app-server" and session.external_id:
            old_thread = session.external_id
            try:
                thread_id = self.codex.start_thread(
                    session.cwd, model=self._effective_model(session)
                )
            except CodexBackendError:
                # Keep the old thread when a new one cannot be created.
                self._send(chat_id, tr("Could not create a new Codex thread; nothing was cleared."))
                return
            try:
                self.codex.archive_thread(old_thread)
            except CodexBackendError:
                LOGGER.warning("could not archive old Codex thread %s", old_thread)
            self.sessions.update_backend(session.session_id, "codex-app-server", thread_id)
            self._reset_session_result(session)
            self._send(chat_id, tr("{session} started a new conversation (the old thread was archived).", session=session.label))
            return
        if session.backend == "headless-json":
            self.sessions.rotate_external_id(session.session_id)
            self._reset_session_result(session)
            self._send(chat_id, tr("{session} started a new conversation; the context was cleared.", session=session.label))
            return
        self._send(chat_id, tr("{session} does not support /clear.", session=session.label))

    def _reset_session_result(self, session: CliSession) -> None:
        with self._state_lock:
            self.sessions.clear_in_flight(session.session_id)
            self.sessions.clear_last_completed_messages(session.session_id)
            self._last_completed_results.pop(session.session_id, None)
            self._session_states[session.session_id] = STATE_IDLE
            self._queues.pop(session.session_id, None)
            for key in list(self._turns):
                if key[0] == session.session_id:
                    self._turns.pop(key, None)

    def _send_to_current(
        self, chat_id: int, text: str, attachments: tuple[Attachment, ...] = ()
    ) -> None:
        session = self._current_or_reply(chat_id)
        if not session:
            return
        self._send_to_session(chat_id, session, text, attachments)

    def _send_to_session(
        self,
        chat_id: int,
        session: CliSession,
        text: str,
        attachments: tuple[Attachment, ...] = (),
    ) -> None:
        bot_key = self._active_bot_key()
        if session.cli not in self.config.enabled_clis:
            self._send(chat_id, tr("{cli} is not enabled for this bot, so this session cannot continue.", cli=session.cli))
            return
        if session.backend == "codex-app-server" and session.external_id:
            with self._state_lock:
                active_turn = self._codex_active_turns.get(session.external_id)
                active_view = (
                    self._turns.get((session.session_id, active_turn))
                    if active_turn and active_turn != "starting"
                    else None
                )
                steer_boundary = len(self._turn_progress(active_view)) if active_view else 0
            # Steering keeps the active turn's immutable reply route. A message
            # from another Bot must therefore become its own queued turn, or its
            # answer would appear only in the Bot that started the active turn.
            if active_view and active_view.bot_key == bot_key:
                try:
                    returned = self.codex.steer_turn(
                        session.external_id, active_turn, text, attachments
                    )
                    if returned != active_turn:
                        raise CodexBackendError("turn/steer returned a different turn ID")
                    self._record_steer(chat_id, session, active_turn, steer_boundary, bot_key)
                    return
                except CodexBackendError as exc:
                    if exc.ambiguous:
                        self._send(chat_id, tr(
                            "The added request was not confirmed, so it was not resent to avoid running it twice. "
                            "Check the current task output."
                        ))
                        return
                    LOGGER.warning("Codex steer failed; queued instead: %s", exc)
        elif session.cli == "claude" and session.backend == "headless-json":
            with self._state_lock:
                active_view = next(
                    (
                        view for (session_id, _turn_id), view in self._turns.items()
                        if session_id == session.session_id and view.status == "running"
                    ),
                    None,
                )
                steer_boundary = len(self._turn_progress(active_view)) if active_view else 0
            # Same reply-route rule as Codex: input from another Bot becomes its own turn.
            if active_view and active_view.bot_key == bot_key:
                confirm = self.headless.steer_turn(session.session_id, text, attachments)
                if confirm is not None:
                    # The message is already written, so a failure below must never
                    # requeue it. Waiting for Claude's receipt stays off the dispatcher.
                    self._run_in_background(
                        "steer-confirm", self._confirm_headless_steer, chat_id, session,
                        active_view.turn_id, steer_boundary, bot_key, confirm,
                    )
                    return
        self._start_session_turn(chat_id, session, text, attachments)

    def _confirm_headless_steer(
        self,
        chat_id: int,
        session: CliSession,
        turn_id: str,
        steer_boundary: int,
        bot_key: str,
        confirm: Callable[[float], bool],
    ) -> None:
        if confirm(HEADLESS_STEER_CONFIRM_SECONDS):
            self._record_steer(chat_id, session, turn_id, steer_boundary, bot_key)
            return
        self._send(chat_id, tr(
            "The added request was not confirmed, so it was not resent to avoid running it twice. "
            "Check the current task output."
        ), bot_key)

    def _record_steer(
        self, chat_id: int, session: CliSession, turn_id: str, steer_boundary: int, bot_key: str
    ) -> None:
        with self._state_lock:
            view = self._turns.get((session.session_id, turn_id))
            if view:
                view.steered += 1
                view.steer_notices.append((view.steered, steer_boundary))
                view.revision += 1
                view.dirty = True
        if view is None:
            # The publisher may have finished while the CLI confirmed the input.
            self._run_in_background(
                "steer-ack", self._send, chat_id,
                tr("[{session}] The CLI accepted the added request.", session=session.session_id), bot_key,
            )

    def _enqueue(self, session: CliSession, pending: PendingInput) -> int | None:
        with self._state_lock:
            queue_for_session = self._queues.setdefault(session.session_id, deque())
            if len(queue_for_session) >= MAX_PENDING_INPUTS_PER_SESSION:
                return None
            queue_for_session.append(pending)
            return len(queue_for_session)

    def _finish_turn(
        self,
        session: CliSession,
        thread_id: str | None = None,
        turn_id: str | None = None,
        *,
        drain_queue: bool = True,
    ) -> None:
        """Release the session claim, or keep it and start the next queued turn.

        Busy is cleared in the same lock as the queue check so a concurrent user
        message cannot start a second overlapping turn.
        """
        with self._state_lock:
            current_session = self.sessions.get(session.session_id)
            has_next = bool(
                drain_queue
                and not self.stop_event.is_set()
                and current_session and not current_session.archived
                and self._queues.get(session.session_id)
            )
            if thread_id is not None:
                current = self._codex_active_turns.get(thread_id)
                if current and current not in {turn_id, "starting"} and turn_id is not None:
                    return  # A late terminal event cannot release a newer turn.
                if current == turn_id or current == "starting" or turn_id is None:
                    if has_next:
                        self._codex_active_turns[thread_id] = "starting"
                    else:
                        self._codex_active_turns.pop(thread_id, None)
            if has_next:
                self._turn_claims.add(session.session_id)
            else:
                self._turn_claims.discard(session.session_id)
        if not has_next:
            return
        if session.backend == "codex-app-server":
            threading.Thread(
                target=self._start_next_queued,
                args=(session,),
                name=f"queue-{session.session_id}",
                daemon=True,
            ).start()
            return
        self._start_next_queued(session)

    def _start_next_queued(self, session: CliSession) -> None:
        with self._state_lock:
            refreshed = self.sessions.get(session.session_id)
            if self.stop_event.is_set() or not refreshed or refreshed.archived:
                self._turn_claims.discard(session.session_id)
                return
            session = refreshed
            queue_for_session = self._queues.get(session.session_id)
            pending = queue_for_session.popleft() if queue_for_session else None
            if queue_for_session is not None and not queue_for_session:
                self._queues.pop(session.session_id, None)
            if pending:
                self._turn_claims.add(session.session_id)
                self._drain_threads[session.session_id] = threading.get_ident()
                if session.backend == "codex-app-server" and session.external_id:
                    self._codex_active_turns[session.external_id] = "starting"
            else:
                self._turn_claims.discard(session.session_id)
                if session.external_id and self._codex_active_turns.get(session.external_id) == "starting":
                    self._codex_active_turns.pop(session.external_id, None)
        if pending:
            try:
                with self._bot_scope(pending.bot_key):
                    self._start_session_turn(
                        pending.chat_id, session, pending.text, pending.attachments
                    )
            finally:
                with self._state_lock:
                    self._drain_threads.pop(session.session_id, None)

    def _start_session_turn(
        self,
        chat_id: int,
        session: CliSession,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        bot_key: str | None = None,
    ) -> None:
        queued_notice: str | None = None
        with self._state_lock:
            draining = self._drain_threads.get(session.session_id) == threading.get_ident()
            if not draining:
                if self._session_is_busy_locked(session):
                    pending = PendingInput(
                        chat_id,
                        text,
                        attachments,
                        bot_key=bot_key or self._active_bot_key(),
                    )
                    position = self._enqueue(session, pending)
                    if position is None:
                        queued_notice = (
                            tr("{session}'s queue is full (at most {limit}).", session=session.label, limit=MAX_PENDING_INPUTS_PER_SESSION)
                        )
                    else:
                        queued_notice = (
                            tr("{session} is running; queued as #{position}.", session=session.label, position=position)
                        )
                else:
                    self._turn_claims.add(session.session_id)
                    if session.backend == "codex-app-server" and session.external_id:
                        self._codex_active_turns[session.external_id] = "starting"
        if queued_notice is not None:
            self._send(chat_id, queued_notice)
            return
        with self._backend_reload_lock:
            try:
                self._start_session_turn_locked(chat_id, session, text, attachments, bot_key)
            except Exception:
                self._finish_turn(
                    session,
                    session.external_id if session.backend == "codex-app-server" else None,
                    "starting",
                )
                raise

    def _start_session_turn_locked(
        self,
        chat_id: int,
        session: CliSession,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        bot_key: str | None = None,
    ) -> None:
        bot_key = bot_key or self._active_bot_key()
        refreshed = self.sessions.get(session.session_id)
        if self.stop_event.is_set() or not refreshed or refreshed.archived:
            self._finish_turn(session, session.external_id, "starting", drain_queue=False)
            return
        session = refreshed
        if session.cli not in self.config.enabled_clis:
            self._send(
                chat_id,
                tr("{cli} is not enabled for this bot, so the task cannot start.", cli=session.cli),
                bot_key,
            )
            self._finish_turn(
                session,
                session.external_id if session.backend == "codex-app-server" else None,
                "starting",
            )
            return
        try:
            self._telegram(bot_key).send_action(chat_id)
        except TelegramError:
            pass
        # Capture the values about to be sent. Reading the session again at
        # publish time would show a /model or /effort change that does not
        # apply to this turn.
        model_label = self._model_header_label(session)
        effort_label = self._effort_header_label(session)
        effective_model = self._effective_model(session)
        effective_effort = self._effective_effort(session)
        start_error: Exception | None = None
        try:
            with self._state_lock:
                self._starting_events[session.session_id] = []
                self._interrupted_sessions.discard(session.session_id)
            if session.backend == "codex-app-server" and session.external_id:
                with self._state_lock:
                    self._codex_active_turns[session.external_id] = "starting"
                turn_id = self.codex.start_turn(
                    session.external_id,
                    text,
                    attachments,
                    model=effective_model,
                    effort=effective_effort,
                )
                with self._state_lock:
                    self._codex_active_turns[session.external_id] = turn_id
                self._register_turn(
                    session,
                    turn_id,
                    bot_key,
                    model_label=model_label,
                    effort_label=effort_label,
                )
            elif session.backend == "headless-json":
                effective_session = replace(
                    session,
                    model=effective_model,
                    effort=effective_effort,
                )
                turn_id = self.headless.start_turn(effective_session, text, attachments)
                self.sessions.increment_turn_count(session.session_id)
                self._register_turn(
                    session,
                    turn_id,
                    bot_key,
                    model_label=model_label,
                    effort_label=effort_label,
                )
            else:
                raise SessionError(tr("Unsupported session backend: {backend}", backend=session.backend))
            self.sessions.set_in_flight(
                session.session_id, session.chat_id, turn_id, bot_key
            )
            self._set_session_state(session.session_id, STATE_WORKING)
        except (CodexBackendError, HeadlessBackendError, SessionError) as exc:
            start_error = exc
            self._send(chat_id, tr("Could not send: {error}", error=exc), bot_key)
        finally:
            # Replay under the same lock used by the callbacks so later events
            # cannot overtake a buffered completion. No RPC waits occur here.
            with self._state_lock:
                events = self._starting_events.pop(session.session_id, [])
                with self._bot_scope(bot_key):
                    for source, args in events:
                        if source == "codex":
                            self._on_codex_notification(*args)
                        else:
                            self._on_headless_event(*args)
        if start_error is not None and not events:
            ambiguous = isinstance(start_error, CodexBackendError) and start_error.ambiguous
            if ambiguous:
                self.sessions.set_in_flight(session.session_id, chat_id, "starting", bot_key)
                self._set_session_state(session.session_id, STATE_INTERRUPTED)
                self._send(chat_id, tr(
                    "The task start was not confirmed, so the queue is paused. Check the native "
                    "session first so nothing runs twice."
                ), bot_key)
            self._finish_turn(
                session,
                session.external_id if session.backend == "codex-app-server" else None,
                "starting",
                drain_queue=not ambiguous,
            )

    def _register_turn(
        self,
        session: CliSession,
        turn_id: str,
        bot_key: str | None = None,
        *,
        model_label: str | None = None,
        effort_label: str | None = None,
    ) -> TurnView:
        key = (session.session_id, turn_id)
        with self._state_lock:
            existing = self._turns.get(key)
            if existing is not None:
                if bot_key is not None:
                    existing.bot_key = bot_key
                self._assign_turn_labels(existing, session, model_label, effort_label)
                return existing
            running = [
                (old_key, view)
                for old_key, view in self._turns.items()
                if old_key[0] == session.session_id and view.status == "running"
            ]
            if running:
                _old_key, view = min(running, key=lambda item: item[1].started_at)
                if view.turn_id != turn_id:
                    self._turns.pop((session.session_id, view.turn_id), None)
                    view.turn_id = turn_id
                    self._turns[key] = view
                if bot_key is not None:
                    view.bot_key = bot_key
                self._assign_turn_labels(view, session, model_label, effort_label)
                return view
            view = TurnView(
                session=session,
                turn_id=turn_id,
                bot_key=bot_key or self._active_bot_key(),
            )
            self._assign_turn_labels(view, session, model_label, effort_label)
            self._turns[key] = view
            return view

    def _assign_turn_labels(
        self,
        view: TurnView,
        session: CliSession,
        model_label: str | None,
        effort_label: str | None,
    ) -> None:
        if not view.model_label:
            view.model_label = (
                model_label if model_label is not None else self._model_header_label(session)
            )
        if not view.effort_label:
            view.effort_label = (
                effort_label if effort_label is not None else self._effort_header_label(session)
            )

    def _update_turn(self, session: CliSession, turn_id: str, kind: str, data: Any = None) -> bool:
        with self._state_lock:
            if (session.session_id, turn_id) in self._finished_turns:
                return False
        view = self._turns.get((session.session_id, turn_id))
        if view is None:
            with self._state_lock:
                running = [
                    candidate
                    for (session_id, _candidate_turn), candidate in self._turns.items()
                    if session_id == session.session_id and candidate.status == "running"
                ]
            view = running[0] if len(running) == 1 else self._register_turn(session, turn_id)
        with self._state_lock:
            if kind == "delta" and isinstance(data, str):
                view.parts.append(data)
                if data:
                    view.notice = ""
            elif kind in {"message_started", "message_delta", "message_completed"} and isinstance(data, dict):
                item_id = data.get("id")
                if not isinstance(item_id, str):
                    return False
                message = view.assistant_messages.setdefault(item_id, AssistantMessage())
                phase = data.get("phase")
                if isinstance(phase, str) and phase in {"commentary", "final_answer"}:
                    message.phase = phase
                if not message.completed:
                    if kind == "message_delta" and isinstance(data.get("delta"), str):
                        message.parts.append(data["delta"])
                    elif kind in {"message_started", "message_completed"} and isinstance(data.get("text"), str):
                        # Completion carries the authoritative text and may be the
                        # only visible event after a reconnect. Never append it twice.
                        if data["text"]:
                            message.parts = [data["text"]]
                    if kind == "message_completed":
                        message.completed = True
                if message.parts:
                    view.notice = ""
            elif kind == "thinking":
                # Reasoning is not user-visible output. Do not retain or publish it.
                pass
            elif kind == "command":
                view.command = self._display_value(data)[:1000]
                view.command_output = ""
                view.notice = ""
            elif kind == "notice":
                view.notice = self._display_value(data)[:2000]
            elif kind == "command_output":
                view.command_output = (view.command_output + self._display_value(data))[-1200:]
            elif kind == "completed":
                view.status = "completed"
                self.sessions.clear_in_flight(session.session_id, turn_id)
                if view.turn_id != turn_id:
                    self.sessions.clear_in_flight(session.session_id, view.turn_id)
                self._interrupted_sessions.discard(session.session_id)
                self._set_session_state(session.session_id, STATE_IDLE)
            elif kind == "error":
                view.status = "failed"
                view.error = self._display_value(data)[:2000]
                self.sessions.clear_in_flight(session.session_id, turn_id)
                if view.turn_id != turn_id:
                    self.sessions.clear_in_flight(session.session_id, view.turn_id)
                if session.session_id in self._interrupted_sessions:
                    # Interrupted by the user: not a failure, back to idle.
                    self._interrupted_sessions.discard(session.session_id)
                    self._set_session_state(session.session_id, STATE_IDLE)
                else:
                    self._set_session_state(session.session_id, STATE_FAILED)
            elif kind == "interrupted":
                # Killed by a signal (gateway restart or shutdown, user interrupt, OOM, ...).
                # Keep in_flight: an unexpected interruption stays a recovery candidate for
                # /resume, /cancel or restart recovery.
                view.status = "interrupted"
                if session.session_id in self._interrupted_sessions:
                    # Interrupted by the user: _interrupt_session already cleared in_flight
                    # and replied, so finish quietly.
                    self._interrupted_sessions.discard(session.session_id)
                    self.sessions.clear_in_flight(session.session_id, turn_id)
                    self._set_session_state(session.session_id, STATE_IDLE)
                else:
                    view.resumable = True
                    self._set_session_state(session.session_id, STATE_INTERRUPTED)
            view.dirty = True
            view.revision += 1
            if kind in {"completed", "error", "interrupted"}:
                self._finished_turns.append((session.session_id, turn_id))
            return True

    @staticmethod
    def _display_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            return str(value)

    def _recover_interrupted_turns(self) -> None:
        """Handle tasks left in flight when the gateway starts.

        Tasks the user interrupted cleared in_flight at the time, so only unexpected
        interruptions remain (gateway restart or shutdown, killed processes). By default the
        user decides with /resume or /cancel; AUTO_RESUME=true resumes right away through
        the headless resume mode of the same session.
        """
        for session_id, chat_id, turn_id, bot_key in self.sessions.stale_in_flight_routes():
            if bot_key not in self._telegrams:
                LOGGER.warning("kept recovery record for %s: Bot entrance %s is not configured", session_id, bot_key)
                continue
            session = self.sessions.get(session_id)
            if session and (session.archived or session.cli not in self.config.enabled_clis):
                continue
            label = session.label if session else session_id
            resume_failed = self._session_states.get(session_id) == STATE_FAILED
            if session:
                self._set_session_state(session_id, STATE_INTERRUPTED)
            if self.config.auto_resume and session and not resume_failed:
                with self._bot_scope(bot_key):
                    self._send(
                        chat_id,
                        tr(
                            "Task interrupted by a gateway restart: {session} resumed automatically and is "
                            "continuing the unfinished work. If it already had side effects, stop it with /interrupt.",
                            session=label,
                        ),
                    )
                    self._start_session_turn(chat_id, session, RESUME_PROMPT)
            else:
                with self._bot_scope(bot_key):
                    self._send(
                        chat_id,
                        tr(
                            "⚠️ The last task was interrupted: {session} ({id}). Reply /resume to continue "
                            "or /cancel to give up.",
                            session=label, id=session_id,
                        ),
                    )

    def _on_headless_event(self, session_id: str, turn_id: str, kind: str, data: Any) -> None:
        with self._state_lock:
            if session_id in self._starting_events:
                self._starting_events[session_id].append(("headless", (session_id, turn_id, kind, data)))
                return
        session = self.sessions.get(session_id)
        if session:
            if kind == "usage":
                if isinstance(data, dict):
                    self.sessions.update_usage(
                        session_id, data.get("external_id"), data, deferred=True
                    )
                return
            if not self._update_turn(session, turn_id, kind, data):
                return
            if kind in {"completed", "error"}:
                self._finish_turn(session)
            elif kind == "interrupted":
                view = self._turns.get((session_id, turn_id))
                self._finish_turn(session, drain_queue=bool(view and not view.resumable))

    def _on_codex_notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        session = self.sessions.find_by_external_id(thread_id)
        if not session:
            return
        if method == "thread/tokenUsage/updated":
            usage = codex_usage(params.get("tokenUsage"))
            if usage:
                self.sessions.update_usage(session.session_id, thread_id, usage, deferred=True)
            return  # Usage can arrive after turn/completed; never create an answer card.
        with self._state_lock:
            if session.session_id in self._starting_events:
                self._starting_events[session.session_id].append(("codex", (method, params)))
                return
        if method == "gateway/disconnected":
            with self._state_lock:
                turn_id = self._codex_active_turns.get(thread_id)
                pending = self._pending_codex_compactions.pop(thread_id, None)
                compacting = self._codex_compaction_turns.pop((session.session_id, turn_id), None)
            if not turn_id and pending is None:
                return
            if turn_id and turn_id != "starting" and compacting is None:
                self._update_turn(session, turn_id, "interrupted")
            else:
                self._set_session_state(session.session_id, STATE_FAILED)
            self._finish_turn(session, thread_id, turn_id, drain_queue=False)
            return
        if method == "turn/started":
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if isinstance(turn_id, str):
                with self._state_lock:
                    self._codex_active_turns[thread_id] = turn_id
                    bot_key = self._pending_codex_compactions.pop(thread_id, None)
                    if bot_key is not None:
                        self._codex_compaction_turns[(session.session_id, turn_id)] = bot_key
                if bot_key is None:
                    view = self._register_turn(session, turn_id)
                    # Also covers an accepted turn whose start response was lost.
                    if self.sessions.get_in_flight(session.session_id) != turn_id:
                        self.sessions.set_in_flight(session.session_id, session.chat_id, turn_id, view.bot_key)
                    self._set_session_state(session.session_id, STATE_WORKING)
            return
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str):
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            return
        with self._state_lock:
            if (session.session_id, turn_id) in self._finished_turns:
                return
        item = params.get("item")
        if method == "item/completed" and isinstance(item, dict) and item.get("type") == "contextCompaction":
            self.sessions.update_usage(session.session_id, thread_id, {"context_tokens": None})
        compaction_key = (session.session_id, turn_id)
        with self._state_lock:
            is_compaction_turn = compaction_key in self._codex_compaction_turns
        if is_compaction_turn:
            # `/compact` already has its own command acknowledgement. Its internal
            # turn has no assistant answer and must never enter the Telegram status
            # publisher, otherwise a background reader thread routes a blank card
            # through the default Bot.
            if method == "turn/completed":
                with self._state_lock:
                    self._codex_compaction_turns.pop(compaction_key, None)
                self.sessions.update_usage(session.session_id, thread_id, {"context_tokens": None})
                self._finish_turn(session, thread_id, turn_id)
            return
        if method == "item/agentMessage/delta":
            if isinstance(params.get("itemId"), str):
                self._update_turn(session, turn_id, "message_delta", {
                    "id": params["itemId"], "delta": params.get("delta"),
                })
            else:
                self._update_turn(session, turn_id, "delta", params.get("delta"))
        elif method == "item/started":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "commandExecution":
                self._update_turn(session, turn_id, "command", item.get("command"))
            elif isinstance(item, dict) and item.get("type") == "agentMessage":
                self._update_turn(session, turn_id, "message_started", item)
        elif method == "item/commandExecution/outputDelta":
            self._update_turn(session, turn_id, "command_output", params.get("delta"))
        elif method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "commandExecution":
                output = item.get("aggregatedOutput") or item.get("output")
                if output:
                    self._update_turn(session, turn_id, "command_output", output)
            elif isinstance(item, dict) and item.get("type") == "agentMessage":
                self._update_turn(session, turn_id, "message_completed", item)
        elif method == "turn/completed":
            turn = params.get("turn")
            status = turn.get("status") if isinstance(turn, dict) else None
            error = turn.get("error") if isinstance(turn, dict) else None
            if status == "failed" or error:
                detail = error.get("message") if isinstance(error, dict) else error
                self._update_turn(session, turn_id, "error", detail or "Codex task failed")
            elif status == "interrupted":
                self._update_turn(session, turn_id, "interrupted")
            else:
                self._update_turn(session, turn_id, "completed")
            view = self._turns.get((session.session_id, turn_id))
            self._finish_turn(session, thread_id, turn_id, drain_queue=not bool(view and view.resumable))
        elif method == "error":
            # ErrorNotification is diagnostic, including when willRetry is false.
            # Only turn/completed closes the turn and releases its session claim;
            # finishing here would suppress that event and all retried output.
            error = params.get("error")
            detail = error.get("message") if isinstance(error, dict) else error
            detail = detail or params.get("message") or "Codex task error"
            label = (
                tr("Codex is retrying") if params.get("willRetry") is True
                else tr("Codex reported an error; waiting for the task to end")
            )
            self._update_turn(session, turn_id, "notice", tr(
                "{label}: {detail}", label=label, detail=self._display_value(detail),
            ))

    def _on_codex_disconnect(self) -> None:
        # Keep native IDs and recovery records. Never replay work merely because
        # the transport died; the user can reload the backend and resume.
        with self._state_lock:
            threads = set(self._codex_active_turns) | set(self._pending_codex_compactions)
        for thread_id in threads:
            self._on_codex_notification("gateway/disconnected", {"threadId": thread_id})

    def _on_codex_server_request(
        self, request_id: str | int, method: str, params: dict[str, Any]
    ) -> None:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            # Never triggered in approval-free mode; the blocked signal stays for a future
            # on-request mode.
            thread_id = params.get("threadId")
            session = (
                self.sessions.find_by_external_id(thread_id)
                if isinstance(thread_id, str)
                else None
            )
            if session:
                self._set_session_state(session.session_id, STATE_BLOCKED)
            try:
                self.codex.respond(request_id, {"decision": "acceptForSession"})
            except CodexBackendError:
                LOGGER.exception("could not auto-approve Codex request")
                return
            # The turn continues after the automatic approval.
            if session:
                self._set_session_state(session.session_id, STATE_WORKING)
            return
        LOGGER.warning("declined unsupported Codex server request: %s", method)
        try:
            self.codex.respond(request_id, {"decision": "decline"})
        except CodexBackendError:
            LOGGER.exception("could not decline unsupported Codex request")

    def _turn_progress(self, view: TurnView) -> str:
        with self._state_lock:
            bodies = ["".join(view.parts)] if view.parts else []
            bodies.extend(
                "".join(message.parts) for message in view.assistant_messages.values()
                if message.phase != "final_answer" and message.parts
            )
        return "\n\n".join(bodies)

    def _turn_answer(self, view: TurnView) -> str:
        with self._state_lock:
            final = [
                "".join(message.parts) for message in view.assistant_messages.values()
                if message.phase == "final_answer" and message.parts
            ]
            if final:
                return "\n\n".join(final).strip()
            # Providers without MessagePhase retain their existing answer behavior.
            unknown = ["".join(view.parts)] if view.parts else []
            unknown.extend(
                "".join(message.parts) for message in view.assistant_messages.values()
                if message.phase is None and message.parts
            )
        return "\n\n".join(unknown).strip()

    def _turn_has_phases(self, view: TurnView) -> bool:
        # Without native phases the streamed text is the answer itself. Sealing it
        # into reading segments would post the reply twice: as records and again
        # as the final card.
        with self._state_lock:
            return any(message.phase is not None for message in view.assistant_messages.values())

    def _render_turn(
        self, view: TurnView, now: float, background: bool = False,
        *, record_text: str | None = None,
    ) -> tuple[str, str]:
        elapsed = max(0, int(now - view.started_at))
        label = tr({
            "running": N_("Running"),
            "completed": N_("Done"),
            "failed": N_("Failed"),
            "interrupted": N_("Interrupted"),
        }.get(view.status, view.status))
        if background and view.status == "running":
            label = tr("Running in background")
        if record_text is not None:
            label = tr("Run log · part {page}", page=view.progress_page)
        status = tr("[{session}] · {label} {seconds}s", session=view.session.session_id, label=label, seconds=elapsed)
        if view.steered:
            status += tr(" · {count} added requests", count=view.steered)
        model_label = view.model_label or "CLI default"
        effort_label = view.effort_label or "CLI default"
        header = f"model: {model_label} · effort: {effort_label}\n{status}"
        answer = self._turn_answer(view)
        sections = [header]
        if record_text is not None:
            if record_text.strip():
                sections.append(record_text.strip("\n"))
            return "\n\n".join(sections), answer
        if view.status == "running":
            with self._state_lock:
                has_final = any(
                    message.phase == "final_answer" and message.parts
                    for message in view.assistant_messages.values()
                )
            if has_final or view.progress_closed or not self._turn_has_phases(view):
                body = ("…" if len(answer) > 27000 else "") + answer[-27000:]
            else:
                body, _end = stream_segment(
                    self._turn_progress(view), view.progress_offset, STREAM_SEGMENT_UNITS
                )
                if body.strip():
                    sections[0] += tr(" · part {page}", page=view.progress_page)
            if body.strip():
                sections.append(body.strip("\n"))
            if view.command and not background:
                command = view.command[-700:].replace("```", "` ` `")
                sections.append(tr("Current command:") + f"\n```text\n{command}\n```")
            if view.command_output and not background:
                output = view.command_output[-500:].replace("```", "` ` `")
                sections.append(tr("Command output:") + f"\n```text\n{output}\n```")
        elif answer:
            sections.append(answer)
        elif view.status == "completed":
            if self._turn_progress(view).strip():
                sections.append(tr("The task finished; its progress is in the run log. The CLI gave no separate final answer."))
            else:
                sections.append(tr(
                    "⚠️ The model finished without a visible answer. The gateway did not retry, "
                    "to avoid repeating actions."
                ))
        if view.status == "interrupted" and view.resumable:
            sections.append(tr("⚠️ The task was interrupted. Reply /resume to continue or /cancel to give up."))
        if view.status == "running" and view.notice:
            notice = view.notice.replace("```", "` ` `")
            sections.append(tr("Notice:") + f"\n```text\n{notice}\n```")
        if view.error:
            error = view.error.replace("```", "` ` `")
            sections.append(tr("Error:") + f"\n```text\n{error}\n```")
        return "\n\n".join(sections), answer

    def _view_card_ids(self, view: TurnView) -> list[int]:
        ids = [message_id for message_id in view.message_ids if message_id > 0]
        if not ids and view.message_id is not None and view.message_id > 0:
            ids = [view.message_id]
        return ids

    def _abandon_view_cards(self, view: TurnView) -> None:
        for message_id in self._view_card_ids(view):
            if message_id not in view.stale_message_ids:
                view.stale_message_ids.append(message_id)
        view.message_id = None
        view.message_ids = []

    def _apply_published_ids(self, view: TurnView, ids: list[int] | None) -> None:
        if not ids:
            return
        published = [message_id for message_id in ids if isinstance(message_id, int) and message_id > 0]
        if not published:
            return
        leftover = [message_id for message_id in view.message_ids if message_id not in published]
        for message_id in leftover:
            if message_id not in view.stale_message_ids:
                view.stale_message_ids.append(message_id)
        view.message_ids = published
        view.message_id = published[0]
        for message_id in published:
            self.sessions.bind_message(
                view.session.chat_id,
                message_id,
                view.session.session_id,
                view.bot_key,
            )

    def _pin_turn(self, view: TurnView, now: float) -> bool:
        """Pin the session's running progress to a new message at the bottom and stream it.

        An older card, if any, is downgraded to "running in background" and recorded in
        stale_message_ids to be closed out when the task ends. Every switch back to the
        session pins progress to the newest message again.
        """
        with self._publish_lock:
            with self._state_lock:
                current = self.sessions.current(view.session.chat_id, view.bot_key)
                if (
                    view.status != "running"
                    or view.live
                    or not current
                    or current.session_id != view.session.session_id
                ):
                    return False
                old_ids = self._view_card_ids(view)
                self._abandon_view_cards(view)
                view.dirty = True
            rendered, _ = self._render_turn(view, now)
            self._publish_running_turn(view, rendered)
            with self._state_lock:
                view.live = True
            if old_ids:
                stamp, _ = self._render_turn(view, now, background=True)
                stamp = self._one_card_text(stamp, view.rich_mode)
                for old_id in old_ids:
                    try:
                        if view.rich_mode:
                            self._telegram(view.bot_key).edit_rich_markdown(
                                view.session.chat_id, old_id, stamp
                            )
                        else:
                            self._telegram(view.bot_key).edit_markdown(
                                view.session.chat_id, old_id, stamp
                            )
                    except TelegramError as exc:
                        if exc.retry_after is not None or not exc.fallback_allowed:
                            raise
                        LOGGER.warning(
                            "could not stamp old progress card %s for %s: %s",
                            old_id,
                            view.session.session_id,
                            exc,
                        )
            return True

    def _background_turn(self, view: TurnView, now: float) -> None:
        """On switching away, freeze the current card as "running in background" and stop refreshing it."""
        rendered, _ = self._render_turn(view, now, background=True)
        rendered = self._one_card_text(rendered, view.rich_mode)
        if view.message_id is not None and view.message_id > 0:
            try:
                if view.rich_mode:
                    self._telegram(view.bot_key).edit_rich_markdown(
                        view.session.chat_id, view.message_id, rendered
                    )
                else:
                    self._telegram(view.bot_key).edit_markdown(
                        view.session.chat_id, view.message_id, rendered
                    )
            except TelegramError as exc:
                if exc.retry_after is not None or not exc.fallback_allowed:
                    raise
                view.rich_mode = False
                self._telegram(view.bot_key).edit_markdown(
                    view.session.chat_id, view.message_id, rendered
                )
        with self._state_lock:
            view.live = False

    def _resolve_stale(self, view: TurnView) -> None:
        """When a task ends, close out leftover progress cards in the history with a one-line status."""
        with self._state_lock:
            stale_ids = list(view.stale_message_ids)
            view.stale_message_ids.clear()
        if not stale_ids:
            return
        if view.status == "completed":
            stamp = tr("✅ This task finished; the result is in the newest progress message.")
        elif view.status == "failed":
            stamp = tr("⚠️ This task failed; see the newest progress message for details.")
        else:
            stamp = tr("⏹ This task was interrupted.")
        for message_id in stale_ids:
            try:
                self._telegram(view.bot_key).edit_message(
                    view.session.chat_id, message_id, stamp
                )
            except TelegramError:
                LOGGER.warning("could not resolve stale progress card %s", message_id)

    @staticmethod
    def _one_card_text(rendered: str, rich: bool) -> str:
        chunks = split_rich_markdown(rendered) if rich else split_markdown(rendered)
        return chunks[0] if chunks else rendered

    def _send_turn_cards(
        self,
        view: TurnView,
        rendered: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        rich: bool,
        max_chunks: int | None = None,
        silent: bool = False,
    ) -> list[int]:
        client = self._telegram(view.bot_key)
        send = client.send_rich_markdown_parts if rich else client.send_markdown_parts
        try:
            options = {"disable_notification": True} if silent else {}
            ids = send(
                view.session.chat_id,
                rendered,
                reply_markup=reply_markup,
                max_chunks=max_chunks,
                **options,
            )
        except TelegramError as exc:
            self._apply_published_ids(view, exc.message_ids)
            raise
        return [item for item in ids if isinstance(item, int) and item > 0]

    def _edit_turn_cards(
        self,
        view: TurnView,
        rendered: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        rich: bool,
        allow_split: bool,
    ) -> list[int]:
        client = self._telegram(view.bot_key)
        edit = client.edit_rich_markdown if rich else client.edit_markdown
        try:
            ids = edit(
                view.session.chat_id,
                view.message_id,
                rendered,
                reply_markup=reply_markup,
                extra_message_ids=view.message_ids[1:],
                allow_split=allow_split,
            )
        except TelegramError as exc:
            self._apply_published_ids(view, exc.message_ids)
            raise
        return [item for item in ids if isinstance(item, int) and item > 0]

    def _publish_running_turn(self, view: TurnView, rendered: str) -> None:
        with self._state_lock:
            progress = self._turn_progress(view)
            has_final = any(
                message.phase == "final_answer" and message.parts
                for message in view.assistant_messages.values()
            )
        if not view.progress_closed and self._turn_has_phases(view):
            if has_final:
                self._seal_progress(view, progress)
                view.progress_closed = True
                # The caller may have rendered progress just before the final answer
                # arrived; that snapshot must not become the notifying final card.
                rendered, _answer = self._render_turn(view, time.monotonic())
            else:
                rolled = False
                while view.progress_offset < len(progress):
                    chunk, end = stream_segment(progress, view.progress_offset, STREAM_SEGMENT_UNITS)
                    if end == len(progress):
                        break
                    self._archive_progress_segment(view, chunk, end)
                    rolled = True
                if rolled:
                    rendered, _answer = self._render_turn(view, time.monotonic())
        self._publish_stream_card(view, rendered, silent=not has_final)

    def _archive_progress_segment(self, view: TurnView, chunk: str, end: int) -> None:
        rendered, _answer = self._render_turn(view, time.monotonic(), record_text=chunk)
        self._publish_stream_card(view, rendered)
        # Advance only after Telegram confirms publication. Historical segments
        # stay bound to the session but must never enter duplicate-card cleanup.
        with self._state_lock:
            view.progress_offset = end
            view.progress_page += 1
            view.message_id = None
            view.message_ids = []
            view.published = False

    def _seal_progress(self, view: TurnView, progress: str) -> None:
        while view.progress_offset < len(progress):
            chunk, end = stream_segment(progress, view.progress_offset, STREAM_SEGMENT_UNITS)
            self._archive_progress_segment(view, chunk, end)

    def _publish_steer_notice(self, view: TurnView) -> None:
        with self._state_lock:
            number, boundary = view.steer_notices[0]
            progress = self._turn_progress(view)
        if not view.progress_closed and self._turn_has_phases(view):
            self._seal_progress(view, progress[:boundary])
        text = tr(
            "[{session}] The CLI accepted added request #{number}.\nFurther progress appears below this message.",
            session=view.session.session_id, number=number,
        )
        client = self._telegram(view.bot_key)
        try:
            ids = client.send_rich_markdown_parts(
                view.session.chat_id, text, max_chunks=1, disable_notification=True,
            )
        except TelegramError as exc:
            if exc.retry_after is not None or not exc.fallback_allowed:
                raise
            ids = client.send_markdown_parts(
                view.session.chat_id, text, max_chunks=1, disable_notification=True,
            )
        if not ids:
            raise TelegramError("steer acknowledgement returned no message ID", fallback_allowed=False)
        for message_id in ids:
            self.sessions.bind_message(view.session.chat_id, message_id, view.session.session_id, view.bot_key)
        with self._state_lock:
            view.steer_notices.popleft()
            # If there was only a command/status card, or a final was already
            # streaming, retire that duplicate so the next update follows the ack.
            if view.message_id is not None:
                self._abandon_view_cards(view)
            view.published = False
            view.dirty = True

    def _publish_stream_card(self, view: TurnView, rendered: str, *, silent: bool = True) -> None:
        # Edit just the active segment. Telegram Rich Message
        # Drafts cannot be finalized or removed by the gateway once interrupted,
        # leaving an eternal "loading" bubble in the chat, so they are not used.
        rendered = self._one_card_text(rendered, view.rich_mode)
        if view.rich_mode:
            try:
                if view.message_id is None:
                    ids = self._send_turn_cards(view, rendered, rich=True, max_chunks=1, silent=silent)
                    if ids:
                        self._apply_published_ids(view, ids)
                    else:
                        view.message_id = 0
                elif view.message_id > 0:
                    self._edit_turn_cards(view, rendered, rich=True, allow_split=False)
                view.published = True
                return
            except TelegramError as exc:
                if exc.retry_after is not None or not exc.fallback_allowed:
                    raise
                view.rich_mode = False
                LOGGER.warning(
                    "Rich Message streaming unavailable for %s; using HTML fallback: %s",
                    view.session.session_id,
                    exc,
                )
                rendered = self._one_card_text(rendered, False)
        if view.message_id is None:
            ids = self._send_turn_cards(view, rendered, rich=False, max_chunks=1, silent=silent)
            if ids:
                self._apply_published_ids(view, ids)
            else:
                view.message_id = 0
        elif view.message_id > 0:
            self._edit_turn_cards(view, rendered, rich=False, allow_split=False)
        view.published = True

    def _is_view_current(self, view: TurnView) -> bool:
        current = self.sessions.current(view.session.chat_id, view.bot_key)
        return bool(current and current.session_id == view.session.session_id)

    def _publish_final_turn(
        self,
        view: TurnView,
        rendered: str,
        with_artifacts: bool = True,
        *,
        allow_split: bool = True,
        allow_new_messages: bool = True,
        auto_send: bool | None = None,
    ) -> bool:
        if auto_send is None:
            auto_send = with_artifacts
        if not allow_new_messages and (view.message_id is None or view.message_id <= 0):
            return False
        if allow_new_messages and not view.progress_closed and self._turn_has_phases(view):
            with self._state_lock:
                progress = self._turn_progress(view)
            self._seal_progress(view, progress)
            view.progress_closed = True
        if with_artifacts:
            view.artifacts = self._find_artifacts(view.session, self._turn_answer(view))
            markup = self._artifact_markup(view)
        else:
            view.artifacts = []
            markup = None
        try:
            if view.message_id is not None and view.message_id > 0:
                ids = self._edit_turn_cards(
                    view, rendered, markup, rich=True, allow_split=allow_split
                )
            elif allow_new_messages:
                ids = self._send_turn_cards(view, rendered, markup, rich=True)
            else:
                return False
            if ids:
                self._apply_published_ids(view, ids)
            elif view.message_id is None:
                view.message_id = 0
            view.published = True
            if auto_send:
                self._auto_send_artifacts(view)
            return True
        except TelegramError as exc:
            if exc.retry_after is not None or not exc.fallback_allowed:
                raise
            LOGGER.warning(
                "Rich Message final unavailable for %s; using HTML fallback: %s",
                view.session.session_id,
                exc,
            )
        if view.message_id is not None and view.message_id > 0:
            ids = self._edit_turn_cards(
                view, rendered, markup, rich=False, allow_split=allow_split
            )
        elif allow_new_messages:
            ids = self._send_turn_cards(view, rendered, markup, rich=False)
        else:
            return False
        if ids:
            self._apply_published_ids(view, ids)
        elif view.message_id is None:
            view.message_id = 0
        view.published = True
        if auto_send:
            self._auto_send_artifacts(view)
        return True

    # 45 MiB by default with the cloud Bot API; 1 GiB with a local --local server.
    @property
    def artifact_send_limit(self) -> int:
        return self.config.telegram_max_file_bytes

    def _auto_send_artifacts(self, view: TurnView) -> None:
        """Send the CLI's artifact files to the user after the answer is published.

        Modes (AUTO_SEND_ARTIFACTS):
          off    - send nothing automatically; keep the "send file/image/video" buttons (default)
          images - send only images within the sendPhoto limit
          all    - send images, MP4 videos and other files as their Telegram types

        An artifact over the Bot API upload limit is never sent automatically and gets no
        "send file" button: the card offers only a button that returns its local path.

        Sent paths are recorded in view.auto_sent so publication retries never resend them;
        files that fail stay on their buttons as a manual fallback. Called only when the
        current session's final state is published; a background completion only updates
        the buttons on its own card and never drops files at the bottom of the chat.
        """
        self._auto_send_paths(view.session.chat_id, view.bot_key, view.artifacts, view.auto_sent)

    def _auto_send_paths(
        self, chat_id: int, bot_key: str, paths: list[Path], auto_sent: list[str]
    ) -> None:
        mode = self.config.auto_send_artifacts
        if mode == "off":
            return
        for path in paths:
            if len(auto_sent) >= MAX_AUTO_SENT_ARTIFACTS:
                break
            key = str(path)
            if key in auto_sent:
                continue
            try:
                path = self._validated_local_file(path)
                size = path.stat().st_size
            except (OSError, ValueError):
                continue
            mime_type = mimetypes.guess_type(path.name)[0] or ""
            if mime_type.startswith("image/") and size <= PHOTO_UPLOAD_MAX_BYTES:
                send_options, label = {"as_photo": True}, "image"
            elif mode == "all":
                if path.suffix.lower() == ".mp4":
                    send_options, label = {"as_video": True}, "video"
                else:
                    send_options, label = {"as_photo": False}, "file"
            else:
                continue  # an image over the sendPhoto limit without mode all keeps its button
            try:
                self._telegram(bot_key).send_local_file(chat_id, path, **send_options)
            except (TelegramError, OSError) as exc:
                LOGGER.warning("could not auto-send %s %s: %s", label, path, exc)
                continue
            auto_sent.append(key)

    def _validated_local_file(self, candidate: Path) -> Path:
        """Validate a local artifact that may be sent (directory and send limit)."""
        candidate = self._validated_artifact_path(candidate)
        if candidate.stat().st_size > self.config.telegram_max_file_bytes:
            raise ValueError(tr("The file exceeds the configured send size limit"))
        return candidate

    def _validated_artifact_path(self, candidate: Path) -> Path:
        """Validate a local artifact whose path may be returned: it only has to exist inside
        ALLOWED_WORKDIRS.

        A file over the Bot API upload limit is still worth telling the user about; it just
        cannot be sent as a file.
        """
        candidate = candidate.expanduser().resolve(strict=True)
        if not candidate.is_file():
            raise ValueError(tr("Not a regular file"))
        if not any(
            candidate == root or candidate.is_relative_to(root)
            for root in self.config.allowed_roots
        ):
            raise ValueError(tr("The file is not inside ALLOWED_WORKDIRS"))
        return candidate

    def _find_artifacts(self, session: CliSession, answer: str) -> list[Path]:
        return self._find_artifacts_in(Path(session.cwd), answer)

    def _find_artifacts_in(self, workdir: Path, answer: str) -> list[Path]:
        candidates: list[str] = []
        candidates.extend(re.findall(r"`([^`\n]{1,500})`", answer))
        candidates.extend(re.findall(r"\]\(([^)\n]{1,500})\)", answer))
        candidates.extend(re.findall(r"(?<![\w:])(/[^\s`)\]}>]{1,500})", answer))
        found: list[Path] = []
        for raw in candidates:
            raw = raw.strip().strip("'\"<>.,;：，。")
            raw = re.sub(r":\d+(?::\d+)?$", "", raw)
            if not raw or (not Path(raw).is_absolute() and "/" not in raw and "." not in raw):
                continue
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = workdir / candidate
            try:
                candidate = self._validated_artifact_path(candidate)
            except (OSError, ValueError):
                continue
            if candidate not in found:
                found.append(candidate)
            if len(found) >= 6:
                break
        return found

    def _artifact_markup(self, view: TurnView) -> dict[str, Any] | None:
        return self._artifact_buttons(
            view.session.chat_id,
            view.session.session_id,
            view.bot_key,
            view.artifacts,
            view.artifact_tokens,
        )

    def _artifact_buttons(
        self,
        chat_id: int,
        owner_id: str,
        bot_key: str,
        paths: list[Path],
        tokens: dict[str, str],
    ) -> dict[str, Any] | None:
        """Build send buttons for local artifacts.

        Tokens are reused per path, so publish retries and refreshed cards do not
        fill the bounded artifact table with duplicates.
        """
        rows: list[list[dict[str, str]]] = []
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            oversized = size > self.artifact_send_limit
            if oversized:
                action, label = "sendpath", N_("Send path: {name}")
            else:
                mime_type = mimetypes.guess_type(path.name)[0] or ""
                if mime_type.startswith("image/"):
                    action, label = "photo", N_("Send image: {name}")
                elif path.suffix.lower() == ".mp4":
                    action, label = "video", N_("Send video: {name}")
                else:
                    action, label = "file", N_("Send file: {name}")
            token = tokens.get(str(path))
            if token is None:
                token = self.sessions.register_artifact(
                    chat_id, owner_id, path, bot_key, only_path=oversized
                )
                tokens[str(path)] = token
            rows.append([{
                "text": tr(label, name=path.name)[:60],
                "callback_data": f"{action}:{token}",
            }])
        return {"inline_keyboard": rows} if rows else None

    # --- Direct command extensions ---
    #
    # Command cards deliberately bypass _turns/TurnView: a command has no CLI session, no
    # queue and no last_completed_message, and switching sessions must not copy its result.
    # They reuse the stable pieces: artifact validation, send buttons and in-place edits.

    def _render_direct_command(self, run: DirectCommandRun, now: float) -> tuple[str, str]:
        elapsed = max(0, int(now - run.started_at))
        label = tr({
            "running": N_("Running"),
            "completed": N_("Done"),
            "failed": N_("Failed"),
            "interrupted": N_("Interrupted"),
        }.get(run.status, run.status))
        header = f"/{run.command.command} · {run.command.display_name()}\n" + tr(
            "{label} {seconds}s", label=label, seconds=elapsed,
        )
        sections = [header]
        if run.args:
            sections.append(tr("Arguments: `{args}`", args=" ".join(run.args)[:300]))
        output = run.output_text()
        limit = run.command.max_output_bytes
        truncated = len(output) > limit
        visible = output[-limit:] if truncated else output
        if run.status == "running":
            if run.progress:
                sections.append(tr("Progress: {progress}", progress=run.progress))
            if visible:
                sections.append(tr("Output:") + "\n```text\n" + visible + "\n```")
            if not run.progress and not visible:
                sections.append(tr("Started; waiting for output…"))
        else:
            if truncated:
                sections.append(tr("(Only the last {limit} bytes of output are kept)", limit=limit))
            # Command output is mostly pre-formatted text (help, progress, path lists).
            # Telegram's Rich Message renderer joins single newlines into one paragraph,
            # so hard breaks keep the extension's own layout.
            sections.append(harden_rich_markdown(visible) or tr("The command printed nothing."))
        error = run.error_text()
        if error and (run.status != "completed" or run.timed_out):
            error = error[-limit:].replace("```", "` ` `")
            sections.append(tr("Error:") + "\n```text\n" + error + "\n```")
        return "\n\n".join(sections), output

    def _direct_artifact_markup(self, run: DirectCommandRun) -> dict[str, Any] | None:
        return self._artifact_buttons(
            run.chat_id,
            run.turn_id,
            run.bot_key,
            self._find_artifacts_in(run.command.cwd, run.artifacts_source()),
            run.artifact_tokens,
        )

    def _auto_send_direct_artifacts(self, run: DirectCommandRun, markup: dict[str, Any] | None) -> None:
        if markup is None:
            return
        self._auto_send_paths(
            run.chat_id,
            run.bot_key,
            self._find_artifacts_in(run.command.cwd, run.artifacts_source()),
            run.auto_sent,
        )

    def _publish_direct_command(
        self, run: DirectCommandRun, markup: dict[str, Any] | None = None
    ) -> bool:
        """Send a new card or edit the old one in place; return whether Telegram was written."""
        now = time.monotonic()
        rendered, _output = self._render_direct_command(run, now)
        rendered = self._one_card_text(rendered, run.rich_mode)
        # Telegram I/O runs under the publish lock only; the state lock is shared
        # with every CLI event callback and must not wait on the network.
        with self._publish_lock:
            with self._state_lock:
                if (
                    run.status == "running"
                    and run.message_id is None
                    and not self._direct_run_is_foreground(run)
                ):
                    # The user switched away, or another command card is already at the bottom:
                    # don't take the bottom spot; edit the original card at the final state.
                    return False
            try:
                if run.rich_mode:
                    if run.message_id is None:
                        ids = self._send_direct_cards(run, rendered, markup, rich=True)
                        if not ids:
                            return False
                        self._apply_direct_ids(run, ids)
                    elif run.message_id > 0:
                        self._edit_direct_cards(run, rendered, markup, rich=True)
                    run.published = True
                    with self._state_lock:
                        run.last_edit = now
                        run.publish_failures = 0
                        run.next_publish_at = 0.0
                        run.dirty = False
                    return True
            except TelegramError as exc:
                if exc.retry_after is not None or not exc.fallback_allowed:
                    raise
                run.rich_mode = False
                LOGGER.warning(
                    "Rich Message unavailable for /%s; using HTML fallback: %s",
                    run.command.command,
                    exc,
                )
            if run.message_id is None:
                ids = self._send_direct_cards(run, rendered, markup, rich=False)
                if not ids:
                    return False
                self._apply_direct_ids(run, ids)
            elif run.message_id > 0:
                self._edit_direct_cards(run, rendered, markup, rich=False)
            run.published = True
            with self._state_lock:
                run.last_edit = now
                run.publish_failures = 0
                run.next_publish_at = 0.0
                run.dirty = False
            return True

    def _send_direct_cards(
        self,
        run: DirectCommandRun,
        rendered: str,
        reply_markup: dict[str, Any] | None,
        *,
        rich: bool,
    ) -> list[int]:
        client = self._telegram(run.bot_key)
        send = client.send_rich_markdown_parts if rich else client.send_markdown_parts
        ids = send(run.chat_id, rendered, reply_markup=reply_markup, max_chunks=1)
        return [item for item in ids if isinstance(item, int) and item > 0]

    def _edit_direct_cards(
        self,
        run: DirectCommandRun,
        rendered: str,
        reply_markup: dict[str, Any] | None,
        *,
        rich: bool,
    ) -> list[int]:
        client = self._telegram(run.bot_key)
        edit = client.edit_rich_markdown if rich else client.edit_markdown
        ids = edit(
            run.chat_id,
            run.message_id,
            rendered,
            reply_markup=reply_markup,
            extra_message_ids=run.message_ids[1:],
            allow_split=False,
        )
        return [item for item in ids if isinstance(item, int) and item > 0]

    def _apply_direct_ids(self, run: DirectCommandRun, ids: list[int]) -> None:
        published = [item for item in ids if isinstance(item, int) and item > 0]
        if not published:
            return
        run.message_ids = published
        run.message_id = published[0]
        for message_id in published:
            self.sessions.bind_command_message(run.chat_id, message_id, run.turn_id, run.bot_key)

    def _direct_run_is_foreground(self, run: DirectCommandRun) -> bool:
        newest = max(
            self.direct_commands.running_for_chat(run.chat_id, run.bot_key),
            key=lambda item: item.started_at,
            default=None,
        )
        return newest is not None and newest.turn_id == run.turn_id

    def _refresh_direct_command_card(self, run: DirectCommandRun) -> None:
        """Send the first card right after the command starts; a failure is only logged and
        the status loop retries."""
        try:
            self._publish_direct_command(run)
        except TelegramError as exc:
            delay = self._schedule_direct_retry(run, exc, time.monotonic())
            LOGGER.warning(
                "could not send first card for /%s; retrying in %.1fs: %s",
                run.command.command,
                delay,
                exc,
            )

    @staticmethod
    def _schedule_direct_retry(run: DirectCommandRun, error: TelegramError, now: float) -> float:
        run.publish_failures += 1
        if error.retry_after is not None:
            delay = max(1.0, error.retry_after)
        else:
            delay = min(MAX_PUBLISH_RETRY_SECONDS, 2.0 ** run.publish_failures)
        run.next_publish_at = now + delay
        return delay

    def _pump_direct_commands(self, now: float) -> None:
        """Publish direct command cards from the status loop; drop the runtime state once the
        final state is published."""
        for run in self.direct_commands.runs():
            try:
                # Lock order matches the CLI path: publish lock, then state lock.
                with self._publish_lock:
                    with self._state_lock:
                        if now < run.next_publish_at:
                            continue
                        # The runner thread may finish the command while this card is
                        # published; only a status seen here may release the run.
                        status = run.status
                        if status == "running":
                            if not self._direct_run_is_foreground(run):
                                # Backstage runs never take the bottom slot and are
                                # not refreshed periodically; the terminal pass
                                # still edits the card created at start.
                                continue
                            if run.published and not (
                                run.dirty
                                and now - run.last_edit >= self.config.stream_update_interval
                            ):
                                continue
                    markup = None if status == "running" else self._direct_artifact_markup(run)
                    published = self._publish_direct_command(run, markup)
            except TelegramError as exc:
                delay = self._schedule_direct_retry(run, exc, now)
                LOGGER.warning(
                    "could not update card for /%s; retrying in %.1fs: %s",
                    run.command.command,
                    delay,
                    exc,
                )
                continue
            if published and status != "running":
                if status == "completed":
                    self._auto_send_direct_artifacts(run, markup)
                # The final state is in Telegram, so the runtime state can go: reply routes
                # and file buttons live in the sessions route and artifact tables.
                self.direct_commands.forget(run.turn_id)

    def _status_loop(self) -> None:
        while not self.stop_event.wait(0.5):
            now = time.monotonic()
            self._pump_direct_commands(now)
            with self._state_lock:
                views = list(self._turns.values())
            for view in views:
                action: str | None = None
                published_status: str | None = None
                rendered: str | None = None
                finished = False
                with self._state_lock:
                    if now < view.next_publish_at:
                        continue
                    is_current = self._is_view_current(view)
                    if is_current and view.steer_notices:
                        action = "steer_notice"
                    elif view.status == "running":
                        if is_current and not view.live:
                            action = "pin"
                        elif not is_current and view.live:
                            action = "background"
                        elif not is_current:
                            # Frozen in the background: no periodic refresh until the final
                            # state or a switch back.
                            continue
                    elif not is_current:
                        # A background final state never posts or auto-sends at the bottom of the chat.
                        if view.message_id is not None and view.message_id > 0:
                            action = "background_final"
                            published_status = view.status
                            rendered, _answer = self._render_turn(view, now)
                        else:
                            continue
                    if action is None:
                        # Coalesce meaningful progress; elapsed time alone does not
                        # justify another Telegram edit and can trigger flood control.
                        should_update = (
                            not view.published
                            or view.status != "running"
                            or (
                                view.dirty
                                and now - view.last_edit >= self.config.stream_update_interval
                            )
                        )
                        if not should_update:
                            continue
                        published_status = view.status
                        rendered, _answer = self._render_turn(view, now)
                    published_revision = view.revision
                try:
                    with self._publish_lock:
                        with self._state_lock:
                            if self._turns.get((view.session.session_id, view.turn_id)) is not view:
                                continue
                            if self._is_view_current(view) != is_current:
                                continue
                        if action == "steer_notice":
                            self._publish_steer_notice(view)
                        elif action == "pin":
                            if not self._pin_turn(view, now):
                                continue
                        elif action == "background":
                            self._background_turn(view, now)
                        elif action == "background_final":
                            if not self._publish_final_turn(
                                view,
                                rendered or "",
                                with_artifacts=published_status != "interrupted",
                                allow_split=False,
                                allow_new_messages=False,
                                auto_send=False,
                            ):
                                continue
                        elif published_status == "running":
                            self._publish_running_turn(view, rendered)
                        else:
                            self._publish_final_turn(
                                view,
                                rendered,
                                with_artifacts=published_status != "interrupted",
                                auto_send=False,
                            )
                except TelegramError as exc:
                    delay = self._schedule_publish_retry(view, exc, now)
                    LOGGER.warning(
                        "could not update Telegram message for %s; retrying in %.1fs: %s",
                        view.session.session_id,
                        delay,
                        exc,
                    )
                    continue
                with self._state_lock:
                    view.last_edit = now
                    view.publish_failures = 0
                    view.next_publish_at = 0.0
                    if action == "steer_notice":
                        continue
                    if action is None and view.status != published_status:
                        # A terminal event arrived while Telegram was publishing the
                        # running snapshot. Keep the view so the next pass replaces
                        # that stale message with the terminal status.
                        view.dirty = True
                        continue
                    if view.revision != published_revision:
                        # Output or an accepted steer arrived during the network
                        # call. Preserve it for the next publication, including
                        # acknowledgements racing with a terminal answer.
                        view.dirty = True
                        continue
                    finished = (
                        published_status is not None
                        and published_status != "running"
                        and action in {None, "background_final"}
                    )
                    if (
                        finished
                        and published_status == "completed"
                        and view.message_id is not None
                        and view.message_id > 0
                    ):
                        # Save the pointer before removing the view so a concurrent
                        # session switch cannot briefly copy the previous result.
                        self.sessions.set_last_completed_message(
                            view.session.session_id, view.message_id, view.bot_key
                        )
                        if rendered is not None:
                            self._last_completed_results[view.session.session_id] = rendered
                    view.dirty = False
                    if finished:
                        self._turns.pop((view.session.session_id, view.turn_id), None)
                if finished:
                    if action is None:
                        # Uploads can take seconds; session switches and steering must
                        # not wait for them behind the publish lock.
                        self._auto_send_artifacts(view)
                    self._resolve_stale(view)

    @staticmethod
    def _schedule_publish_retry(view: TurnView, error: TelegramError, now: float) -> float:
        view.publish_failures += 1
        if error.retry_after is not None:
            delay = max(1.0, error.retry_after)
        else:
            delay = float(2 ** min(view.publish_failures - 1, 5))
            delay = min(MAX_PUBLISH_RETRY_SECONDS, delay)
        view.next_publish_at = now + delay
        return delay

    def _safe_upload_name(self, name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._")
        return cleaned[:120] or "upload.bin"

    def _download_attachment(self, message: dict[str, Any], session: CliSession) -> Attachment | None:
        file_id: str | None = None
        name = ""
        mime_type = "application/octet-stream"
        is_image = False
        field = _file_field(message)
        photos = message.get("photo")
        if field:
            media = message[field]
            file_id = media.get("file_id") if isinstance(media.get("file_id"), str) else None
            name = str(media.get("file_name") or "")
            mime_type = str(
                media.get("mime_type")
                or mimetypes.guess_type(name)[0]
                or TELEGRAM_FILE_FIELDS[field]
            )
            if not name:
                # Voice and video notes have no file name; videos shot on a phone usually lack one too.
                unique = str(media.get("file_unique_id") or uuid.uuid4().hex)
                name = f"{field}-{unique}{mimetypes.guess_extension(mime_type) or '.bin'}"
            is_image = mime_type.startswith("image/")
        elif isinstance(photos, list) and photos:
            photo = max(
                (item for item in photos if isinstance(item, dict)),
                key=lambda item: int(item.get("file_size") or 0),
                default={},
            )
            file_id = photo.get("file_id") if isinstance(photo.get("file_id"), str) else None
            unique = str(photo.get("file_unique_id") or uuid.uuid4().hex)
            name = f"photo-{unique}.jpg"
            mime_type = "image/jpeg"
            is_image = True
        if not file_id:
            return None
        safe_name = self._safe_upload_name(name)
        destination = (
            self.config.runtime_dir
            / "uploads"
            / str(session.chat_id)
            / session.session_id
            / f"{uuid.uuid4().hex}_{safe_name}"
        )
        self.telegram.download_file(file_id, destination, self.config.telegram_max_file_bytes)
        return Attachment(destination, safe_name, mime_type, is_image)

    def _prepare_sessions(self) -> None:
        for session in self.sessions.all_sessions():
            if (
                session.cli != "codex"
                or session.cli not in self.config.enabled_clis
                or session.backend != "codex-app-server"
                or not session.external_id
            ):
                continue
            try:
                self.codex.resume_thread(session.external_id, model=self._effective_model(session))
            except CodexBackendError:
                LOGGER.exception("could not resume Codex thread for %s", session.session_id)
                # A temporary resume error is not permission to discard the user's
                # native context and silently create a thread.
                self._set_session_state(session.session_id, STATE_FAILED)

    def _coalesce_updates(self, updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge the messages of one media_group_id into a single update.

        Telegram delivers every item of an album or multi-file group as its own message
        sharing a media_group_id. Handled one by one, the first image would start a task and
        the rest would become steers or queued turns instead of one task. Adjacent messages
        with the same media_group_id in a batch become one message carrying a ``media`` list,
        and update_id becomes the group's highest so nothing is consumed twice. A group
        split across batches (rare) is unaffected and handled message by message.
        """
        merged: list[dict[str, Any]] = []
        index = 0
        while index < len(updates):
            update = updates[index]
            message = update.get("message")
            group_id = message.get("media_group_id") if isinstance(message, dict) else None
            if not isinstance(group_id, str):
                merged.append(update)
                index += 1
                continue
            members = [update]
            max_update_id: Any = update.get("update_id")
            index += 1
            while index < len(updates):
                candidate = updates[index]
                candidate_message = candidate.get("message")
                if (
                    isinstance(candidate_message, dict)
                    and candidate_message.get("media_group_id") == group_id
                ):
                    members.append(candidate)
                    candidate_id = candidate.get("update_id")
                    if isinstance(candidate_id, int) and (
                        not isinstance(max_update_id, int) or candidate_id > max_update_id
                    ):
                        max_update_id = candidate_id
                    index += 1
                else:
                    break
            merged.append(self._merge_media_members(members, max_update_id))
        return merged

    @staticmethod
    def _merge_media_members(
        members: list[dict[str, Any]], max_update_id: Any
    ) -> dict[str, Any]:
        first = members[0].get("message")
        first = first if isinstance(first, dict) else {}
        media: list[dict[str, Any]] = []
        caption = ""
        for member in members:
            message = member.get("message")
            if not isinstance(message, dict):
                continue
            field = _file_field(message)
            photos = message.get("photo")
            if field:
                media.append({field: message[field]})
            elif isinstance(photos, list):
                media.append({"photo": photos})
            if not caption:
                item_caption = message.get("caption")
                if isinstance(item_caption, str) and item_caption.strip():
                    caption = item_caption.strip()
        combined = {
            key: value
            for key, value in first.items()
            if key not in {*TELEGRAM_FILE_FIELDS, "photo", "caption"}
        }
        combined["media"] = media
        if caption:
            combined["caption"] = caption
        return {"update_id": max_update_id, "message": combined}

    def _coalesce_text_fragments(
        self, updates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Join long text that the client split back into one update.

        Clients split long text into ordinary messages without a grouping marker such as
        media_group_id: parts arrive in order and all but the last are at least
        TEXT_FRAGMENT_MIN_CHARS long. Only this conservative pattern is merged, so several
        separate messages are never mistaken for one. Parts from later batches are first
        gathered by _await_text_fragments.
        """
        merged: list[dict[str, Any]] = []
        index = 0
        while index < len(updates):
            update = updates[index]
            first = self._fragment_message(update)
            if first is None or not self._may_be_continued(first):
                merged.append(update)
                index += 1
                continue
            group = [update]
            total = len(first["text"].encode())
            last = first
            index += 1
            while (
                len(group) < TEXT_FRAGMENT_MAX_PARTS
                and self._may_be_continued(last)
                and index < len(updates)
            ):
                candidate = self._fragment_message(updates[index])
                if candidate is None or not self._same_fragment_run(last, candidate):
                    break
                size = len(candidate["text"].encode())
                if total + size > TEXT_FRAGMENT_MAX_TOTAL_BYTES:
                    break
                group.append(updates[index])
                total += size
                last = candidate
                index += 1
            merged.append(self._stitch_text_fragments(group) if len(group) > 1 else update)
        return merged

    @staticmethod
    def _may_be_continued(message: dict[str, Any]) -> bool:
        """Every non-final part a client splits off exceeds 2048 units (Desktop looks for a
        break only in the second half); a shorter part is the last one."""
        return _utf16_len(message["text"]) >= TEXT_FRAGMENT_MIN_CHARS

    @staticmethod
    def _fragment_message(update: dict[str, Any]) -> dict[str, Any] | None:
        """Only plain text messages are merged; attachments, albums and forwards are never
        parts of a split text."""
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        if any(
            key in message
            for key in (
                "caption",
                "document",
                "forward_origin",
                "media",
                "media_group_id",
                "photo",
                "sticker",
                "voice",
            )
        ):
            return None
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return message

    @staticmethod
    def _same_fragment_run(previous: dict[str, Any], candidate: dict[str, Any]) -> bool:
        def identity(message: dict[str, Any]) -> tuple[Any, ...]:
            chat = message.get("chat")
            sender = message.get("from")
            return (
                chat.get("id") if isinstance(chat, dict) else None,
                sender.get("id") if isinstance(sender, dict) else None,
                message.get("message_thread_id"),
                message.get("direct_messages_topic_id"),
            )

        # Only the first part may carry reply_to_message (a long reply to some message).
        # Later parts that also reply are separate replies and must not be joined.
        if isinstance(candidate.get("reply_to_message"), dict):
            return False
        if identity(previous) != identity(candidate):
            return False
        previous_id = previous.get("message_id")
        candidate_id = candidate.get("message_id")
        if (
            not isinstance(previous_id, int)
            or not isinstance(candidate_id, int)
            or not 0 < candidate_id - previous_id <= 2
        ):
            return False
        previous_date = previous.get("date")
        candidate_date = candidate.get("date")
        if (
            isinstance(previous_date, int)
            and isinstance(candidate_date, int)
            and not 0 <= candidate_date - previous_date <= 2
        ):
            return False
        return True

    @staticmethod
    def _stitch_text_fragments(group: list[dict[str, Any]]) -> dict[str, Any]:
        # Entity offsets no longer match the joined text, so drop them.
        first = {key: value for key, value in group[0]["message"].items() if key != "entities"}
        parts = [part["message"]["text"] for part in group]
        multiline = any("\n" in part for part in parts)
        text = parts[0]
        for previous, following in pairwise(parts):
            text += _fragment_separator(previous, following, multiline) + following
        first["text"] = text
        return {"update_id": group[-1].get("update_id"), "message": first}

    def _await_text_fragments(
        self, telegram: TelegramClient, offset: int | None, updates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """When a batch ends like an unfinished long text, wait for the rest before dispatching.

        Clients send parts one by one and the Bot API ends the long poll at the first, so
        later parts land in the next batch. Polling again with the same offset leaves the
        fetched updates unconfirmed, so a restart while waiting loses nothing. Once the parts
        stop growing, the last part is short, or the wait cap is reached, the batch goes to
        _coalesce_text_fragments.
        """
        started = last_growth = time.monotonic()
        while updates:
            tail = self._fragment_message(updates[-1])
            if tail is None or not self._may_be_continued(tail):
                break
            now = time.monotonic()
            if (
                now - last_growth >= TEXT_FRAGMENT_QUIET_SECONDS
                or now - started >= TEXT_FRAGMENT_MAX_WAIT_SECONDS
                or self.stop_event.wait(TEXT_FRAGMENT_POLL_SECONDS)
            ):
                break
            refreshed = telegram.get_updates(offset, 0)
            if len(refreshed) > len(updates):
                updates = refreshed
                last_growth = time.monotonic()
        return updates

    def handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback_query(callback)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender = message.get("from", {})
        user_id = sender.get("id")
        chat_id = message.get("chat", {}).get("id")
        chat_type = message.get("chat", {}).get("type")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return
        if user_id not in self.config.allowed_user_ids:
            LOGGER.warning("ignored Telegram message from unauthorized user %s", user_id)
            return
        if chat_type != "private" and not self.config.allow_groups:
            LOGGER.info("ignored message from non-private chat %s", chat_id)
            return
        routed_session: CliSession | None = None
        replied = message.get("reply_to_message")
        replied_id = replied.get("message_id") if isinstance(replied, dict) else None
        if isinstance(replied_id, int):
            # A reply to a direct command card must not send text into the current CLI session.
            if self.sessions.command_route_for_message(
                chat_id, replied_id, self._active_bot_key()
            ):
                return
            routed_session = self.sessions.session_for_message(
                chat_id, replied_id, self._active_bot_key()
            )
            if routed_session and self.sessions.is_alive(routed_session):
                try:
                    self._switch_session(chat_id, routed_session.session_id)
                except SessionError:
                    routed_session = None
            else:
                routed_session = None
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            text = text.strip()
            if text.startswith("/"):
                head, separator, raw_args = text.partition(" ")
                command = head[1:].split("@", 1)[0].lower()
                self._bot_local.user_id = user_id if isinstance(user_id, int) else 0
                self._handle_command(chat_id, command, raw_args if separator else "")
            else:
                if routed_session:
                    self._send_to_session(chat_id, routed_session, text)
                else:
                    self._send_to_current(chat_id, text)
            return
        media_items = message.get("media")
        if isinstance(media_items, list) and media_items:
            session = routed_session or self._current_or_reply(chat_id)
            if not session:
                return
            attachments: list[Attachment] = []
            for item in media_items:
                if not isinstance(item, dict):
                    continue
                try:
                    attachment = self._download_attachment(item, session)
                except TelegramError as exc:
                    self._send(chat_id, tr("Could not receive the file: {error}", error=exc))
                    return
                if attachment:
                    attachments.append(attachment)
            if not attachments:
                self._send(chat_id, tr("Could not recognize this attachment."))
                return
            caption = message.get("caption")
            prompt = caption.strip() if isinstance(caption, str) and caption.strip() else "Please review and handle these attachments."
            self._send_to_session(chat_id, session, prompt, tuple(attachments))
            return
        if _file_field(message) or isinstance(message.get("photo"), list):
            session = routed_session or self._current_or_reply(chat_id)
            if not session:
                return
            try:
                attachment = self._download_attachment(message, session)
            except TelegramError as exc:
                self._send(chat_id, tr("Could not receive the file: {error}", error=exc))
                return
            if not attachment:
                self._send(chat_id, tr("Could not recognize this attachment."))
                return
            caption = message.get("caption")
            prompt = caption.strip() if isinstance(caption, str) and caption.strip() else "Please review and handle this attachment."
            self._send_to_session(chat_id, session, prompt, (attachment,))
            return
        self._send(chat_id, tr(
            "Text and files of every kind (images, audio, video, voice and more) are supported; "
            "stickers and similar messages are not forwarded to the CLI."
        ))

    def stop(self, *_args: object) -> None:
        self.codex.begin_shutdown()
        self.direct_commands.shutdown()
        self.stop_event.set()

    def _dispatch_update(self, bot_key: str, update: dict[str, Any]) -> None:
        # Keep the pre-multi-bot single-dispatch ordering while each Bot long-polls
        # independently. Backend streams still run on their existing worker threads.
        with self._dispatch_lock:
            with self._bot_scope(bot_key):
                self.handle_update(update)

    def _command_menu_entries(self) -> tuple[tuple[str, str], ...]:
        """Extension commands also go into Telegram's / menu so users need not guess."""
        return tuple(
            (command.command, (command.display_description() or command.display_name())[:120])
            for command in sorted(self.commands.values(), key=lambda item: item.command)
        )

    def _poll_bot(self, bot_key: str) -> None:
        telegram = self._telegram(bot_key)
        offset = self.sessions.get_telegram_offset(bot_key)
        while not self.stop_event.is_set():
            try:
                updates = telegram.get_updates(offset, self.config.poll_timeout)
                batch = self._coalesce_updates(
                    self._await_text_fragments(telegram, offset, updates)
                )
                for update in self._coalesce_text_fragments(batch):
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                        self.sessions.set_telegram_offset(offset, bot_key)
                    self._dispatch_update(bot_key, update)
            except TelegramError as exc:
                LOGGER.error("Telegram bot %s: %s", bot_key, exc)
                delay = max(1.0, exc.retry_after or 3.0)
                self.stop_event.wait(delay)
            except Exception:
                LOGGER.exception("unexpected error in Telegram bot %s update loop", bot_key)
                self.stop_event.wait(3)

    def run(self) -> None:
        self._acquire_singleton_lock()
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        try:
            if "codex" in self.config.enabled_clis:
                self.codex.start()
            self._prepare_sessions()
            self._recover_interrupted_turns()
            for bot_key, telegram in self._telegrams.items():
                try:
                    telegram.set_commands((
                        *((name, tr(description)) for name, description in BOT_COMMANDS),
                        *self._command_menu_entries(),
                    ))
                except TelegramError as exc:
                    # Command metadata is optional; Telegram flood control must not
                    # prevent the gateway from starting and receiving updates.
                    LOGGER.warning(
                        "could not refresh Telegram commands for bot %s: %s",
                        bot_key,
                        exc,
                    )
            status_thread = threading.Thread(target=self._status_loop, name="status-pump", daemon=True)
            status_thread.start()
            poll_threads = [
                threading.Thread(
                    target=self._poll_bot,
                    args=(bot_key,),
                    name=f"telegram-poll-{bot_key}",
                    daemon=True,
                )
                for bot_key in self._telegrams
            ]
            for thread in poll_threads:
                thread.start()
            LOGGER.info(
                "gateway started; bots=%d allowed users=%d",
                len(self._telegrams),
                len(self.config.allowed_user_ids),
            )
            self.stop_event.wait()
            for thread in poll_threads:
                thread.join(timeout=3)
            status_thread.join(timeout=3)
        finally:
            self.headless.close()
            if "codex" in self.config.enabled_clis:
                self.codex.close()
            try:
                self.sessions.flush()
            except OSError:
                LOGGER.exception("could not write gateway state on shutdown")
            for telegram in self._telegrams.values():
                telegram.flush_metrics()
            if self._lock_handle is not None:
                self._lock_handle.close()
                self._lock_handle = None
        LOGGER.info("gateway stopped; CLI session IDs remain resumable")
