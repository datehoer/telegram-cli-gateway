from __future__ import annotations

import fcntl
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
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .attachments import Attachment
from .codex_backend import CodexAppServer, CodexBackendError
from .config import Config, ConfigError
from .headless_backend import HeadlessBackend, HeadlessBackendError
from .models import list_efforts, list_models
from .sessions import CliSession, SessionError, SessionManager
from .telegram import TelegramClient, TelegramError


LOGGER = logging.getLogger("telegram-cli-gateway")
MAX_PENDING_INPUTS_PER_SESSION = 20
MAX_PUBLISH_RETRY_SECONDS = 30.0
MAX_AUTO_SENT_ARTIFACTS = 3
PHOTO_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # Telegram sendPhoto 上限

# 会话状态（展示用，事件驱动；busy 判定仍是运行逻辑的权威）
STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_BLOCKED = "blocked"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"

STATE_LABELS = {
    STATE_WORKING: "🟡 运行中",
    STATE_BLOCKED: "🔴 等待",
    STATE_FAILED: "⚫ 上次失败",
    STATE_INTERRUPTED: "🔴 已中断待恢复",
    STATE_IDLE: "🟢 空闲",
}

# 恢复时作为新的用户输入发给 CLI（headless 后端按 turn_count 自动进入 resume 模式）
RESUME_PROMPT = (
    "【任务恢复】你上次的任务因网关重启或进程被中断而未完成。"
    "请先查看之前的上下文，继续完成未完成的工作；"
    "如果任务实际上已经完成，请直接说明结果，不要重复执行已完成或可能产生副作用的步骤。"
)

HELP_TEXT = """远程 CLI 网关

/new [cli] [目录]  新建会话；无参数时点选 CLI
/use <会话ID|cli>  切换；该 CLI 无会话时自动新建
/back               回到上一个会话
/sessions           查看会话列表
/tasks              查看当前任务与等待队列
/tgstats            查看 Telegram 写入与限流统计
/where              查看当前会话和目录
/interrupt          中断当前任务
/resume [会话]      继续上次被中断的任务
/cancel [会话]      放弃上次被中断的任务
/compact            压缩当前会话上下文（Codex、Pi）
/clear              当前会话开新对话，清空上下文
/reload [current|all] 重载当前会话或全部 CLI
/stop [会话ID|cli]  停止会话
/rename [会话] 名称  重命名会话
/archive [会话]     归档会话
/delete [会话]      删除会话（需确认）
/model [名称]       查看或切换当前会话模型
/effort [级别]      查看或切换推理力度
/send <文本>        显式发送给当前 CLI
/help               查看帮助

支持的 CLI：claude、codex、grok、pi。
普通文本、文件和图片会直接交给当前会话。所有 CLI 均已启用免审批模式。"""

BOT_COMMANDS = (
    ("new", "新建 CLI 会话"),
    ("use", "切换 CLI 或会话"),
    ("back", "返回上一个会话"),
    ("sessions", "列出会话"),
    ("tasks", "查看任务队列"),
    ("tgstats", "查看 Telegram API 统计"),
    ("where", "显示当前会话"),
    ("interrupt", "中断当前任务"),
    ("resume", "继续上次被中断的任务"),
    ("cancel", "放弃上次被中断的任务"),
    ("compact", "压缩当前会话上下文"),
    ("clear", "当前会话开新对话"),
    ("reload", "重载 CLI 后端"),
    ("stop", "停止会话"),
    ("rename", "重命名会话"),
    ("archive", "归档会话"),
    ("delete", "删除会话"),
    ("model", "切换当前会话模型"),
    ("effort", "切换推理力度"),
    ("help", "显示帮助"),
)


@dataclass
class TurnView:
    session: CliSession
    turn_id: str
    bot_key: str = "default"
    started_at: float = field(default_factory=time.monotonic)
    parts: list[str] = field(default_factory=list)
    command: str = ""
    command_output: str = ""
    status: str = "running"
    error: str = ""
    message_id: int | None = None
    last_edit: float = 0.0
    dirty: bool = True
    published: bool = False
    rich_mode: bool = True
    artifacts: list[Path] = field(default_factory=list)
    auto_sent: list[str] = field(default_factory=list)
    steered: int = 0
    publish_failures: int = 0
    next_publish_at: float = 0.0
    resumable: bool = False
    live: bool = False
    stale_message_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class PendingInput:
    chat_id: int
    text: str
    attachments: tuple[Attachment, ...] = ()
    bot_key: str = "default"


