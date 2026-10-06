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
from .models import clear_listing_cache, list_efforts, list_models
from .sessions import CliSession, SessionError, SessionManager
from .telegram import TelegramClient, TelegramError
from .usage import codex_usage, context_lines, quota_lines, usage_lines


LOGGER = logging.getLogger("telegram-cli-gateway")
MAX_PENDING_INPUTS_PER_SESSION = 20
MAX_PUBLISH_RETRY_SECONDS = 30.0
STREAM_SEGMENT_UNITS = 1500
MAX_AUTO_SENT_ARTIFACTS = 3
PHOTO_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # Telegram sendPhoto 上限

# 收到的文件按客户端的发送方式落在不同字段（WAV/MP3 常是 audio），全部转交给 CLI。
# 值是缺 mime_type 时的类型：video_note 没有这个字段，voice 几乎总是 OGG/Opus。
# GIF 的 animation 为兼容旧客户端同时带 document，所以 document 排第一；贴纸不转交。
TELEGRAM_FILE_FIELDS = {
    "document": "application/octet-stream",
    "audio": "application/octet-stream",
    "video": "video/mp4",
    "animation": "video/mp4",
    "voice": "audio/ogg",
    "video_note": "video/mp4",
}

# 客户端把超过 4096 字符（按 UTF-16 计）的长文本拆成多条消息，没有 media_group_id
# 之类的分组标记。Telegram Desktop 在 2048~4096 之间挑段落/换行/空格断开，Android
# 在 4096 处硬切；服务端会去掉每段首尾的空白，片段也常常分几个 getUpdates 批次到达。
# 这里把它们拼回一条输入，否则后半段会变成 steer（追加）或排队。
TELEGRAM_TEXT_MAX_CHARS = 4096
TEXT_FRAGMENT_MIN_CHARS = 2000
TEXT_FRAGMENT_MAX_PARTS = 24
# 拼好的文本经 argv 交给 claude/grok/pi，Linux 单个参数上限 128 KiB。
TEXT_FRAGMENT_MAX_TOTAL_BYTES = 100_000
# 批次末尾像未完的片段时，轮询线程先等后续片段，安静这么久或到总上限才分发。
TEXT_FRAGMENT_POLL_SECONDS = 0.25
TEXT_FRAGMENT_QUIET_SECONDS = 1.5
TEXT_FRAGMENT_MAX_WAIT_SECONDS = 10.0

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
/status             查看 CLI 状态、上下文用量与账户额度
/context            查看上下文用量与剩余空间
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
/commands           列出直连命令扩展
/help               查看帮助

