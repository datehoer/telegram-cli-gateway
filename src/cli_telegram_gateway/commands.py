"""直连命令扩展（Direct Command）。

一个扩展 = 一个目录 + 一个 ``manifest.json`` + 一个可执行的 ``exec`` 数组。
命中命令时由网关直接 spawn 本地进程，不经过任何 AI CLI，也不产生会话上下文。

设计边界（见 AGENTS.md「Direct Command Extensions」一节）：
- 没有插件间通信、没有热重载、没有插件自持持久状态、没有插件自定义 Telegram UI。
- 进程以网关账号权限运行；装什么扩展由部署者自己审查，网关不做安全沙箱。
- 插件只通过环境变量 + argv 收参数，通过 stdout 回结果，通过约定的进度行回进度。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .config import Config

SCHEMA_VERSION = 1
COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MANIFEST_NAME = "manifest.json"

# 进度行约定：插件在 stdout 输出「@@PROGRESS <文本>」，网关原地刷新进度卡片。
# 其余 stdout 全部缓冲到最后一次性发出。
PROGRESS_PREFIX = "@@PROGRESS"

MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 6 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 30 * 60
DEFAULT_MAX_OUTPUT_BYTES = 16384
MAX_OUTPUT_BYTES_LIMIT = 1024 * 1024
# exec 里唯一支持的替换：{extension_dir} → manifest 所在目录的绝对路径。
# 它让「脚本放在扩展目录、工作目录在别处」这种常见组合不需要写死绝对路径。
EXTENSION_DIR_TOKEN = "{extension_dir}"
# stdout 按行留尾；渲染时再按 max_output_bytes 截断。多留几倍是为了不让进度行
# 被大量普通输出挤掉，同时避免长时间下载把网关内存吃光。
OUTPUT_LINE_BUFFER = 4000
STDERR_LINE_BUFFER = 200
KILL_GRACE_SECONDS = 5.0


class CommandError(RuntimeError):
    """命令无法执行（命令不存在、参数非法、重复执行）。"""


class CommandManifestError(ValueError):
    """manifest.json 不合法；调用方跳过该扩展并告警。"""


class CommandDisabled(CommandManifestError):
    """manifest 里 enabled=false：这是正常状态，不告警。"""


def _require(mapping: dict[str, Any], key: str, kind: type, default: Any = None) -> Any:
    value = mapping.get(key, default)
    if value is None:
        raise CommandManifestError(f"缺少字段 {key}")
    if not isinstance(value, kind):
        raise CommandManifestError(f"字段 {key} 类型应为 {kind.__name__}")
    return value


def _optional_str(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CommandManifestError(f"字段 {key} 类型应为字符串")
    return value.strip()


def _bounded_int(
    mapping: dict[str, Any], key: str, default: int, low: int, high: int
) -> int:
    raw = mapping.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise CommandManifestError(f"字段 {key} 类型应为整数")
    if not low <= raw <= high:
        raise CommandManifestError(f"字段 {key} 必须在 {low}..{high} 之间")
    return raw


@dataclass(frozen=True)
class DirectCommand:
    """一个已校验的直连命令定义。"""

    id: str
    name: str
    command: str
    usage: str
    description: str
    argv: tuple[str, ...]
    cwd: Path
    manifest_path: Path
    timeout_seconds: int
    max_output_bytes: int

    @property
    def directory(self) -> Path:
        return self.manifest_path.parent

    def resolved_argv(self) -> tuple[str, ...]:
        return tuple(
            item.replace(EXTENSION_DIR_TOKEN, str(self.directory)) for item in self.argv
        )

    def spawn_argv(self) -> tuple[str, ...]:
        """实际传给 Popen 的 argv。

        cwd 可能与扩展目录不同，所以相对路径不能直接交给子进程。
        带路径分隔符的相对 argv 元素按扩展目录解析；裸命令名（如 python3）
        仍然交给 PATH 查找。
        """
        resolved: list[str] = []
        for item in self.resolved_argv():
            path = Path(item)
            if path.is_absolute():
                resolved.append(item)
            elif "/" in item or (self.directory / item).exists():
                resolved.append(str((self.directory / item).resolve()))
            else:
                resolved.append(item)
        return tuple(resolved)

    def usage_text(self) -> str:
        return self.usage or f"/{self.command}"

    def help_line(self) -> str:
        description = self.description or self.name
        return f"{self.usage_text()}  {description}"


def parse_manifest(manifest_path: Path, config: Config) -> DirectCommand:
    """校验单个 manifest；任何不合规都抛 CommandManifestError。"""
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CommandManifestError(f"无法读取 {manifest_path.name}: {exc}") from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CommandManifestError(f"{manifest_path.name} 不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CommandManifestError(f"{manifest_path.name} 顶层必须是对象")

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CommandManifestError(
            f"schema_version 必须是 {SCHEMA_VERSION}，实际为 {payload.get('schema_version')!r}"
        )
    if payload.get("enabled", True) is False:
        raise CommandDisabled("enabled=false")

    command_id = _optional_str(payload, "id")
    if not COMMAND_NAME_RE.match(command_id):
        raise CommandManifestError("字段 id 必须匹配 ^[a-z][a-z0-9_]{0,31}$")
    command = _optional_str(payload, "command")
    if not COMMAND_NAME_RE.match(command):
        raise CommandManifestError("字段 command 必须匹配 ^[a-z][a-z0-9_]{0,31}$")

    raw_exec = _require(payload, "exec", list)
    if not raw_exec:
        raise CommandManifestError("字段 exec 不能为空")
    for index, item in enumerate(raw_exec):
        if not isinstance(item, str) or not item.strip():
            raise CommandManifestError(f"exec[{index}] 必须是非空字符串")

    directory = manifest_path.parent
    raw_cwd = payload.get("cwd")
    if raw_cwd is None or raw_cwd == "":
        cwd = config.default_workdir
    else:
        if not isinstance(raw_cwd, str):
            raise CommandManifestError("字段 cwd 类型应为字符串")
        candidate = Path(raw_cwd).expanduser()
        if not candidate.is_absolute():
            candidate = directory / candidate
        cwd = candidate.resolve()
        if not cwd.is_dir():
            raise CommandManifestError(f"cwd 目录不存在: {cwd}")
    if not any(cwd == root or cwd.is_relative_to(root) for root in config.allowed_roots):
        raise CommandManifestError(
            f"cwd 必须位于 ALLOWED_WORKDIRS 内，否则产物无法回传: {cwd}"
        )

    return DirectCommand(
        id=command_id,
        name=_optional_str(payload, "name") or command_id,
        command=command,
        usage=_optional_str(payload, "usage"),
        description=_optional_str(payload, "description"),
        argv=tuple(raw_exec),
        cwd=cwd,
        manifest_path=manifest_path.resolve(),
        timeout_seconds=_bounded_int(
            payload, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS,
            MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS,
        ),
        max_output_bytes=_bounded_int(
            payload, "max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES,
            1, MAX_OUTPUT_BYTES_LIMIT,
        ),
    )


def load_direct_commands(
    config: Config,
    *,
    reserved: frozenset[str] | set[str] = frozenset(),
    warn: Callable[[str], None] | None = None,
) -> dict[str, DirectCommand]:
    """扫描所有扩展目录，返回 command -> DirectCommand。

    单个扩展出错只跳过它自己并告警，不影响网关启动，也不影响其他扩展。
    """
    emit = warn or (lambda _message: None)
    loaded: dict[str, DirectCommand] = {}
    for root in config.direct_command_roots:
        if not root.is_dir():
            continue
        for manifest_path in sorted(root.glob(f"*/{MANIFEST_NAME}")):
            try:
                command = parse_manifest(manifest_path, config)
            except CommandDisabled:
                continue
            except CommandManifestError as exc:
                emit(f"扩展命令已跳过 {manifest_path}: {exc}")
                continue
            if command.command in reserved:
                emit(
                    f"扩展命令已跳过 {manifest_path}: 命令名 /{command.command} 与内置命令冲突"
                )
                continue
            if command.command in loaded:
                emit(
                    f"扩展命令已跳过 {manifest_path}: 命令名 /{command.command} "
                    f"已被 {loaded[command.command].manifest_path} 占用"
                )
                continue
            loaded[command.command] = command
    return loaded


@dataclass
class DirectCommandRun:
    """一次命令执行的可见状态。发布逻辑由 GatewayApp 的状态循环驱动。"""

    command: DirectCommand
    chat_id: int
    bot_key: str
    args: list[str]
    raw_args: str
    user_id: int
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "running"  # running | completed | failed | interrupted
    started_at: float = field(default_factory=time.monotonic)
    progress: str = ""
    output_lines: deque[str] = field(default_factory=lambda: deque(maxlen=OUTPUT_LINE_BUFFER))
    error_lines: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_LINE_BUFFER))
    timed_out: bool = False
    message_id: int | None = None
    message_ids: list[int] = field(default_factory=list)
    dirty: bool = True
    published: bool = False
    rich_mode: bool = True
    last_edit: float = 0.0
    next_publish_at: float = 0.0
    publish_failures: int = 0
    auto_sent: list[str] = field(default_factory=list)
    # 路径 → artifact token。卡片每 10 秒原地刷新一次，复用 token 可以避免把
    # sessions 的 artifact 表刷满（那张表只保留 500 条）。
    artifact_tokens: dict[str, str] = field(default_factory=dict)
    process: subprocess.Popen[str] | None = None

    @property
    def label(self) -> str:
        return "/" + self.command.command

    def output_text(self) -> str:
        return "".join(self.output_lines).strip()

    def error_text(self) -> str:
        return "".join(self.error_lines).strip()

    def artifacts_source(self) -> str:
        text = self.output_text()
        limit = self.command.max_output_bytes * 2
        return text if len(text) <= limit else text[-limit:]


class DirectCommandRunner:
    """执行直连命令并保存可见状态；进度由 GatewayApp 的状态循环发布。"""

    def __init__(
        self, config: Config, commands: dict[str, DirectCommand] | None = None
    ) -> None:
        self.config = config
        self.commands = commands if commands is not None else {}
        self._lock = threading.RLock()
        self._runs: dict[str, DirectCommandRun] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._stopping = False

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.commands))

    def get(self, turn_id: str) -> DirectCommandRun | None:
        with self._lock:
            return self._runs.get(turn_id)

    def runs(self) -> list[DirectCommandRun]:
        with self._lock:
            return list(self._runs.values())

    def running_for_chat(self, chat_id: int, bot_key: str = "default") -> list[DirectCommandRun]:
        with self._lock:
            return [
                run
                for run in self._runs.values()
                if run.chat_id == chat_id and run.bot_key == bot_key and run.status == "running"
            ]

    def forget(self, turn_id: str) -> None:
        with self._lock:
            self._runs.pop(turn_id, None)

    def start(
        self,
        chat_id: int,
        command_name: str,
        args: list[str],
        *,
        raw_args: str = "",
        bot_key: str = "default",
        user_id: int = 0,
    ) -> DirectCommandRun:
        command = self.commands.get(command_name)
        if command is None:
            raise CommandError(f"没有名为 /{command_name} 的扩展命令")
        with self._lock:
            if any(
                run.command.command == command_name
                and run.chat_id == chat_id
                and run.bot_key == bot_key
                and run.status == "running"
                for run in self._runs.values()
            ):
                raise CommandError(f"/{command_name} 正在运行，请等它结束或先 /interrupt")
            if self._stopping:
                raise CommandError("网关正在退出")

            run = DirectCommandRun(
                command=command,
                chat_id=chat_id,
                bot_key=bot_key,
                args=list(args),
                raw_args=raw_args or " ".join(args),
                user_id=user_id,
            )
            self._runs[run.turn_id] = run

        try:
            self._spawn(run)
        except OSError as exc:
            with self._lock:
                run.status = "failed"
                run.error_lines.append(f"无法启动命令: {exc}")
            raise CommandError(f"无法启动 /{command_name}: {exc}") from exc
        return run

    def _spawn(self, run: DirectCommandRun) -> None:
        command = run.command
        env = os.environ.copy()
        # 让 Python 写的插件默认行缓冲；否则进度行会卡在子进程缓冲区里。
        env["PYTHONUNBUFFERED"] = "1"
        # 网关自己的配置不一定会出现在扩展进程的环境里（systemd 只给 .env 覆盖的
        # 那几行），扩展却需要知道发送上限这类值。显式透传带前缀的变量。
        for key in ("TELEGRAM_MAX_FILE_BYTES",):
            if key not in env:
                env[key] = str(getattr(self.config, "telegram_max_file_bytes", ""))
        env.update(
            {
                "TG_COMMAND_ID": command.id,
                "TG_COMMAND": command.command,
                "TG_COMMAND_NAME": command.name,
                "TG_ARGS_JSON": json.dumps(run.args, ensure_ascii=False),
                # 未经任何分词的原始文本，Cookie / JSON / 引号都保持原样。
                "TG_RAW_ARGS": run.raw_args,
                "TG_ARGV0": run.args[0] if run.args else "",
                "TG_CHAT_ID": str(run.chat_id),
                "TG_USER_ID": str(run.user_id),
                "TG_WORKDIR": str(command.cwd),
                "TG_MANIFEST_DIR": str(command.directory),
            }
        )
        process = subprocess.Popen(
            [*command.spawn_argv(), *run.args[1:]],
            cwd=str(command.cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        with self._lock:
            run.process = process

        threading.Thread(
            target=self._read_stdout,
            args=(run, process),
            name=f"direct-cmd-{command.command}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(run, process),
            name=f"direct-cmd-err-{command.command}",
            daemon=True,
        ).start()

        timer = threading.Timer(command.timeout_seconds, self._expire, args=(run,))
        timer.daemon = True
        with self._lock:
            if run.status != "running":
                # 进程在定时器注册前就结束了，不要再持有它。
                timer.cancel()
                return
            self._timers[run.turn_id] = timer
        timer.start()

    def _read_stdout(self, run: DirectCommandRun, process: subprocess.Popen[str]) -> None:
        stdout = process.stdout
        if stdout is not None:
            try:
                for raw_line in stdout:
                    line = raw_line.rstrip("\n")
                    if line.startswith(PROGRESS_PREFIX):
                        text = line[len(PROGRESS_PREFIX):].strip()
                        with self._lock:
                            if run.status == "running" and text:
                                run.progress = text
                                run.dirty = True
                        continue
                    with self._lock:
                        run.output_lines.append(line + "\n")
                        run.dirty = True
            except (OSError, ValueError):  # 进程被杀死时管道会先关闭
                pass
        try:
            returncode = process.wait()
        except OSError:
            returncode = -1
        finally:
            stdout.close()
        with self._lock:
            self._cancel_timer(run.turn_id)
            if run.status == "running":
                if returncode == 0:
                    run.status = "completed"
                else:
                    run.status = "failed"
                    if not run.timed_out:
                        detail = run.error_text()
                        run.error_lines.append(
                            f"命令退出码 {returncode}" + (f"\n{detail}" if detail else "")
                        )
            run.dirty = True

    def _read_stderr(self, run: DirectCommandRun, process: subprocess.Popen[str]) -> None:
        stderr = process.stderr
        if stderr is None:
            return
        try:
            for raw_line in stderr:
                with self._lock:
                    run.error_lines.append(raw_line.rstrip("\n") + "\n")
        except (OSError, ValueError):
            pass
        finally:
            stderr.close()

    def _cancel_timer(self, turn_id: str) -> None:
        timer = self._timers.pop(turn_id, None)
        if timer is not None:
            timer.cancel()

    def _expire(self, run: DirectCommandRun) -> None:
        with self._lock:
            if run.status != "running":
                return
            run.timed_out = True
            run.error_lines.append(
                f"执行超过 {run.command.timeout_seconds} 秒，已终止\n"
            )
            run.dirty = True
        self._terminate(run, mark_interrupted=True)

    def _terminate(self, run: DirectCommandRun, *, mark_interrupted: bool) -> None:
        """终止进程组；状态先落定，避免读取线程把终止误报成命令失败。"""
        with self._lock:
            process = run.process
            if mark_interrupted:
                if run.status != "running":
                    return
                run.status = "interrupted"
                run.dirty = True
                self._cancel_timer(run.turn_id)
        if process is not None and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    process.terminate()
                except OSError:
                    return

            def escalate() -> None:
                if process.poll() is None:
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        try:
                            process.kill()
                        except OSError:
                            pass

            threading.Timer(KILL_GRACE_SECONDS, escalate).start()

    def interrupt(self, turn_id: str) -> bool:
        with self._lock:
            run = self._runs.get(turn_id)
            if run is None or run.status != "running":
                return False
        self._terminate(run, mark_interrupted=True)
        return True

    def interrupt_chat(self, chat_id: int, bot_key: str = "default") -> int:
        interrupted = 0
        for run in self.running_for_chat(chat_id, bot_key):
            if self.interrupt(run.turn_id):
                interrupted += 1
        return interrupted

    def shutdown(self) -> None:
        with self._lock:
            self._stopping = True
            runs = [run for run in self._runs.values() if run.status == "running"]
        for run in runs:
            self.interrupt(run.turn_id)