class GatewayApp:
    def __init__(self, config: Config):
        self.config = config
        self._telegrams: dict[str, TelegramClient] = {}
        for bot_key, token in config.telegram_bots:
            metrics_name = (
                "telegram-metrics.json"
                if bot_key == "default"
                else f"telegram-metrics-{bot_key}.json"
            )
            self._telegrams[bot_key] = TelegramClient(
                token, config.runtime_dir / metrics_name
            )
        self._default_telegram = next(iter(self._telegrams.values()))
        self.sessions = SessionManager(config)
        self.codex = CodexAppServer(
            config.cli_commands["codex"],
            self._on_codex_notification,
            self._on_codex_server_request,
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
        self._pending_codex_compactions: dict[str, str] = {}
        self._codex_compaction_turns: dict[tuple[str, str], str] = {}
        self._turns: dict[tuple[str, str], TurnView] = {}
        self._queues: dict[str, deque[PendingInput]] = {}
        self._session_states: dict[str, str] = {}
        self._interrupted_sessions: set[str] = set()
        self._last_completed_results: dict[str, str] = {}

    def _active_bot_key(self) -> str:
        bot_key = getattr(self._bot_local, "bot_key", "default")
        return bot_key if bot_key in self._telegrams else "default"

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
        return f"{configured}（网关默认）" if configured else "CLI 默认"

    def _effort_display(self, session: CliSession) -> str:
        if session.effort:
            return session.effort
        configured = self.config.default_effort_for(session.cli)
        return f"{configured}（网关默认）" if configured else "CLI 默认"

    def _switch_session(
        self, chat_id: int, session_id: str, bot_key: str | None = None
    ) -> CliSession:
        return self.sessions.switch(chat_id, session_id, bot_key or self._active_bot_key())

    def _back_session(self, chat_id: int) -> CliSession | None:
        return self.sessions.back(chat_id, self._active_bot_key())

    def _acquire_singleton_lock(self) -> None:
        lock_path = self.config.runtime_dir / "gateway.lock"
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_handle = lock_path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
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
            f"Telegram 写入统计（UTC {snapshot['utc_day']}）",
            (
                "今日成功写入："
                f"新消息 {today['new_messages']} · "
                f"编辑 {today['message_edits']} · "
                f"其他 {today['other_writes']}"
            ),
            (
                f"今日请求：成功 {today['successful_requests']} · "
                f"失败 {today['failed_requests']} · "
                f"429 {today['rate_limited']} · "
                f"本地延后 {today['local_deferrals']}"
            ),
            (
                "近 7 天成功写入："
                f"新消息 {recent['new_messages']} · "
                f"编辑 {recent['message_edits']} · "
                f"其他 {recent['other_writes']} · "
                f"429 {recent['rate_limited']}"
            ),
            f"当前 flood wait：{snapshot['flood_wait_seconds']} 秒",
        ]
        if ranked_methods:
            lines.append(
                "今日方法：" + " · ".join(
                    f"{method} {count}" for method, count in ranked_methods
                )
            )
        lines.append("仅统计 gateway 的实际写调用，不代表 Telegram 官方每日额度。")
        return "\n".join(lines)

    def _current_or_reply(self, chat_id: int) -> CliSession | None:
        session = self._current_session(chat_id)
        if not session:
            self._send(chat_id, "当前没有活动会话。使用 /new 选择并创建一个。")
            return None
        if session.cli not in self.config.enabled_clis:
            self._send(chat_id, f"此 Bot 未启用 {session.cli}，请新建已启用的 CLI 会话。")
            return None
        if not self.sessions.is_alive(session):
            self._send(chat_id, f"会话 {session.session_id} 已经退出，请创建或切换到其他会话。")
            return None
        return session

    def _new_session(self, chat_id: int, cli: str, requested_cwd: str | None) -> None:
        cli = cli.lower()
        if cli not in self.config.enabled_clis:
            self._send(chat_id, f"此 Bot 未启用 {cli}。可选：{', '.join(self.config.enabled_clis)}")
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
            self._send(chat_id, f"创建失败：{exc}")
            return
        self._send(
            chat_id,
            f"已创建并切换到 {session.session_id}\nCLI：{session.cli}\n目录：{session.cwd}"
            f"\n模型：{self._model_display(session)}\n推理力度：{self._effort_display(session)}"
            "\n权限：免审批",
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
                f"选择要创建的 CLI：\n默认目录：{self.config.default_workdir}",
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
                    "text": f"当前会话（{current.cli}）",
                    "callback_data": f"reload:{current.session_id}",
                }]
            )
        buttons.append([{"text": "全部 CLI", "callback_data": "reload:all"}])
        detail = (
            f"当前：{current.label} · {current.cli}\n" if current else "当前没有活动会话。\n"
        )
        try:
            self.telegram.send_message(
                chat_id,
                "选择重载范围：\n"
                + detail
                + "运行中的任务不会被强制中断。Codex 是共享后端，重载一个 Codex 会话会恢复全部 Codex 会话。"
                "此操作不会重读 .env 或 Gateway 源码。",
                reply_markup={"inline_keyboard": buttons},
            )
        except TelegramError:
            LOGGER.exception("could not send reload picker to chat %s", chat_id)

    def _parse_args(self, chat_id: int, raw_args: str) -> list[str] | None:
        try:
            return shlex.split(raw_args)
        except ValueError as exc:
            self._send(chat_id, f"参数格式错误：{exc}")
            return None

    def _handle_command(self, chat_id: int, command: str, raw_args: str) -> None:
        if command in ("start", "help"):
            supported = "、".join(self.config.enabled_clis)
            self._send(
                chat_id,
                HELP_TEXT.replace(
                    "支持的 CLI：claude、codex、grok、pi。",
                    f"此 Bot 启用的 CLI：{supported}。",
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
                self._send(chat_id, f"用法：/new <{'|'.join(self.config.enabled_clis)}> [目录]")
                return
            self._new_session(chat_id, args[0], args[1] if len(args) == 2 else None)
            return
        if command == "use":
            target = raw_args.strip().lower()
            if not target:
                self._send(chat_id, f"用法：/use <会话ID|{'|'.join(self.config.enabled_clis)}>")
                return
            session = self.sessions.resolve(chat_id, target)
            if session and session.cli in self.config.enabled_clis:
                self._switch_session(chat_id, session.session_id)
                self._send(chat_id, f"已切换到 {session.session_id}\n目录：{session.cwd}")
                self._surface_switched_session(session)
            elif target in self.config.enabled_clis:
                current = self._current_session(chat_id)
                self._new_session(chat_id, target, current.cwd if current else None)
            else:
                self._send(chat_id, f"找不到会话：{target}")
            return
        if command == "back":
            session = self._back_session(chat_id)
            self._send(
                chat_id,
                f"已返回 {session.session_id}\n目录：{session.cwd}"
                if session
                else "没有可返回的活动会话。",
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
        if command == "where":
            session = self._current_or_reply(chat_id)
            if session:
                self._send(
                    chat_id,
                    f"当前：{session.session_id}\nCLI：{session.cli}\n目录：{session.cwd}"
                    f"\n模型：{self._model_display(session)}"
                    f"\n推理力度：{self._effort_display(session)}\n权限：免审批",
                )
            return
        if command == "interrupt":
            session = self._current_or_reply(chat_id)
            if not session:
                return
            try:
                interrupted = self._interrupt_session(session)
            except (CodexBackendError, SessionError) as exc:
                self._send(chat_id, f"中断失败：{exc}")
                return
            self._send(chat_id, f"已中断 {session.session_id}。" if interrupted else "当前没有正在运行的任务。")
            return
        if command == "resume":
            target = raw_args.strip()
            session = self.sessions.resolve(chat_id, target) if target else self._current_session(chat_id)
            if not session:
                self._send(chat_id, "找不到要恢复的会话。")
                return
            if not self.sessions.get_in_flight(session.session_id):
                self._send(chat_id, f"{session.label} 没有待恢复的任务。")
                return
            self._send(chat_id, f"正在恢复 {session.label} 的任务…")
            self._start_session_turn(chat_id, session, RESUME_PROMPT)
            return
        if command == "cancel":
            target = raw_args.strip()
            session = self.sessions.resolve(chat_id, target) if target else self._current_session(chat_id)
            if not session:
                self._send(chat_id, "找不到会话。")
                return
            cleared = self.sessions.clear_in_flight(session.session_id)
            self._set_session_state(session.session_id, STATE_IDLE)
            self._send(
                chat_id,
                f"已放弃 {session.label} 的待恢复任务。" if cleared else f"{session.label} 没有待恢复的任务。",
            )
            return
        if command == "compact":
            session = self._current_or_reply(chat_id)
            if not session:
                return
            try:
                self._compact_session(chat_id, session)
            except (CodexBackendError, HeadlessBackendError, SessionError) as exc:
                self._send(chat_id, f"压缩失败：{exc}")
            return
        if command == "clear":
            session = self._current_or_reply(chat_id)
            if not session:
                return
            try:
                self._clear_session(chat_id, session)
            except (CodexBackendError, SessionError) as exc:
                self._send(chat_id, f"清空失败：{exc}")
            return
        if command == "reload":
            target = raw_args.strip().lower()
            if not target:
                self._send_reload_picker(chat_id)
                return
            if target not in {"current", "all"}:
                self._send(chat_id, "用法：/reload [current|all]")
                return
            try:
                result = self._reload_cli_backends(chat_id, target)
            except (CodexBackendError, SessionError) as exc:
                result = f"重载失败：{exc}"
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
                    f"{session.label} 模型已设为 {self._model_display(refreshed)}（下次任务生效）。",
                )
            else:
                self._send_model_picker(chat_id, session)
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
                    f"{session.label} 推理力度已设为 {self._effort_display(refreshed)}（下次任务生效）。",
                )
            else:
                self._send_effort_picker(chat_id, session)
            return
        if command == "stop":
            target = raw_args.strip()
            session = self.sessions.resolve(chat_id, target) if target else self._current_session(chat_id)
            if not session:
                self._send(chat_id, "找不到要停止的会话。")
                return
            try:
                self._interrupt_session(session)
            except (CodexBackendError, SessionError):
                LOGGER.exception("could not interrupt %s before stopping", session.session_id)
            self.sessions.stop(session)
            self._session_states.pop(session.session_id, None)
            self._interrupted_sessions.discard(session.session_id)
            self._send(chat_id, f"已停止 {session.session_id}。")
            return
        if command == "rename":
            args = self._parse_args(chat_id, raw_args)
            if args is None:
                return
            current = self._current_session(chat_id)
            if not args or not current:
                self._send(chat_id, "用法：/rename [会话ID] <新名称>")
                return
            explicit = self.sessions.get(args[0]) if len(args) > 1 else None
            session = explicit if explicit and explicit.chat_id == chat_id else current
            name_parts = args[1:] if session is explicit else args
            try:
                renamed = self.sessions.rename(session.session_id, " ".join(name_parts))
            except SessionError as exc:
                self._send(chat_id, f"重命名失败：{exc}")
                return
            self._send(chat_id, f"已将 {renamed.session_id} 重命名为「{renamed.label}」。")
            return
        if command == "archive":
            target = raw_args.strip()
            session = self.sessions.resolve(chat_id, target) if target else self._current_session(chat_id)
            if not session:
                self._send(chat_id, "找不到要归档的会话。")
                return
            self._archive_session(chat_id, session)
            return
        if command == "delete":
            target = raw_args.strip()
            session = self._session_for_management(chat_id, target)
            if not session:
                self._send(chat_id, "找不到要删除的会话。")
                return
            self._send_delete_confirmation(chat_id, session)
            return
        if command == "send":
            if not raw_args:
                self._send(chat_id, "用法：/send <要发送给当前 CLI 的文本>")
                return
            self._send_to_current(chat_id, raw_args)
            return
        self._send(chat_id, "未知命令。使用 /help 查看可用命令。")

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
        lines = ["任务状态："]
        buttons: list[list[dict[str, str]]] = []
        found = False
        for session in self.sessions.list_for_chat(chat_id):
            busy = self._session_is_busy(session)
            with self._state_lock:
                queued = len(self._queues.get(session.session_id, ()))
            if busy or queued:
                found = True
                lines.append(
                    f"• {session.label} ({session.session_id})：{self._session_status_text(session)}"
                )
                if busy:
                    row = [{
                        "text": f"中断 {session.label}",
                        "callback_data": f"taskinterrupt:{session.session_id}",
                    }]
                    if queued:
                        row.append({
                            "text": "中断并清空",
                            "callback_data": f"stopqueue:{session.session_id}",
                        })
                    buttons.append(row)
                if queued:
                    buttons.append([{
                        "text": f"清空 {session.label} 的 {queued} 条等待",
                        "callback_data": f"clearqueue:{session.session_id}",
                    }])
        text = "\n".join(lines) if found else "当前没有运行中或排队的任务。"
        return text, {"inline_keyboard": buttons}

    def _clear_queue(self, session_id: str) -> int:
        with self._state_lock:
            pending = self._queues.pop(session_id, None)
            return len(pending) if pending else 0

    def _archive_session(self, chat_id: int, session: CliSession) -> None:
        try:
            self._interrupt_session(session)
        except (CodexBackendError, SessionError):
            LOGGER.exception("could not interrupt %s before archiving", session.session_id)
        with self._state_lock:
            self._queues.pop(session.session_id, None)
        self._session_states.pop(session.session_id, None)
        self._interrupted_sessions.discard(session.session_id)
        self.sessions.set_archived(session.session_id, True)
        self._send(chat_id, f"已归档 {session.label}。用 /sessions all 查看或恢复。")

    def _send_delete_confirmation(self, chat_id: int, session: CliSession) -> None:
        markup = {
            "inline_keyboard": [[
                {"text": "确认删除", "callback_data": f"delete!:{session.session_id}"},
                {"text": "取消", "callback_data": f"cancel:{session.session_id}"},
            ]]
        }
        try:
            self.telegram.send_message(
                chat_id,
                f"确认永久删除会话 {session.label}（{session.session_id}）？",
                reply_markup=markup,
            )
        except TelegramError:
            LOGGER.exception("could not send delete confirmation")

    def _session_is_busy(self, session: CliSession) -> bool:
        if session.backend == "codex-app-server" and session.external_id:
            with self._state_lock:
                return bool(self._codex_active_turns.get(session.external_id))
        return self.headless.is_active(session.session_id)

    def _set_session_state(self, session_id: str, state: str) -> None:
        with self._state_lock:
            self._session_states[session_id] = state

    def _session_status_text(self, session: CliSession) -> str:
        """会话状态的展示文本：🟡 运行中 / 🟢 空闲 / ⚫ 上次失败 / 🔴 等待，附排队数。"""
        with self._state_lock:
            state = self._session_states.get(session.session_id)
            queued = len(self._queues.get(session.session_id, ()))
        if state is None:
            # 旧会话/未初始化：回退到实时 busy 判定
            state = STATE_WORKING if self._session_is_busy(session) else STATE_IDLE
        text = STATE_LABELS.get(state, STATE_LABELS[STATE_IDLE])
        if queued:
            text += f" · 排队 {queued}"
        return text

    def _surface_switched_session(self, session: CliSession) -> None:
        """切回 session 时把它当前最有用的状态放到聊天底部。"""
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
                # 发起入口的运行态由 status pump 重新钉住。
                return
            if source_view and source_view.bot_key != bot_key:
                rendered, _answer = self._render_turn(source_view, time.monotonic())
            else:
                rendered = None
            for view in pending_views:
                if view.bot_key != bot_key:
                    continue
                # CLI 已结束但终态尚未发布：强制下一轮在底部发新卡片。
                if view.message_id is not None and view.message_id > 0:
                    view.stale_message_ids.append(view.message_id)
                view.message_id = None
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
        if source_message_id is not None:
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

        cached_result = self._last_completed_results.get(session.session_id)
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

        self._send(session.chat_id, f"[{session.session_id}] · 有什么可以帮你？")

    def _sessions_view(
        self, chat_id: int, include_archived: bool = False
    ) -> tuple[str, dict[str, Any]]:
        current = self._current_session(chat_id)
        sessions = self.sessions.list_for_chat(chat_id, include_archived=include_archived)
        lines = ["会话列表（点击按钮切换）："]
        buttons: list[list[dict[str, str]]] = []
        for session in sessions:
            selected = bool(current and current.session_id == session.session_id)
            marker = "▶" if selected else " "
            if session.archived:
                status = "已归档"
            else:
                status = self._session_status_text(session)
                if not self.sessions.is_alive(session):
                    status += " · 已退出"
            identity = session.label
            if session.display_name:
                identity += f" ({session.session_id})"
            lines.append(f"{marker} {identity} · {status}\n    {session.cwd}")
            if session.archived:
                buttons.append([
                    {"text": f"恢复 {session.label}", "callback_data": f"restore:{session.session_id}"},
                    {"text": "删除", "callback_data": f"delete?:{session.session_id}"},
                ])
            elif self.sessions.is_alive(session):
                label = f"✓ {session.label}" if selected else session.label
                row = [{"text": label, "callback_data": f"use:{session.session_id}"}]
                if self._session_is_busy(session):
                    row.append({"text": "中断", "callback_data": f"interrupt:{session.session_id}"})
                buttons.append(row)
                buttons.append([
                    {"text": "归档", "callback_data": f"archive:{session.session_id}"},
                    {"text": "删除", "callback_data": f"delete?:{session.session_id}"},
                ])
        return "\n".join(lines), {"inline_keyboard": buttons}

    def _send_sessions(self, chat_id: int, include_archived: bool = False) -> None:
        sessions = self.sessions.list_for_chat(chat_id, include_archived=include_archived)
        if not sessions:
            self._send(chat_id, "目前没有会话。")
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
            f"{session.label} · 当前模型：{self._model_display(session)}",
            "点击切换（下次任务生效）：",
        ]
        buttons: list[list[dict[str, str]]] = []
        for index, model in enumerate(models):
            marker = "✓ " if model == current else ""
            buttons.append([{
                "text": f"{marker}{model}"[:64],
                "callback_data": f"model:{session.session_id}:{index}",
            }])
        reset_label = "恢复网关默认" if self.config.default_model_for(session.cli) else "恢复默认"
        buttons.append([{"text": reset_label, "callback_data": f"modelclear:{session.session_id}"}])
        if not models:
            lines.append("该 CLI 无法列出模型，请直接 /model <名称>。")
        return "\n".join(lines), {"inline_keyboard": buttons}

    def _effort_view(self, session: CliSession) -> tuple[str, dict[str, Any]]:
        efforts = list_efforts(session.cli)
        current = self._effective_effort(session)
        lines = [
            f"{session.label} · 当前推理力度：{self._effort_display(session)}",
            "点击切换（下次任务生效）：",
        ]
        buttons: list[list[dict[str, str]]] = []
        for index, effort in enumerate(efforts):
            marker = "✓ " if effort == current else ""
            buttons.append([{
                "text": f"{marker}{effort}",
                "callback_data": f"effort:{session.session_id}:{index}",
            }])
        reset_label = "恢复网关默认" if self.config.default_effort_for(session.cli) else "恢复默认"
        buttons.append([{"text": reset_label, "callback_data": f"effortclear:{session.session_id}"}])
        if not efforts:
            lines.append("该 CLI 不支持手动设置推理力度。")
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
                self.telegram.answer_callback_query(query_id, "没有权限")
            except TelegramError:
                pass
            return
        if not isinstance(data, str) or ":" not in data:
            try:
                self.telegram.answer_callback_query(query_id, "未知操作")
            except TelegramError:
                pass
            return
        action, target = data.split(":", 1)
        if action == "new":
            if target not in self.config.enabled_clis:
                try:
                    self.telegram.answer_callback_query(query_id, "CLI 已失效，请重新 /new")
                except TelegramError:
                    pass
                return
            try:
                self.telegram.answer_callback_query(query_id, f"正在创建 {target}")
            except TelegramError:
                LOGGER.exception("could not answer new-session callback for chat %s", chat_id)
            if isinstance(message_id, int):
                try:
                    self.telegram.edit_message(chat_id, message_id, f"已选择 {target}，正在创建…")
                except TelegramError:
                    LOGGER.exception("could not close new-session picker for chat %s", chat_id)
            self._new_session(chat_id, target, None)
            return

        if action == "reload":
            try:
                self.telegram.answer_callback_query(query_id, "正在重载")
            except TelegramError:
                LOGGER.exception("could not answer reload callback for chat %s", chat_id)
            try:
                result = self._reload_cli_backends(chat_id, target)
            except (CodexBackendError, SessionError) as exc:
                result = f"重载失败：{exc}"
            try:
                if isinstance(message_id, int):
                    self.telegram.edit_message(chat_id, message_id, result)
                else:
                    self._send(chat_id, result)
            except TelegramError:
                LOGGER.exception("could not finish reload callback for chat %s", chat_id)
            return

        if action in {"file", "photo"}:
            resolved = self.sessions.resolve_artifact(
                chat_id, target, self._active_bot_key()
            )
            if not resolved:
                self.telegram.answer_callback_query(query_id, "文件已失效")
                return
            _session, path = resolved
            try:
                path = self._validated_local_file(path)
                self.telegram.answer_callback_query(query_id, "正在发送")
                try:
                    self.telegram.send_local_file(chat_id, path, as_photo=action == "photo")
                except TelegramError:
                    if action != "photo":
                        raise
                    self.telegram.send_local_file(chat_id, path, as_photo=False)
            except (OSError, ValueError, TelegramError) as exc:
                LOGGER.warning("artifact send failed: %s", exc)
                self._send(chat_id, f"发送文件失败：{exc}")
            return

        if action in {"model", "modelclear", "effort", "effortclear"}:
            if action in {"model", "effort"}:
                session_id, separator, index_text = target.partition(":")
                index = int(index_text) if separator and index_text.isdigit() else -1
            else:
                session_id, index = target, -1
            session = self.sessions.get(session_id)
            if not session or session.chat_id != chat_id:
                self.telegram.answer_callback_query(query_id, "会话已失效")
                return
            if action == "modelclear":
                self.sessions.set_model(session_id, None)
                default_model = self.config.default_model_for(session.cli)
                notice = f"已恢复网关默认模型 {default_model}" if default_model else "已恢复默认模型"
            elif action == "effortclear":
                self.sessions.set_effort(session_id, None)
                default_effort = self.config.default_effort_for(session.cli)
                notice = (
                    f"已恢复网关默认推理力度 {default_effort}"
                    if default_effort
                    else "已恢复默认推理力度"
                )
            elif action == "model":
                models = list_models(session.cli, self.config.cli_commands[session.cli])
                if 0 <= index < len(models):
                    value = models[index]
                    self.sessions.set_model(session_id, value)
                    notice = f"模型已设为 {value}"
                else:
                    notice = "选择已失效，请重新 /model"
            else:
                efforts = list_efforts(session.cli)
                if 0 <= index < len(efforts):
                    value = efforts[index]
                    self.sessions.set_effort(session_id, value)
                    notice = f"推理力度已设为 {value}"
                else:
                    notice = "选择已失效，请重新 /effort"
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
            self.telegram.answer_callback_query(query_id, "会话已失效")
            return
        try:
            if action in {"taskinterrupt", "clearqueue", "stopqueue"}:
                if action == "taskinterrupt":
                    interrupted = self._interrupt_session(session)
                    notice = "已中断当前任务" if interrupted else "当前没有运行任务"
                elif action == "clearqueue":
                    cleared = self._clear_queue(session.session_id)
                    notice = f"已清空 {cleared} 条等待消息" if cleared else "队列已经为空"
                else:
                    # Clear first so the terminal callback cannot start the next queued turn.
                    cleared = self._clear_queue(session.session_id)
                    interrupted = self._interrupt_session(session)
                    if interrupted:
                        notice = f"已中断并清空 {cleared} 条等待消息"
                    else:
                        notice = f"已清空 {cleared} 条等待消息" if cleared else "当前没有运行任务"
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
                notice = f"已切换到 {session.label}"
            elif action == "interrupt":
                interrupted = self._interrupt_session(session)
                notice = "已中断" if interrupted else "当前没有运行任务"
            elif action == "archive":
                try:
                    self._interrupt_session(session)
                except (CodexBackendError, SessionError):
                    LOGGER.exception("could not interrupt before archive")
                with self._state_lock:
                    self._queues.pop(session.session_id, None)
                self.sessions.set_archived(session.session_id, True)
                notice = "已归档"
            elif action == "restore":
                self.sessions.set_archived(session.session_id, False)
                notice = "已恢复"
                include_archived = True
            elif action == "delete?":
                self.telegram.answer_callback_query(query_id, "请再次确认")
                self._send_delete_confirmation(chat_id, session)
                return
            elif action == "delete!":
                try:
                    self._interrupt_session(session)
                except (CodexBackendError, SessionError):
                    pass
                with self._state_lock:
                    self._queues.pop(session.session_id, None)
                self._session_states.pop(session.session_id, None)
                self._interrupted_sessions.discard(session.session_id)
                self._last_completed_results.pop(session.session_id, None)
                self.sessions.stop(session)
                notice = "已删除"
                refresh = False
                if isinstance(message_id, int):
                    self.telegram.edit_message(
                        chat_id, message_id, f"已删除 {session.label}（{session.session_id}）。"
                    )
            elif action == "cancel":
                notice = "已取消"
                refresh = False
                if isinstance(message_id, int):
                    self.telegram.edit_message(chat_id, message_id, "已取消删除。")
            else:
                self.telegram.answer_callback_query(query_id, "未知操作")
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

    def _interrupt_session(self, session: CliSession) -> bool:
        if session.backend == "codex-app-server" and session.external_id:
            with self._state_lock:
                turn_id = self._codex_active_turns.get(session.external_id)
            if not turn_id or turn_id == "starting":
                return False
            self.codex.interrupt(session.external_id, turn_id)
            self._interrupted_sessions.add(session.session_id)
            self._set_session_state(session.session_id, STATE_IDLE)
            return True
        if session.backend == "headless-json":
            interrupted = self.headless.interrupt(session.session_id)
            if interrupted:
                self._interrupted_sessions.add(session.session_id)
                # 用户主动中断：清掉恢复候选，重启后不再自动提示恢复。
                self.sessions.clear_in_flight(session.session_id)
                self._set_session_state(session.session_id, STATE_IDLE)
            return interrupted
        self.sessions.interrupt(session)
        return True

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
                raise SessionError("会话已失效，请重新 /reload")
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
                    busy.append("Codex 上下文压缩")
            if busy:
                shown = "、".join(busy[:5])
                suffix = " 等" if len(busy) > 5 else ""
                raise SessionError(f"仍有任务运行：{shown}{suffix}；请等待完成后重试")

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
                lines.append(f"Codex 后端已重启，已恢复 {len(resumed)} 个会话。")
                if failed:
                    lines.append(
                        "恢复失败："
                        + "、".join(failed[:5])
                        + (" 等" if len(failed) > 5 else "")
                    )

            headless_clis = [
                cli
                for cli in self.config.enabled_clis
                if cli in target_clis and cli != "codex"
            ]
            if headless_clis:
                lines.append(
                    "、".join(headless_clis)
                    + " 按任务启动新进程；下次任务会直接使用当前安装版本。"
                )
            return "\n".join(lines) or "没有可重载的 CLI。"

    def _compact_session(self, chat_id: int, session: CliSession) -> None:
        """压缩当前会话上下文：Codex 用原生 thread/compact/start，Pi 用一次性 RPC。

        会话忙碌时拒绝（压缩需要空闲上下文）；Grok/Claude 无可用手动压缩，
        回复状态说明而不是假装成功。
        """
        if self._session_is_busy(session):
            self._send(chat_id, f"{session.label} 正在运行，请先 /interrupt 再压缩。")
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
            self._send(chat_id, f"已触发 {session.label} 的上下文压缩。")
            return
        if session.cli == "pi":
            result = self.headless.compact_session(session)
            self._send(chat_id, f"{session.label}：{result}")
            return
        if session.cli == "claude":
            self._send(
                chat_id,
                f"{session.label} 使用 Claude 的 --autocompact 自动压缩；"
                "手动 /compact 仅交互模式可用，headless 下没有。",
            )
            return
        self._send(
            chat_id,
            f"{session.label}（Grok）无手动压缩；上下文接近 85% 时 Grok 会自动压缩。"
            "需要重置可 /clear 开新对话。",
        )

    def _clear_session(self, chat_id: int, session: CliSession) -> None:
        """当前会话开新对话：网关记录保留，但 CLI 上下文清零重来。

        Codex 建新 thread 并归档旧 thread；headless 换新 external_id 并重置
        turn_count（下次自动走首轮模式）。会话忙碌时拒绝。
        """
        if self._session_is_busy(session):
            self._send(chat_id, f"{session.label} 正在运行，请先 /interrupt 再 /clear。")
            return
        self.sessions.clear_in_flight(session.session_id)
        self._set_session_state(session.session_id, STATE_IDLE)
        if session.backend == "codex-app-server" and session.external_id:
            old_thread = session.external_id
            try:
                thread_id = self.codex.start_thread(
                    session.cwd, model=self._effective_model(session)
                )
            except CodexBackendError:
                # 新建失败则保留原线程，不破坏会话
                self._send(chat_id, "新建 Codex 线程失败，未清空。")
                return
            try:
                self.codex.archive_thread(old_thread)
            except CodexBackendError:
                LOGGER.warning("could not archive old Codex thread %s", old_thread)
            self.sessions.update_backend(session.session_id, "codex-app-server", thread_id)
            self.sessions.clear_last_completed_messages(session.session_id)
            self._last_completed_results.pop(session.session_id, None)
            self._send(chat_id, f"{session.label} 已开新对话（旧线程已归档）。")
            return
        if session.backend == "headless-json":
            self.sessions.rotate_external_id(session.session_id)
            self.sessions.clear_last_completed_messages(session.session_id)
            self._last_completed_results.pop(session.session_id, None)
            self._send(chat_id, f"{session.label} 已开新对话，上下文已清空。")
            return
        self._send(chat_id, f"{session.label} 不支持 /clear。")

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
            self._send(chat_id, f"此 Bot 未启用 {session.cli}，不能继续该会话。")
            return
        if self._session_is_busy(session):
            if session.backend == "codex-app-server" and session.external_id:
                with self._state_lock:
                    active_turn = self._codex_active_turns.get(session.external_id)
                    active_view = (
                        self._turns.get((session.session_id, active_turn))
                        if active_turn and active_turn != "starting"
                        else None
                    )
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
                        with self._state_lock:
                            view = self._turns.get((session.session_id, active_turn))
                            if view:
                                view.steered += 1
                                view.dirty = True
                        self._send(chat_id, f"已追加到 {session.label} 当前任务。")
                        with self._state_lock:
                            # The acknowledgement is newer than the existing progress
                            # card. Rotate the message pointer immediately so the next
                            # stream update cannot land on the original card.
                            if view and view.status == "running":
                                view.live = False
                        if view:
                            now = time.monotonic()
                            try:
                                pinned = self._pin_turn(view, now)
                            except TelegramError as exc:
                                with self._state_lock:
                                    delay = self._schedule_publish_retry(view, exc, now)
                                LOGGER.warning(
                                    "could not re-pin Telegram message for %s after steering; "
                                    "retrying in %.1fs: %s",
                                    view.session.session_id,
                                    delay,
                                    exc,
                                )
                            else:
                                if pinned:
                                    with self._state_lock:
                                        view.last_edit = now
                                        view.publish_failures = 0
                                        view.next_publish_at = 0.0
                        return
                    except CodexBackendError as exc:
                        LOGGER.warning("Codex steer failed; queued instead: %s", exc)
            position = self._enqueue(
                session,
                PendingInput(
                    chat_id, text, attachments, bot_key=bot_key
                ),
            )
            if position is None:
                self._send(
                    chat_id,
                    f"{session.label} 的等待队列已满（最多 {MAX_PENDING_INPUTS_PER_SESSION} 条）。",
                )
            else:
                self._send(chat_id, f"{session.label} 正在运行；已加入队列（第 {position} 条）。")
            return
        self._start_session_turn(chat_id, session, text, attachments)

    def _enqueue(self, session: CliSession, pending: PendingInput) -> int | None:
        with self._state_lock:
            queue_for_session = self._queues.setdefault(session.session_id, deque())
            if len(queue_for_session) >= MAX_PENDING_INPUTS_PER_SESSION:
                return None
            queue_for_session.append(pending)
            return len(queue_for_session)

    def _start_next_queued(self, session: CliSession) -> None:
        with self._state_lock:
            queue_for_session = self._queues.get(session.session_id)
            pending = queue_for_session.popleft() if queue_for_session else None
            if queue_for_session is not None and not queue_for_session:
                self._queues.pop(session.session_id, None)
        if pending:
            with self._bot_scope(pending.bot_key):
                self._start_session_turn(
                    pending.chat_id, session, pending.text, pending.attachments
                )

    def _start_session_turn(
        self,
        chat_id: int,
        session: CliSession,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        bot_key: str | None = None,
    ) -> None:
        with self._backend_reload_lock:
            self._start_session_turn_locked(chat_id, session, text, attachments, bot_key)

    def _start_session_turn_locked(
        self,
        chat_id: int,
        session: CliSession,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        bot_key: str | None = None,
    ) -> None:
        bot_key = bot_key or self._active_bot_key()
        if session.cli not in self.config.enabled_clis:
            self._send(
                chat_id,
                f"此 Bot 未启用 {session.cli}，不能启动任务。",
                bot_key,
            )
            return
        try:
            self._telegram(bot_key).send_action(chat_id)
        except TelegramError:
            pass
        try:
            if session.backend == "codex-app-server" and session.external_id:
                with self._state_lock:
                    self._codex_active_turns[session.external_id] = "starting"
                try:
                    turn_id = self.codex.start_turn(
                        session.external_id,
                        text,
                        attachments,
                        model=self._effective_model(session),
                        effort=self._effective_effort(session),
                    )
                except Exception:
                    with self._state_lock:
                        self._codex_active_turns.pop(session.external_id, None)
                    raise
                with self._state_lock:
                    self._codex_active_turns[session.external_id] = turn_id
                self._register_turn(session, turn_id, bot_key)
            elif session.backend == "headless-json":
                effective_session = replace(
                    session,
                    model=self._effective_model(session),
                    effort=self._effective_effort(session),
                )
                turn_id = self.headless.start_turn(effective_session, text, attachments)
                self.sessions.increment_turn_count(session.session_id)
                self._register_turn(session, turn_id, bot_key)
            else:
                raise SessionError("旧 TUI 会话尚未迁移，请重启网关后重试")
            self.sessions.set_in_flight(
                session.session_id, session.chat_id, turn_id, bot_key
            )
            self._interrupted_sessions.discard(session.session_id)
            self._set_session_state(session.session_id, STATE_WORKING)
        except (CodexBackendError, HeadlessBackendError, SessionError) as exc:
            self._send(chat_id, f"发送失败：{exc}", bot_key)

    def _register_turn(
        self, session: CliSession, turn_id: str, bot_key: str | None = None
    ) -> TurnView:
        key = (session.session_id, turn_id)
        with self._state_lock:
            view = self._turns.setdefault(
                key,
                TurnView(
                    session=session,
                    turn_id=turn_id,
                    bot_key=bot_key or self._active_bot_key(),
                ),
            )
            if bot_key is not None:
                view.bot_key = bot_key
            return view

    def _update_turn(self, session: CliSession, turn_id: str, kind: str, data: Any = None) -> None:
        view = self._register_turn(session, turn_id)
        with self._state_lock:
            if kind == "delta" and isinstance(data, str):
                view.parts.append(data)
            elif kind == "thinking":
                # Reasoning is not user-visible output. Do not retain or publish it.
                pass
            elif kind == "command":
                view.command = self._display_value(data)[:1000]
                view.command_output = ""
            elif kind == "command_output":
                view.command_output = (view.command_output + self._display_value(data))[-1200:]
            elif kind == "completed":
                view.status = "completed"
                self.sessions.clear_in_flight(session.session_id, turn_id)
                self._interrupted_sessions.discard(session.session_id)
                self._set_session_state(session.session_id, STATE_IDLE)
            elif kind == "error":
                view.status = "failed"
                view.error = self._display_value(data)[:2000]
                self.sessions.clear_in_flight(session.session_id, turn_id)
                if session.session_id in self._interrupted_sessions:
                    # 用户主动中断：不显示为失败，恢复空闲
                    self._interrupted_sessions.discard(session.session_id)
                    self._set_session_state(session.session_id, STATE_IDLE)
                else:
                    self._set_session_state(session.session_id, STATE_FAILED)
            elif kind == "interrupted":
                # 进程被信号杀死（网关重启/关机连累、用户中断、OOM 等）。
                # 不清 in_flight：意外中断时保留为恢复候选，供 /resume、/cancel 或重启恢复处理。
                view.status = "interrupted"
                if session.session_id in self._interrupted_sessions:
                    # 用户主动中断：_interrupt_session 已清 in_flight 并通知，这里静默收尾
                    self._interrupted_sessions.discard(session.session_id)
                    self._set_session_state(session.session_id, STATE_IDLE)
                else:
                    view.resumable = True
                    self._set_session_state(session.session_id, STATE_INTERRUPTED)
            view.dirty = True

    @staticmethod
    def _display_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            return str(value)

    def _recover_interrupted_turns(self) -> None:
        """在网关启动时处理上次遗留的进行中任务。

        用户主动中断的任务已在中断时清除 in_flight，不会出现在这里；
        能留下的都是意外中断（网关重启/关机连累、进程被杀死）。
        默认只发提示，由用户用 /resume 或 /cancel 决定；AUTO_RESUME=true
        时直接自动恢复（复用 headless 的 resume 模式续跑同一会话）。
        """
        for session_id, chat_id, turn_id, bot_key in self.sessions.stale_in_flight_routes():
            session = self.sessions.get(session_id)
            label = session.label if session else session_id
            if self.config.auto_resume and session:
                with self._bot_scope(bot_key):
                    self._send(
                        chat_id,
                        f"任务中断（网关重启）：{label} 已自动恢复，正在继续上次未完成的工作。"
                        "如已产生副作用，请 /interrupt 停止。",
                    )
                    self._start_session_turn(chat_id, session, RESUME_PROMPT)
            else:
                with self._bot_scope(bot_key):
                    self._send(
                        chat_id,
                        f"⚠️ 上次任务被中断：{label}（{session_id}）。"
                        "回复 /resume 继续，或 /cancel 放弃。",
                    )

    def _on_headless_event(self, session_id: str, turn_id: str, kind: str, data: Any) -> None:
        session = self.sessions.get(session_id)
        if session:
            self._update_turn(session, turn_id, kind, data)
            if kind in {"completed", "error"}:
                self._start_next_queued(session)

    def _on_codex_notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        session = self.sessions.find_by_external_id(thread_id)
        if not session:
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
                    self._register_turn(session, turn_id)
            return
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str):
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            return
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
                    if self._codex_active_turns.get(thread_id) == turn_id:
                        self._codex_active_turns.pop(thread_id, None)
                    self._codex_compaction_turns.pop(compaction_key, None)
                threading.Thread(
                    target=self._start_next_queued,
                    args=(session,),
                    name=f"queue-{session.session_id}",
                    daemon=True,
                ).start()
            return
        if method == "item/agentMessage/delta":
            self._update_turn(session, turn_id, "delta", params.get("delta"))
        elif method == "item/started":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "commandExecution":
                self._update_turn(session, turn_id, "command", item.get("command"))
        elif method == "item/commandExecution/outputDelta":
            self._update_turn(session, turn_id, "command_output", params.get("delta"))
        elif method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "commandExecution":
                output = item.get("aggregatedOutput") or item.get("output")
                if output:
                    self._update_turn(session, turn_id, "command_output", output)
        elif method == "turn/completed":
            turn = params.get("turn")
            status = turn.get("status") if isinstance(turn, dict) else None
            error = turn.get("error") if isinstance(turn, dict) else None
            with self._state_lock:
                self._codex_active_turns.pop(thread_id, None)
            if status == "failed" or error:
                detail = error.get("message") if isinstance(error, dict) else error
                self._update_turn(session, turn_id, "error", detail or "Codex task failed")
            else:
                self._update_turn(session, turn_id, "completed")
            threading.Thread(
                target=self._start_next_queued,
                args=(session,),
                name=f"queue-{session.session_id}",
                daemon=True,
            ).start()
        elif method == "error":
            self._update_turn(session, turn_id, "error", params.get("message") or params.get("error"))

    def _on_codex_server_request(
        self, request_id: str | int, method: str, params: dict[str, Any]
    ) -> None:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            # 免审批模式下不会触发；保留 blocked 信号供未来 on-request 模式使用。
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
            # 自动接受后 turn 继续执行
            if session:
                self._set_session_state(session.session_id, STATE_WORKING)
            return
        LOGGER.warning("declined unsupported Codex server request: %s", method)
        try:
            self.codex.respond(request_id, {"decision": "decline"})
        except CodexBackendError:
            LOGGER.exception("could not decline unsupported Codex request")

    def _render_turn(self, view: TurnView, now: float, background: bool = False) -> tuple[str, str]:
        elapsed = max(0, int(now - view.started_at))
        label = {
            "running": "运行中",
            "completed": "完成",
            "failed": "失败",
            "interrupted": "已中断",
        }.get(view.status, view.status)
        if background and view.status == "running":
            label = "后台运行中"
        header = f"[{view.session.session_id}] · {label} {elapsed}秒"
        if view.steered:
            header += f" · 已追加 {view.steered} 次"
        answer = "".join(view.parts).strip()
        sections = [header]
        if view.status == "running" and not background:
            if answer:
                sections.append(("…" if len(answer) > 27000 else "") + answer[-27000:])
            if view.command:
                command = view.command[-700:].replace("```", "` ` `")
                sections.append(f"当前命令：\n```text\n{command}\n```")
            if view.command_output:
                output = view.command_output[-500:].replace("```", "` ` `")
                sections.append(f"命令输出：\n```text\n{output}\n```")
        elif view.status == "running" and background and answer:
            sections.append(("…" if len(answer) > 27000 else "") + answer[-27000:])
        elif answer:
            sections.append(answer)
        elif view.status == "completed":
            sections.append(
                "⚠️ 模型已结束，但没有返回可见回答。"
                "为避免重复执行操作，网关未自动重试。"
            )
        if view.status == "interrupted" and view.resumable:
            sections.append("⚠️ 任务被中断。回复 /resume 继续，或 /cancel 放弃。")
        if view.error:
            error = view.error.replace("```", "` ` `")
            sections.append(f"错误：\n```text\n{error}\n```")
        return "\n\n".join(sections), answer

    def _pin_turn(self, view: TurnView, now: float) -> bool:
        """把当前 session 的运行进度钉到一条新的底部消息上并开始直播。

        旧卡片（若存在）降级为“后台运行中”，记入 stale_message_ids，待任务
        终态时一并收尾。每次切回该 session 都会重新把进度钉到最新一条消息。
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
                old_id = view.message_id if view.message_id and view.message_id > 0 else None
                if old_id is not None:
                    view.stale_message_ids.append(old_id)
                view.message_id = None
                view.dirty = True
            rendered, _ = self._render_turn(view, now)
            self._publish_running_turn(view, rendered)
            with self._state_lock:
                view.live = True
            if old_id is not None:
                stamp, _ = self._render_turn(view, now, background=True)
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
                    if exc.retry_after is not None:
                        raise
                    LOGGER.warning(
                        "could not stamp old progress card %s for %s: %s",
                        old_id,
                        view.session.session_id,
                        exc,
                    )
            return True

    def _background_turn(self, view: TurnView, now: float) -> None:
        """切换走时把当前卡片定格为“后台运行中”，停止周期刷新。"""
        rendered, _ = self._render_turn(view, now, background=True)
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
                if exc.retry_after is not None:
                    raise
                view.rich_mode = False
                self._telegram(view.bot_key).edit_markdown(
                    view.session.chat_id, view.message_id, rendered
                )
        with self._state_lock:
            view.live = False

    def _resolve_stale(self, view: TurnView) -> None:
        """任务结束后把历史里残留的旧进度卡片收尾成一句短状态。"""
        with self._state_lock:
            stale_ids = list(view.stale_message_ids)
            view.stale_message_ids.clear()
        if not stale_ids:
            return
        if view.status == "completed":
            stamp = "✅ 此任务已完成，结果已更新到最新一条进度消息。"
        elif view.status == "failed":
            stamp = "⚠️ 此任务失败，详情见最新一条进度消息。"
        else:
            stamp = "⏹ 此任务已中断。"
        for message_id in stale_ids:
            try:
                self._telegram(view.bot_key).edit_message(
                    view.session.chat_id, message_id, stamp
                )
            except TelegramError:
                LOGGER.warning("could not resolve stale progress card %s", message_id)

    def _publish_running_turn(self, view: TurnView, rendered: str) -> None:
        # Stream by editing one persistent message in place. Telegram Rich Message
        # Drafts cannot be finalized or removed by the gateway once interrupted,
        # leaving an eternal "loading" bubble in the chat, so they are not used.
        if view.rich_mode:
            try:
                if view.message_id is None:
                    message_id = self._telegram(view.bot_key).send_rich_markdown(
                        view.session.chat_id, rendered
                    )
                    view.message_id = message_id if message_id is not None else 0
                    if message_id is not None:
                        self.sessions.bind_message(
                            view.session.chat_id,
                            message_id,
                            view.session.session_id,
                            view.bot_key,
                        )
                elif view.message_id > 0:
                    self._telegram(view.bot_key).edit_rich_markdown(
                        view.session.chat_id, view.message_id, rendered
                    )
                view.published = True
                return
            except TelegramError as exc:
                if exc.retry_after is not None:
                    raise
                view.rich_mode = False
                LOGGER.warning(
                    "Rich Message streaming unavailable for %s; using HTML fallback: %s",
                    view.session.session_id,
                    exc,
                )
        if view.message_id is None:
            message_id = self._telegram(view.bot_key).send_markdown(
                view.session.chat_id, rendered
            )
            view.message_id = message_id if message_id is not None else 0
            if message_id is not None:
                self.sessions.bind_message(
                    view.session.chat_id,
                    message_id,
                    view.session.session_id,
                    view.bot_key,
                )
        elif view.message_id > 0:
            self._telegram(view.bot_key).edit_markdown(
                view.session.chat_id, view.message_id, rendered
            )
        view.published = True

    def _publish_final_turn(
        self, view: TurnView, rendered: str, with_artifacts: bool = True
    ) -> None:
        if with_artifacts:
            view.artifacts = self._find_artifacts(view.session, "".join(view.parts))
            markup = self._artifact_markup(view)
        else:
            view.artifacts = []
            markup = None
        try:
            if view.message_id is not None and view.message_id > 0:
                self._telegram(view.bot_key).edit_rich_markdown(
                    view.session.chat_id,
                    view.message_id,
                    rendered,
                    reply_markup=markup,
                )
            else:
                message_id = self._telegram(view.bot_key).send_rich_markdown(
                    view.session.chat_id, rendered, reply_markup=markup
                )
                view.message_id = message_id if message_id is not None else 0
            if view.message_id and view.message_id > 0:
                self.sessions.bind_message(
                    view.session.chat_id,
                    view.message_id,
                    view.session.session_id,
                    view.bot_key,
                )
            view.published = True
            if with_artifacts:
                self._auto_send_artifacts(view)
            return
        except TelegramError as exc:
            if exc.retry_after is not None:
                raise
            LOGGER.warning(
                "Rich Message final unavailable for %s; using HTML fallback: %s",
                view.session.session_id,
                exc,
            )
        if view.message_id is not None and view.message_id > 0:
            self._telegram(view.bot_key).edit_markdown(
                view.session.chat_id, view.message_id, rendered, reply_markup=markup
            )
        else:
            message_id = self._telegram(view.bot_key).send_markdown(
                view.session.chat_id, rendered, reply_markup=markup
            )
            view.message_id = message_id if message_id is not None else 0
        if view.message_id and view.message_id > 0:
            self.sessions.bind_message(
                view.session.chat_id,
                view.message_id,
                view.session.session_id,
                view.bot_key,
            )
        view.published = True
        if with_artifacts:
            self._auto_send_artifacts(view)

    def _auto_send_artifacts(self, view: TurnView) -> None:
        """回答发布后自动把 CLI 产物文件发送给用户。

        模式（AUTO_SEND_ARTIFACTS）：
          off    - 不自动发送，仅保留"发送文件/图片"按钮（旧行为）
          images - 只自动发送不超过 sendPhoto 上限的图片（默认）
          all    - 图片与普通文件都用 sendDocument 自动发送

        已成功发送的路径记在 view.auto_sent，发布重试时不会重复发送；
        发送失败的文件保留在按钮里作为手动兑底。只在 status 线程调用。
        """
        mode = self.config.auto_send_artifacts
        if mode == "off":
            return
        for path in view.artifacts:
            if len(view.auto_sent) >= MAX_AUTO_SENT_ARTIFACTS:
                break
            key = str(path)
            if key in view.auto_sent:
                continue
            if not path.is_file():
                continue  # 运行期间被删除
            mime_type = mimetypes.guess_type(path.name)[0] or ""
            is_image = mime_type.startswith("image/")
            if is_image and path.stat().st_size <= PHOTO_UPLOAD_MAX_BYTES:
                as_photo, label = True, "图片"
            elif mode == "all":
                as_photo, label = False, "文件"
            else:
                continue  # 图片超 sendPhoto 上限且未开启 all：保留按钮
            try:
                self._telegram(view.bot_key).send_local_file(
                    view.session.chat_id, path, as_photo=as_photo
                )
            except (TelegramError, OSError) as exc:
                LOGGER.warning("自动发送%s失败 %s: %s", label, path, exc)
                continue
            view.auto_sent.append(key)

    def _validated_local_file(self, candidate: Path) -> Path:
        candidate = candidate.expanduser().resolve(strict=True)
        if not candidate.is_file():
            raise ValueError("不是普通文件")
        if not any(
            candidate == root or candidate.is_relative_to(root)
            for root in self.config.allowed_roots
        ):
            raise ValueError("文件不在 ALLOWED_WORKDIRS 中")
        if candidate.stat().st_size > self.config.telegram_max_file_bytes:
            raise ValueError("文件超过配置的发送大小限制")
        return candidate

    def _find_artifacts(self, session: CliSession, answer: str) -> list[Path]:
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
                candidate = Path(session.cwd) / candidate
            try:
                candidate = self._validated_local_file(candidate)
            except (OSError, ValueError):
                continue
            if candidate not in found:
                found.append(candidate)
            if len(found) >= 6:
                break
        return found

    def _artifact_markup(self, view: TurnView) -> dict[str, Any] | None:
        rows: list[list[dict[str, str]]] = []
        for path in view.artifacts:
            token = self.sessions.register_artifact(
                view.session.chat_id,
                view.session.session_id,
                path,
                view.bot_key,
            )
            mime_type = mimetypes.guess_type(path.name)[0] or ""
            action = "photo" if mime_type.startswith("image/") else "file"
            icon = "图片" if action == "photo" else "文件"
            rows.append([{
                "text": f"发送{icon}：{path.name}"[:60],
                "callback_data": f"{action}:{token}",
            }])
        return {"inline_keyboard": rows} if rows else None

    def _status_loop(self) -> None:
        while not self.stop_event.wait(0.5):
            now = time.monotonic()
            with self._state_lock:
                views = list(self._turns.values())
            for view in views:
                action: str | None = None
                published_status: str | None = None
                rendered: str | None = None
                with self._state_lock:
                    if now < view.next_publish_at:
                        continue
                    if view.status == "running":
                        current = self.sessions.current(
                            view.session.chat_id, view.bot_key
                        )
                        is_current = bool(
                            current and current.session_id == view.session.session_id
                        )
                        if is_current and not view.live:
                            action = "pin"
                        elif not is_current and view.live:
                            action = "background"
                        elif not is_current:
                            # 后台冻结中：不周期刷新，等终态或切回前台。
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
                try:
                    with self._publish_lock:
                        if action == "pin":
                            if not self._pin_turn(view, now):
                                continue
                        elif action == "background":
                            self._background_turn(view, now)
                        elif published_status == "running":
                            self._publish_running_turn(view, rendered)
                        else:
                            self._publish_final_turn(
                                view, rendered, with_artifacts=published_status != "interrupted"
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
                    if action is None and view.status != published_status:
                        # A terminal event arrived while Telegram was publishing the
                        # running snapshot. Keep the view so the next pass replaces
                        # that stale message with the terminal status.
                        view.dirty = True
                        continue
                    if (
                        action is None
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
                    if action is None and published_status != "running":
                        self._turns.pop((view.session.session_id, view.turn_id), None)
                if (
                    action is None
                    and published_status is not None
                    and published_status != "running"
                ):
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
        document = message.get("document")
        photos = message.get("photo")
        if isinstance(document, dict):
            file_id = document.get("file_id") if isinstance(document.get("file_id"), str) else None
            name = str(document.get("file_name") or "document.bin")
            mime_type = str(document.get("mime_type") or mimetypes.guess_type(name)[0] or mime_type)
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
            if session.cli not in self.config.enabled_clis:
                continue
            if session.cli == "codex":
                if session.backend == "codex-app-server" and session.external_id:
                    try:
                        self.codex.resume_thread(
                            session.external_id, model=self._effective_model(session)
                        )
                        continue
                    except CodexBackendError:
                        LOGGER.exception("could not resume Codex thread for %s", session.session_id)
                try:
                    thread_id = self.codex.start_thread(
                        session.cwd, model=self._effective_model(session)
                    )
                    self.sessions.update_backend(session.session_id, "codex-app-server", thread_id)
                    self._send(session.chat_id, f"[{session.session_id}] 已迁移到 Codex app-server。")
                except (CodexBackendError, SessionError):
                    LOGGER.exception("could not migrate Codex session %s", session.session_id)
            elif session.backend != "headless-json":
                try:
                    self.sessions.update_backend(session.session_id, "headless-json", str(uuid.uuid4()))
                    self._send(session.chat_id, f"[{session.session_id}] 已迁移到结构化 JSON 后端。旧 TUI 上下文无法恢复。")
                except SessionError:
                    LOGGER.exception("could not migrate session %s", session.session_id)

    def _coalesce_updates(self, updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把同一 media_group_id 的媒体组消息合并成单条更新。

        Telegram 会把相册/多文件媒体组的每一项作为独立 message 下发，但共享
        同一个 media_group_id。若逐条处理，第一张图会先启动一个任务，后续
        图片会因会话忙碌而被当作追加（steer）或排队，而不是组成一个任务。
        这里按 batch 内顺序把相邻、同 media_group_id 的消息合并成一条携带
        ``media`` 列表的消息，并把 update_id 提升为组内最大值，避免重复消费。
        跨 batch 拆开的媒体组（罕见）不受影响，仍按单条处理。
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
            document = message.get("document")
            photos = message.get("photo")
            if isinstance(document, dict):
                media.append({"document": document})
            elif isinstance(photos, list):
                media.append({"photo": photos})
            if not caption:
                item_caption = message.get("caption")
                if isinstance(item_caption, str) and item_caption.strip():
                    caption = item_caption.strip()
        combined = {
            key: value
            for key, value in first.items()
            if key not in {"document", "photo", "caption"}
        }
        combined["media"] = media
        if caption:
            combined["caption"] = caption
        return {"update_id": max_update_id, "message": combined}

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
                    self._send(chat_id, f"接收文件失败：{exc}")
                    return
                if attachment:
                    attachments.append(attachment)
            if not attachments:
                self._send(chat_id, "无法识别这个附件。")
                return
            caption = message.get("caption")
            prompt = caption.strip() if isinstance(caption, str) and caption.strip() else "请查看并处理这些附件。"
            self._send_to_session(chat_id, session, prompt, tuple(attachments))
            return
        if isinstance(message.get("document"), dict) or isinstance(message.get("photo"), list):
            session = routed_session or self._current_or_reply(chat_id)
            if not session:
                return
            try:
                attachment = self._download_attachment(message, session)
            except TelegramError as exc:
                self._send(chat_id, f"接收文件失败：{exc}")
                return
            if not attachment:
                self._send(chat_id, "无法识别这个附件。")
                return
            caption = message.get("caption")
            prompt = caption.strip() if isinstance(caption, str) and caption.strip() else "请查看并处理这个附件。"
            self._send_to_session(chat_id, session, prompt, (attachment,))
            return
        self._send(chat_id, "目前支持文本、文件和图片；语音功能稍后接入。")

    def stop(self, *_args: object) -> None:
        self.codex.begin_shutdown()
        self.stop_event.set()

    def _dispatch_update(self, bot_key: str, update: dict[str, Any]) -> None:
        # Keep the pre-multi-bot single-dispatch ordering while each Bot long-polls
        # independently. Backend streams still run on their existing worker threads.
        with self._dispatch_lock:
            with self._bot_scope(bot_key):
                self.handle_update(update)

    def _poll_bot(self, bot_key: str) -> None:
        telegram = self._telegram(bot_key)
        offset = self.sessions.get_telegram_offset(bot_key)
        while not self.stop_event.is_set():
            try:
                updates = telegram.get_updates(offset, self.config.poll_timeout)
                for update in self._coalesce_updates(updates):
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
        if "codex" in self.config.enabled_clis:
            self.codex.start()
        self._prepare_sessions()
        self._recover_interrupted_turns()
        try:
            for bot_key, telegram in self._telegrams.items():
                try:
                    telegram.set_commands(BOT_COMMANDS)
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
        LOGGER.info("gateway stopped; CLI session IDs remain resumable")