支持的 CLI：claude、codex、grok、pi。
普通文本、文件和图片会直接交给当前会话。所有 CLI 均已启用免审批模式。
直连命令扩展（/commands 查看）不调用 AI，也不占用会话上下文。"""

BOT_COMMANDS = (
    ("new", "新建 CLI 会话"),
    ("use", "切换 CLI 或会话"),
    ("back", "返回上一个会话"),
    ("sessions", "列出会话"),
    ("tasks", "查看任务队列"),
    ("tgstats", "查看 Telegram API 统计"),
    ("where", "显示当前会话"),
    ("status", "查看 CLI 状态与额度"),
    ("context", "查看上下文用量"),
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
    ("commands", "列出直连命令扩展"),
    ("help", "显示帮助"),
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
        return "当前未安装直连命令扩展。把扩展目录放进 extensions/ 或配置 COMMAND_DIR。"
    lines = ["直连命令扩展", ""]
    lines.extend(command.help_line() for command in commands)
    lines.append("")
    lines.append("这些命令直接在本机执行，不调用 AI，也不进入会话上下文。")
    return "\n".join(lines)


def _utf16_len(text: str) -> int:
    """Telegram 按 UTF-16 码元计文本长度，emoji 等算两个。"""
    return len(text.encode("utf-16-le")) // 2


def _file_field(message: dict[str, Any]) -> str | None:
    """消息里携带单个文件的字段名；照片（多尺寸列表）单独处理。"""
    return next(
        (field for field in TELEGRAM_FILE_FIELDS if isinstance(message.get(field), dict)),
        None,
    )


def _fragment_separator(previous: str, following: str, multiline: bool) -> str:
    """补回断点处被服务端去掉的空白。

    恰好 4096 的片段是硬切（Android），断点可能落在词中间，原样相接；其余是客户端
    在空白处断开的（Desktop 优先段落和换行），多行文本补换行，单行文本补空格。
    """
    if previous[-1:].isspace() or following[:1].isspace():
        return ""
    if _utf16_len(previous) >= TELEGRAM_TEXT_MAX_CHARS:
        return ""
    return "\n" if multiline else " "


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
        return f"{configured}（网关默认）" if configured else "CLI 默认"

    def _effort_display(self, session: CliSession) -> str:
        if session.effort:
            return session.effort
        configured = self.config.default_effort_for(session.cli)
        return f"{configured}（网关默认）" if configured else "CLI 默认"

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
                ).replace(
                    "直连命令扩展（/commands 查看）不调用 AI，也不占用会话上下文。",
                    _command_help_section(tuple(self.commands.values())),
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
            if self._session_is_busy(session):
                self._send(chat_id, f"{session.label} 正在运行，无需恢复。")
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
            if self._session_is_busy(session):
                self._send(chat_id, f"{session.label} 正在运行，请用 /interrupt 中断。")
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
                self._retire_session(session, delete=True)
            except (CodexBackendError, SessionError):
                LOGGER.exception("could not interrupt %s before stopping", session.session_id)
                self._send(chat_id, "停止失败，会话记录已保留，请稍后重试。")
                return
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
        if command == "commands":
            self._send(chat_id, self._commands_view())
            return
        if command in self.commands:
            self._start_direct_command(chat_id, command, raw_args)
            return
        self._send(chat_id, "未知命令。使用 /help 查看可用命令。")

    def _commands_view(self) -> str:
        if not self.commands:
            return (
                "当前未安装直连命令扩展。\n"
                "把扩展目录放到 extensions/，或配置 COMMAND_DIR。"
            )
        lines = ["直连命令扩展（不调用 AI）", ""]
        for command in sorted(self.commands.values(), key=lambda item: item.command):
            detail = command.description or command.name
            lines.append(f"/{command.command} · {command.name}")
            lines.append(f"  用法：{command.usage_text()}")
            lines.append(f"  说明：{detail}")
            lines.append(f"  目录：{command.cwd}")
            lines.append(f"  定义：{command.manifest_path}")
        return "\n".join(lines)

    def _start_direct_command(self, chat_id: int, command_name: str, raw_args: str) -> None:
        # 直接命令不做 shell 分词：整段原文交给扩展自己解析（argv[0] 在 TG_ARGV0，
        # 其余每个换行分隔的非空行是一个参数）。shlex 会因为 Cookie、JSON、r'' 这类
        # 内容里的引号直接报 "No closing quotation"，而那些字符在这里是数据不是语法。
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
            self._retire_session(session, delete=False)
        except (CodexBackendError, SessionError):
            LOGGER.exception("could not interrupt %s before archiving", session.session_id)
            self._send(chat_id, "归档失败，会话记录已保留，请稍后重试。")
            return
        self._send(chat_id, f"已归档 {session.label}。用 /sessions all 查看或恢复。")

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
                claude_quota = ["账户额度：暂时无法查询，请稍后重试。"]
            session = self.sessions.get(session.session_id) or session
        lines = [f"{session.label}（{session.session_id}） · {session.cli}"]
        if full:
            lines.extend([
                f"状态：{self._session_status_text(session)}",
                f"目录：{session.cwd}",
                f"模型：{self._model_display(session)}",
                f"推理力度：{self._effort_display(session)}",
            ])
            with self._state_lock:
                view = next((
                    view for (session_id, _turn_id), view in self._turns.items()
                    if session_id == session.session_id and view.status == "running"
                ), None)
                if view:
                    lines.append(f"当前任务：已运行 {max(0, int(time.monotonic() - view.started_at))}秒")
                    if (
                        view.model_label != self._model_header_label(session)
                        or view.effort_label != self._effort_header_label(session)
                    ):
                        lines.append(f"当前任务模型：{view.model_label} · 推理力度：{view.effort_label}")
        lines.extend(context_lines(session.usage))
        if claude_context_unavailable:
            lines.append("Claude 原生窗口查询暂时不可用，当前显示已保存数据。")
        if full:
            lines.extend(usage_lines(session.usage))
            if session.backend == "codex-app-server":
                try:
                    lines.extend(quota_lines(self.codex.read_rate_limits()))
                except CodexBackendError:
                    lines.append("账户额度：暂时无法查询，请稍后重试。")
            elif session.cli == "claude" and session.backend == "headless-json":
                lines.extend(claude_quota or ["账户额度：Claude 未返回额度数据。"])
            else:
                lines.append(f"账户额度：{session.cli} 当前接入方式未提供额度查询。")
        return "\n".join(lines)

    def _surface_switched_session(self, session: CliSession) -> None:
        with self._publish_lock:
            self._surface_switched_session_locked(session)

    def _surface_switched_session_locked(self, session: CliSession) -> None:
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
        for model in models:
            marker = "✓ " if model == current else ""
            buttons.append([{
                "text": f"{marker}{model}"[:64],
                "callback_data": f"model:{session.session_id}:{self._model_choice_id(model)}",
            }])
        reset_label = "恢复网关默认" if self.config.default_model_for(session.cli) else "恢复默认"
        buttons.append([{"text": reset_label, "callback_data": f"modelclear:{session.session_id}"}])
        if not models:
            lines.append("该 CLI 无法列出模型，请直接 /model <名称>。")
        return "\n".join(lines), {"inline_keyboard": buttons}

    @staticmethod
    def _model_choice_id(model: str) -> str:
        # Catalog ordering can change between rendering and clicking a button.
        return hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]

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

        if action in {"file", "photo", "video"}:
            resolved = self.sessions.resolve_artifact(
                chat_id, target, self._active_bot_key()
            )
            if not resolved:
                self.telegram.answer_callback_query(query_id, "文件已失效")
                return
            _session, path = resolved
            try:
                path = self._validated_local_file(path)
            except (OSError, ValueError) as exc:
                LOGGER.warning("artifact send failed: %s", exc)
                self._send(chat_id, f"发送文件失败：{exc}")
                return
            with self._state_lock:
                uploading = target in self._artifact_uploads
                self._artifact_uploads.add(target)
            if uploading:
                try:
                    self.telegram.answer_callback_query(query_id, "正在发送，请稍候")
                except TelegramError:
                    pass
                return
            try:
                self.telegram.answer_callback_query(query_id, "正在发送")
            except TelegramError as exc:
                with self._state_lock:
                    self._artifact_uploads.discard(target)
                LOGGER.warning("artifact send failed: %s", exc)
                self._send(chat_id, f"发送文件失败：{exc}")
                return
            self._run_in_background(
                f"artifact-{target}", self._send_artifact_file, chat_id, target, path, action
            )
            return

        if action == "sendpath":
            # 超过 Bot API 上传上限的产物没法当文件发，只能回一个本机路径。
            resolved = self.sessions.resolve_artifact(chat_id, target, self._active_bot_key())
            if not resolved:
                self.telegram.answer_callback_query(query_id, "路径已失效")
                return
            try:
                self.telegram.answer_callback_query(query_id, "已发送路径")
            except TelegramError:
                pass
            path = resolved[1]
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            self._send(
                chat_id,
                f"产物超过 Telegram 发送上限，这里只给本机路径：\n`{path}`\n"
                f"大小 {size / (1024 * 1024):.1f} MiB / 上限 "
                f"{self.config.telegram_max_file_bytes / (1024 * 1024):.0f} MiB",
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
                selected = [model for model in models if self._model_choice_id(model) == index_text]
                if len(selected) == 1:
                    value = selected[0]
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
                self._retire_session(session, delete=False)
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
                self._retire_session(session, delete=True)
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
            self._send(chat_id, f"发送文件失败：{exc}")
        finally:
            with self._state_lock:
                self._artifact_uploads.discard(token)

    def _interrupt_session(self, session: CliSession) -> bool:
        if not self.sessions.get_in_flight(session.session_id):
            # 会话空闲时，/interrupt 退为“中断此聊天当前正在运行的直连命令”。
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
            with self._state_lock:
                # Claim the session so input sent during compaction is queued and
                # started once the compaction process exits.
                busy = self._session_is_busy_locked(session)
                if not busy:
                    self._turn_claims.add(session.session_id)
            if busy:
                self._send(chat_id, f"{session.label} 正在运行，请先 /interrupt 再压缩。")
                return
            self._send(chat_id, f"正在压缩 {session.label} 的上下文…")
            self._run_in_background(
                f"compact-{session.session_id}", self._run_pi_compaction, chat_id, session
            )
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

    def _run_pi_compaction(self, chat_id: int, session: CliSession) -> None:
        try:
            result = self.headless.compact_session(session)
        except HeadlessBackendError as exc:
            with self._state_lock:
                interrupted = session.session_id in self._interrupted_sessions
                self._interrupted_sessions.discard(session.session_id)
            self._send(chat_id, "压缩已中断。" if interrupted else f"压缩失败：{exc}")
        else:
            self._send(chat_id, f"{session.label}：{result}")
        finally:
            self._finish_turn(session)

    def _clear_session(self, chat_id: int, session: CliSession) -> None:
        with self._backend_reload_lock, self._publish_lock:
            self._clear_session_locked(chat_id, session)

    def _clear_session_locked(self, chat_id: int, session: CliSession) -> None:
        """当前会话开新对话：网关记录保留，但 CLI 上下文清零重来。

        Codex 建新 thread 并归档旧 thread；headless 换新 external_id 并重置
        turn_count（下次自动走首轮模式）。会话忙碌时拒绝。
        """
        if self._session_is_busy(session):
            self._send(chat_id, f"{session.label} 正在运行，请先 /interrupt 再 /clear。")
            return
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
            self._reset_session_result(session)
            self._send(chat_id, f"{session.label} 已开新对话（旧线程已归档）。")
            return
        if session.backend == "headless-json":
            self.sessions.rotate_external_id(session.session_id)
            self._reset_session_result(session)
            self._send(chat_id, f"{session.label} 已开新对话，上下文已清空。")
            return
        self._send(chat_id, f"{session.label} 不支持 /clear。")

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
            self._send(chat_id, f"此 Bot 未启用 {session.cli}，不能继续该会话。")
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
                    with self._state_lock:
                        view = self._turns.get((session.session_id, active_turn))
                        if view:
                            view.steered += 1
                            view.steer_notices.append((view.steered, steer_boundary))
                            view.revision += 1
                            view.dirty = True
                    if view is None:
                        # The publisher may have finished while turn/steer replied.
                        self._run_in_background(
                            "steer-ack", self._send, chat_id,
                            f"[{session.session_id}] 已追加要求，CLI 已接收。", bot_key,
                        )
                    return
                except CodexBackendError as exc:
                    if exc.ambiguous:
                        self._send(chat_id, "追加请求的结果尚未确认，为避免重复执行，未自动重发。请查看当前任务输出。")
                        return
                    LOGGER.warning("Codex steer failed; queued instead: %s", exc)
        self._start_session_turn(chat_id, session, text, attachments)

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
                            f"{session.label} 的等待队列已满（最多 {MAX_PENDING_INPUTS_PER_SESSION} 条）。"
                        )
                    else:
                        queued_notice = (
                            f"{session.label} 正在运行；已加入队列（第 {position} 条）。"
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
                f"此 Bot 未启用 {session.cli}，不能启动任务。",
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
                raise SessionError(f"不支持的会话后端：{session.backend}")
            self.sessions.set_in_flight(
                session.session_id, session.chat_id, turn_id, bot_key
            )
            self._set_session_state(session.session_id, STATE_WORKING)
        except (CodexBackendError, HeadlessBackendError, SessionError) as exc:
            start_error = exc
            self._send(chat_id, f"发送失败：{exc}", bot_key)
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
                self._send(chat_id, "启动结果未确认，等待队列已暂停。请先核对原生会话，避免重复执行。", bot_key)
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
        """在网关启动时处理上次遗留的进行中任务。

        用户主动中断的任务已在中断时清除 in_flight，不会出现在这里；
        能留下的都是意外中断（网关重启/关机连累、进程被杀死）。
        默认只发提示，由用户用 /resume 或 /cancel 决定；AUTO_RESUME=true
        时直接自动恢复（复用 headless 的 resume 模式续跑同一会话）。
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
            label = "Codex 正在重试" if params.get("willRetry") is True else "Codex 报错，等待任务结束"
            self._update_turn(session, turn_id, "notice", f"{label}：{self._display_value(detail)}")

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
        label = {
            "running": "运行中",
            "completed": "完成",
            "failed": "失败",
            "interrupted": "已中断",
        }.get(view.status, view.status)
        if background and view.status == "running":
            label = "后台运行中"
        if record_text is not None:
            label = f"运行记录 · 第 {view.progress_page} 段"
        status = f"[{view.session.session_id}] · {label} {elapsed}秒"
        if view.steered:
            status += f" · 已追加 {view.steered} 次"
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
                    sections[0] += f" · 第 {view.progress_page} 段"
            if body.strip():
                sections.append(body.strip("\n"))
            if view.command and not background:
                command = view.command[-700:].replace("```", "` ` `")
                sections.append(f"当前命令：\n```text\n{command}\n```")
            if view.command_output and not background:
                output = view.command_output[-500:].replace("```", "` ` `")
                sections.append(f"命令输出：\n```text\n{output}\n```")
        elif answer:
            sections.append(answer)
        elif view.status == "completed":
            if self._turn_progress(view).strip():
                sections.append("任务已结束，过程输出见运行记录；CLI 没有提供单独的最终回答。")
            else:
                sections.append(
                    "⚠️ 模型已结束，但没有返回可见回答。"
                    "为避免重复执行操作，网关未自动重试。"
                )
        if view.status == "interrupted" and view.resumable:
            sections.append("⚠️ 任务被中断。回复 /resume 继续，或 /cancel 放弃。")
        if view.status == "running" and view.notice:
            notice = view.notice.replace("```", "` ` `")
            sections.append(f"提示：\n```text\n{notice}\n```")
        if view.error:
            error = view.error.replace("```", "` ` `")
            sections.append(f"错误：\n```text\n{error}\n```")
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
        """切换走时把当前卡片定格为“后台运行中”，停止周期刷新。"""
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
        text = f"[{view.session.session_id}] 已追加第 {number} 次要求，CLI 已接收。\n后续进展会显示在这条消息下方。"
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

    # 官方 Bot API 默认 45 MiB；配置本地 --local 服务后默认 1 GiB。
    @property
    def artifact_send_limit(self) -> int:
        return self.config.telegram_max_file_bytes

    def _auto_send_artifacts(self, view: TurnView) -> None:
        """回答发布后自动把 CLI 产物文件发送给用户。

        模式（AUTO_SEND_ARTIFACTS）：
          off    - 不自动发送，仅保留"发送文件/图片/视频"按钮（默认）
          images - 只自动发送不超过 sendPhoto 上限的图片
          all    - 图片、MP4 视频和普通文件按各自的 Telegram 类型自动发送

        超过 Bot API 上传上限的产物永远不会自动发送，也不会提供“发送文件”
        按钮：卡片上只给一个回本机路径的按钮。

        已成功发送的路径记在 view.auto_sent，发布重试时不会重复发送；
        发送失败的文件保留在按钮里作为手动兑底。只在当前 session 的终态发布时调用；
        后台完成只更新原卡片上的按钮，不往聊天底部塞文件。
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
                send_options, label = {"as_photo": True}, "图片"
            elif mode == "all":
                if path.suffix.lower() == ".mp4":
                    send_options, label = {"as_video": True}, "视频"
                else:
                    send_options, label = {"as_photo": False}, "文件"
            else:
                continue  # 图片超 sendPhoto 上限且未开启 all：保留按钮
            try:
                self._telegram(bot_key).send_local_file(chat_id, path, **send_options)
            except (TelegramError, OSError) as exc:
                LOGGER.warning("自动发送%s失败 %s: %s", label, path, exc)
                continue
            auto_sent.append(key)

    def _validated_local_file(self, candidate: Path) -> Path:
        """校验一个可以发出去的本地产物（目录 + 发送上限）。"""
        candidate = self._validated_artifact_path(candidate)
        if candidate.stat().st_size > self.config.telegram_max_file_bytes:
            raise ValueError("文件超过配置的发送大小限制")
        return candidate

    def _validated_artifact_path(self, candidate: Path) -> Path:
        """校验一个可以“回路径”的本地产物：只要求存在且在 ALLOWED_WORKDIRS 内。

        大文件超出 Bot API 上传上限后依然值得告知用户，只是不能当文件发。
        """
        candidate = candidate.expanduser().resolve(strict=True)
        if not candidate.is_file():
            raise ValueError("不是普通文件")
        if not any(
            candidate == root or candidate.is_relative_to(root)
            for root in self.config.allowed_roots
        ):
            raise ValueError("文件不在 ALLOWED_WORKDIRS 中")
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
                action, icon = "sendpath", "路径："
            else:
                mime_type = mimetypes.guess_type(path.name)[0] or ""
                if mime_type.startswith("image/"):
                    action, icon = "photo", "图片："
                elif path.suffix.lower() == ".mp4":
                    action, icon = "video", "视频："
                else:
                    action, icon = "file", "文件："
            token = tokens.get(str(path))
            if token is None:
                token = self.sessions.register_artifact(
                    chat_id, owner_id, path, bot_key, only_path=oversized
                )
                tokens[str(path)] = token
            rows.append([{
                "text": f"发送{icon}{path.name}"[:60],
                "callback_data": f"{action}:{token}",
            }])
        return {"inline_keyboard": rows} if rows else None

    # --- 直连命令扩展 ---
    #
    # 命令卡片有意不走 _turns/TurnView 那套流程：命令没有 CLI 会话、不参与队列、
    # 不写入 last_completed_message，也不应该被“切回会话时复制最后结果”影响。
    # 它们复用的是产物校验、发送按钮和原地编辑这几个稳定边界。

    def _render_direct_command(self, run: DirectCommandRun, now: float) -> tuple[str, str]:
        elapsed = max(0, int(now - run.started_at))
        label = {
            "running": "运行中",
            "completed": "完成",
            "failed": "失败",
            "interrupted": "已中断",
        }.get(run.status, run.status)
        header = f"/{run.command.command} · {run.command.name}\n{label} {elapsed}秒"
        sections = [header]
        if run.args:
            sections.append("参数：`" + " ".join(run.args)[:300] + "`")
        output = run.output_text()
        limit = run.command.max_output_bytes
        truncated = len(output) > limit
        visible = output[-limit:] if truncated else output
        if run.status == "running":
            if run.progress:
                sections.append(f"进度：{run.progress}")
            if visible:
                sections.append("输出：\n```text\n" + visible + "\n```")
            if not run.progress and not visible:
                sections.append("已启动，等待输出…")
        else:
            if truncated:
                sections.append(f"（输出仅保留最后 {limit} 字节）")
            # 命令输出多是自己排好版的文本（帮助、进度、路径清单）。Telegram 的
            # Rich Message 渲染器会把单换行当成软换行合并成一个段落，所以这里加上
            # 硬换行标记，保住插件自己的排版。
            sections.append(harden_rich_markdown(visible) or "命令没有输出。")
        error = run.error_text()
        if error and (run.status != "completed" or run.timed_out):
            error = error[-limit:].replace("```", "` ` `")
            sections.append("错误：\n```text\n" + error + "\n```")
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
        """发新卡或原地编辑旧卡。返回是否真的写入了 Telegram。"""
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
                    # 用户已经切走会话，或另一条命令卡已是底部卡片：不再抢底部位置，
                    # 等终态再编辑原卡。
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
        """命令启动后立即发第一条卡片；失败只记日志，等状态循环重试。"""
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
        """状态循环里发布直连命令卡片；终态发布成功后删除运行时状态。"""
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
                # 终态已写入 Telegram，运行时状态可以释放；回复路由和文件按钮
                # 分别存在 sessions 的路由表和 artifact 表里，不依赖这个对象。
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
                            # 后台冻结中：不周期刷新，等终态或切回前台。
                            continue
                    elif not is_current:
                        # 后台终态绝不能在当前聊天底部发新消息或自动发文件。
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
                # 语音和圆形视频没有文件名，手机现拍的视频通常也没有。
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
        """把被客户端拆开的长文本片段拼回一条更新。

        客户端把超长文本拆成多条普通消息，没有 media_group_id 之类的分组标记：
        片段按顺序到达，除最后一段外每段都不短于 TEXT_FRAGMENT_MIN_CHARS。只合并
        这种保守模式，避免把用户连续发送的若干条普通消息误当成一条。跨批次到达
        的片段由 _await_text_fragments 先收进同一批。
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
        """客户端拆出的非末段都超过 2048（Desktop 只在后半段找断点），更短的就是末段。"""
        return _utf16_len(message["text"]) >= TEXT_FRAGMENT_MIN_CHARS

    @staticmethod
    def _fragment_message(update: dict[str, Any]) -> dict[str, Any] | None:
        """仅纯文本消息参与片段合并；附件、相册、转发都不属于长文本拆分。"""
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

        # 只有第一段能带 reply_to_message（用户在回复某条消息时输入超长文本）。
        # 后续段都带回复时就说明这是连续发的多条回复，不能拼在一起。
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
        # 拼接后 entities 的偏移不再对应原文，直接丢掉。
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
        """批次末尾像未完的长文本片段时，等后续片段进入同一批再分发。

        客户端逐条发送片段，Bot API 收到第一条就结束长轮询，后续片段落在下一批。
        这里用同一个 offset 重新拉取：已拿到的 update 不会被确认，等待中途重启也
        不会丢；片段停止增长、末段变短或等满上限后，交给 _coalesce_text_fragments。
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
            # 回复一张直连命令卡片不应该把文本送进当前 CLI 会话。
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
        if _file_field(message) or isinstance(message.get("photo"), list):
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
        self._send(chat_id, "目前支持文本和各类文件（图片、音频、视频、语音等）；贴纸这类消息不会转给 CLI。")

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
        """扩展命令也注册到 Telegram 的 / 菜单，避免用户靠猜。"""
        return tuple(
            (command.command, (command.description or command.name)[:120])
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
                    telegram.set_commands((*BOT_COMMANDS, *self._command_menu_entries()))
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
