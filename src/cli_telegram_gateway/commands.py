"""Direct command extensions.

An extension is a directory with a ``manifest.json`` and an executable ``exec`` array.
When its command is invoked, the gateway spawns the local process directly: no AI CLI
runs and no session context is created.

Boundaries (see "Direct Command Extensions" in AGENTS.md):
- No inter-extension communication, hot reload, extension-owned persistent state or
  extension-defined Telegram UI.
- Processes run with the gateway account's permissions; whoever installs an extension
  reviews it. The gateway is not a sandbox.
- Extensions receive input through environment variables and argv, return results on
  stdout, and report progress through the agreed progress lines.
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
from .i18n import LANGUAGES, current_language, tr

SCHEMA_VERSION = 1
COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MANIFEST_NAME = "manifest.json"

# Progress lines: an extension prints "@@PROGRESS <text>" on stdout and the gateway
# updates the card in place. All other stdout is buffered and sent at the end.
PROGRESS_PREFIX = "@@PROGRESS"

MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 6 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 30 * 60
DEFAULT_MAX_OUTPUT_BYTES = 16384
MAX_OUTPUT_BYTES_LIMIT = 1024 * 1024
# The only substitution in exec: {extension_dir} becomes the manifest's absolute
# directory, so a script beside the manifest can run with a cwd elsewhere.
EXTENSION_DIR_TOKEN = "{extension_dir}"
# Keep the tail of stdout by line and cut it to max_output_bytes when rendering. The
# margin keeps progress lines from being crowded out without letting long downloads
# exhaust gateway memory.
OUTPUT_LINE_BUFFER = 4000
STDERR_LINE_BUFFER = 200
KILL_GRACE_SECONDS = 5.0


class CommandError(RuntimeError):
    """The command cannot run (unknown command, invalid arguments, already running)."""


class CommandManifestError(ValueError):
    """manifest.json is invalid; the caller skips the extension with a warning."""


class CommandDisabled(CommandManifestError):
    """The manifest sets enabled=false: a normal state that needs no warning."""


def _require(mapping: dict[str, Any], key: str, kind: type, default: Any = None) -> Any:
    value = mapping.get(key, default)
    if value is None:
        raise CommandManifestError(f"missing field {key}")
    if not isinstance(value, kind):
        raise CommandManifestError(f"field {key} must be of type {kind.__name__}")
    return value


def _optional_str(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CommandManifestError(f"field {key} must be a string")
    return value.strip()


def _bounded_int(
    mapping: dict[str, Any], key: str, default: int, low: int, high: int
) -> int:
    raw = mapping.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise CommandManifestError(f"field {key} must be an integer")
    if not low <= raw <= high:
        raise CommandManifestError(f"field {key} must be between {low} and {high}")
    return raw


@dataclass(frozen=True)
class DirectCommand:
    """A validated direct command definition."""

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
    # Optional display text per language from the manifest's "i18n" object.
    translations: dict[str, dict[str, str]] = field(default_factory=dict, hash=False, compare=False)

    @property
    def directory(self) -> Path:
        return self.manifest_path.parent

    def resolved_argv(self) -> tuple[str, ...]:
        return tuple(
            item.replace(EXTENSION_DIR_TOKEN, str(self.directory)) for item in self.argv
        )

    def spawn_argv(self) -> tuple[str, ...]:
        """The argv passed to Popen.

        The cwd may differ from the extension directory, so relative paths cannot go to
        the child as they are. Relative argv items with a path separator resolve against
        the extension directory; bare command names (such as python3) still use PATH.
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

    def _display(self, key: str) -> str:
        return self.translations.get(current_language(), {}).get(key) or getattr(self, key)

    def display_name(self) -> str:
        return self._display("name")

    def display_description(self) -> str:
        return self._display("description")

    def usage_text(self) -> str:
        return self._display("usage") or f"/{self.command}"

    def help_line(self) -> str:
        description = self.display_description() or self.display_name()
        return f"{self.usage_text()}  {description}"


def parse_manifest(manifest_path: Path, config: Config) -> DirectCommand:
    """Validate one manifest; any violation raises CommandManifestError."""
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CommandManifestError(f"cannot read {manifest_path.name}: {exc}") from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CommandManifestError(f"{manifest_path.name} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CommandManifestError(f"{manifest_path.name} must contain a JSON object")

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CommandManifestError(
            f"schema_version must be {SCHEMA_VERSION}, got {payload.get('schema_version')!r}"
        )
    if payload.get("enabled", True) is False:
        raise CommandDisabled("enabled=false")

    command_id = _optional_str(payload, "id")
    if not COMMAND_NAME_RE.match(command_id):
        raise CommandManifestError("field id must match ^[a-z][a-z0-9_]{0,31}$")
    command = _optional_str(payload, "command")
    if not COMMAND_NAME_RE.match(command):
        raise CommandManifestError("field command must match ^[a-z][a-z0-9_]{0,31}$")

    raw_exec = _require(payload, "exec", list)
    if not raw_exec:
        raise CommandManifestError("field exec must not be empty")
    for index, item in enumerate(raw_exec):
        if not isinstance(item, str) or not item.strip():
            raise CommandManifestError(f"exec[{index}] must be a non-empty string")

    directory = manifest_path.parent
    raw_cwd = payload.get("cwd")
    if raw_cwd is None or raw_cwd == "":
        cwd = config.default_workdir
    else:
        if not isinstance(raw_cwd, str):
            raise CommandManifestError("field cwd must be a string")
        candidate = Path(raw_cwd).expanduser()
        if not candidate.is_absolute():
            candidate = directory / candidate
        cwd = candidate.resolve()
        if not cwd.is_dir():
            raise CommandManifestError(f"cwd does not exist: {cwd}")
    if not any(cwd == root or cwd.is_relative_to(root) for root in config.allowed_roots):
        raise CommandManifestError(
            f"cwd must be inside ALLOWED_WORKDIRS, or its artifacts cannot be sent: {cwd}"
        )

    raw_i18n = payload.get("i18n", {})
    if not isinstance(raw_i18n, dict):
        raise CommandManifestError("field i18n must be an object")
    translations: dict[str, dict[str, str]] = {}
    for language, fields in raw_i18n.items():
        if language not in LANGUAGES or not isinstance(fields, dict):
            raise CommandManifestError(f"i18n.{language} must be an object for one of {', '.join(LANGUAGES)}")
        unknown = set(fields) - {"name", "usage", "description"}
        if unknown:
            raise CommandManifestError(f"i18n.{language} has unsupported fields: {', '.join(sorted(unknown))}")
        translations[language] = {
            key: value for key in ("name", "usage", "description") if (value := _optional_str(fields, key))
        }

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
        translations=translations,
    )


def load_direct_commands(
    config: Config,
    *,
    reserved: frozenset[str] | set[str] = frozenset(),
    warn: Callable[[str], None] | None = None,
) -> dict[str, DirectCommand]:
    """Scan every extension directory and return command -> DirectCommand.

    A broken extension is skipped with a warning; gateway startup and the other
    extensions are unaffected.
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
                emit(f"skipped extension command {manifest_path}: {exc}")
                continue
            if command.command in reserved:
                emit(
                    f"skipped extension command {manifest_path}: /{command.command} clashes with a built-in command"
                )
                continue
            if command.command in loaded:
                emit(
                    f"skipped extension command {manifest_path}: /{command.command} "
                    f"is already taken by {loaded[command.command].manifest_path}"
                )
                continue
            loaded[command.command] = command
    return loaded


@dataclass
class DirectCommandRun:
    """Visible state of one command run; GatewayApp's status loop publishes it."""

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
    # Path -> artifact token. The card refreshes in place every 10 seconds; reusing
    # tokens keeps the sessions artifact table (500 entries) from filling up.
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
    """Run direct commands and keep their visible state; GatewayApp's status loop
    publishes progress."""

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
            raise CommandError(tr("There is no extension command named /{command}", command=command_name))
        with self._lock:
            if any(
                run.command.command == command_name
                and run.chat_id == chat_id
                and run.bot_key == bot_key
                and run.status == "running"
                for run in self._runs.values()
            ):
                raise CommandError(tr("/{command} is already running; wait for it or /interrupt it first", command=command_name))
            if self._stopping:
                raise CommandError(tr("The gateway is shutting down"))

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
                run.error_lines.append(tr("Could not start the command: {error}", error=exc))
            raise CommandError(tr("Could not start /{command}: {error}", command=command_name, error=exc)) from exc
        return run

    def _spawn(self, run: DirectCommandRun) -> None:
        command = run.command
        env = os.environ.copy()
        # Line-buffer Python extensions by default, or progress lines stall in the child's buffer.
        env["PYTHONUNBUFFERED"] = "1"
        # The gateway's settings are not necessarily in the extension's environment
        # (systemd only sets the lines .env overrides), yet extensions need values such
        # as the send limit. Pass them explicitly.
        for key in ("TELEGRAM_MAX_FILE_BYTES",):
            if key not in env:
                env[key] = str(getattr(self.config, "telegram_max_file_bytes", ""))
        env.update(
            {
                "TG_COMMAND_ID": command.id,
                "TG_COMMAND": command.command,
                "TG_COMMAND_NAME": command.name,
                "TG_ARGS_JSON": json.dumps(run.args, ensure_ascii=False),
                # The raw text without any tokenizing: cookies, JSON and quotes stay intact.
                "TG_RAW_ARGS": run.raw_args,
                "TG_ARGV0": run.args[0] if run.args else "",
                "TG_CHAT_ID": str(run.chat_id),
                "TG_USER_ID": str(run.user_id),
                "TG_WORKDIR": str(command.cwd),
                "TG_MANIFEST_DIR": str(command.directory),
                "TG_LANGUAGE": current_language(),
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
                # The process ended before the timer was registered; don't keep it.
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
            except (OSError, ValueError):  # the pipe closes first when the process is killed
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
                            tr("Command exit code {code}", code=returncode) + (f"\n{detail}" if detail else "")
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
                tr("Stopped after running longer than {seconds} seconds", seconds=run.command.timeout_seconds) + "\n"
            )
            run.dirty = True
        self._terminate(run, mark_interrupted=True)

    def _terminate(self, run: DirectCommandRun, *, mark_interrupted: bool) -> None:
        """Terminate the process group; settle the state first so the reader thread
        does not report the termination as a command failure."""
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
