from __future__ import annotations

import json
import os
import signal
import logging
import selectors
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from .attachments import Attachment
from .i18n import tr
from .sessions import CliSession
from .usage import claude_context_window, claude_quota_lines, headless_usage


LOGGER = logging.getLogger("telegram-cli-gateway.headless")

# Pi attaches an @file as an image only for these types; any other @file is inlined as UTF-8 text.
PI_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


class HeadlessBackendError(RuntimeError):
    pass


EventHandler = Callable[[str, str, str, Any], None]


class _TurnInput:
    """The stream-json stdin of one Claude process.

    Every user message carries a UUID, and Claude reports its command_lifecycle:
    queued when read, started when a turn takes it, then a final state. Claude reads
    a message sent mid-turn at the next tool boundary, or answers it in a follow-up
    turn of the same process. Input stays open until a result leaves none of our
    messages waiting. Claude still finishes everything it has read after stdin
    closes, so closing never drops a written message.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._closed = False
        # None until the init event says whether lifecycle events are reported.
        self._tracked: bool | None = None
        self._idle = False
        self._waiting: set[str] = set()
        self._accepted: set[str] = set()
        self._changed = threading.Condition(self._lock)

    def send(self, text: str, *, steer: bool = False) -> str | None:
        """Write one user message; return its ID, or None when input is closed."""
        message_id = str(uuid.uuid4())
        line = json.dumps({
            "type": "user",
            "uuid": message_id,
            "message": {"role": "user", "content": text},
        }, ensure_ascii=False)
        with self._lock:
            # Without lifecycle events a steer could never be confirmed.
            if self._closed or (steer and self._tracked is False):
                return None
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                # A broken pipe means Claude exited and never read the message.
                self._closed = True
                return None
            self._waiting.add(message_id)
        return message_id

    def wait_accepted(self, message_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._changed:
            while message_id not in self._accepted and message_id in self._waiting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            return message_id in self._accepted

    def on_init(self, capabilities: Any) -> None:
        with self._lock:
            self._tracked = isinstance(capabilities, list) and "msg_lifecycle_v1" in capabilities

    def on_lifecycle(self, message_id: str, state: str) -> None:
        with self._changed:
            self._accepted.add(message_id)
            if state != "queued":
                self._waiting.discard(message_id)
            if state == "started":
                self._idle = False
            self._changed.notify_all()
            stream = self._close_if_idle_locked()
        self._close_stream(stream)

    def on_result(self) -> None:
        with self._lock:
            self._idle = True
            stream = self._close_if_idle_locked()
        self._close_stream(stream)

    def close(self) -> None:
        with self._lock:
            stream = self._close_locked()
        self._close_stream(stream)

    def _close_if_idle_locked(self) -> Any:
        # An early result (Claude reporting background tasks orphaned by the
        # previous process) may arrive before our message starts.
        if not self._idle or (self._tracked and self._waiting):
            return None
        return self._close_locked()

    def _close_locked(self) -> Any:
        self._closed = True
        stream, self._stream = self._stream, None
        self._waiting.clear()
        self._changed.notify_all()
        return stream

    @staticmethod
    def _close_stream(stream: Any) -> None:
        if stream is None:
            return
        try:
            stream.close()
        except (OSError, ValueError):
            pass


class HeadlessBackend:
    """Runs Claude, Grok and Pi in their JSON-producing non-interactive modes."""

    def __init__(self, commands: dict[str, tuple[str, ...]], on_event: EventHandler) -> None:
        self.commands = commands
        self.on_event = on_event
        self._lock = threading.RLock()
        self._active: dict[str, subprocess.Popen[str]] = {}
        self._inputs: dict[str, _TurnInput] = {}
        self._interrupted: set[subprocess.Popen[str]] = set()

    def is_active(self, session_id: str) -> bool:
        with self._lock:
            process = self._active.get(session_id)
            return bool(process and process.poll() is None)

    def read_claude_diagnostics(
        self, session: CliSession, *, model: str | None = None,
        include_quota: bool = False, timeout: float = 8,
    ) -> dict[str, Any]:
        """Query native SDK controls in an ephemeral process; send no user turn."""
        if session.cli != "claude":
            raise HeadlessBackendError("Claude diagnostics require a Claude session")
        command = [*self.commands["claude"]]
        if model:
            command.extend(["--model", model])
        command.extend([
            "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", '{"disableAllHooks":true}',
        ])
        try:
            process = subprocess.Popen(
                command, cwd=session.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0,
            )
        except OSError as exc:
            raise HeadlessBackendError(tr("Could not start the native Claude status query")) from exc
        selector = selectors.DefaultSelector()
        assert process.stdin is not None and process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        buffer = b""
        bytes_read = 0

        def request(subtype: str, **options: Any) -> dict[str, Any]:
            nonlocal buffer, bytes_read
            if time.monotonic() >= deadline:
                raise HeadlessBackendError(tr("The native Claude status query timed out"))
            request_id = str(uuid.uuid4())
            process.stdin.write(json.dumps({
                "type": "control_request", "request_id": request_id,
                "request": {"subtype": subtype, **options},
            }).encode() + b"\n")
            process.stdin.flush()
            while True:
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeDecodeError, RecursionError):
                        continue
                    if not isinstance(value, dict) or value.get("type") != "control_response":
                        continue
                    response = value.get("response")
                    if not isinstance(response, dict) or response.get("request_id") != request_id:
                        continue
                    payload = response.get("response")
                    if response.get("subtype") != "success" or not isinstance(payload, dict):
                        raise HeadlessBackendError(tr("The native Claude status query returned no valid data"))
                    return payload
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise HeadlessBackendError(tr("The native Claude status query timed out"))
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise HeadlessBackendError(tr("The native Claude status query exited"))
                bytes_read += len(chunk)
                if bytes_read > 2 * 1024 * 1024:
                    raise HeadlessBackendError(tr("The native Claude status response exceeded the size limit"))
                buffer += chunk

        try:
            request("initialize")
            result: dict[str, Any] = {}
            try:
                result["context"] = claude_context_window(request("get_context_usage", detail="summary"))
            except (HeadlessBackendError, OSError):
                result["context"] = {}
            if include_quota:
                try:
                    # Wire protocol uses snake_case; the SDK's skipBehaviors
                    # option maps to this and avoids scanning unrelated history.
                    result["quota_lines"] = claude_quota_lines(request("get_usage", skip_behaviors=True))
                except (HeadlessBackendError, OSError):
                    result["quota_lines"] = [tr("Account quota: temporarily unavailable; try again later.")]
            return result
        except OSError as exc:
            raise HeadlessBackendError(tr("Could not connect to the native Claude status query")) from exc
        finally:
            self._terminate_process(process)
            selector.close()
            process.stdin.close()
            process.stdout.close()

    def start_turn(
        self,
        session: CliSession,
        text: str,
        attachments: tuple[Attachment, ...] = (),
    ) -> str:
        if session.cli not in {"claude", "grok", "pi"}:
            raise HeadlessBackendError(f"unsupported headless CLI: {session.cli}")
        if not session.external_id:
            raise HeadlessBackendError("headless session has no external session ID")
        streams_input = session.cli == "claude"
        with self._lock:
            if self.is_active(session.session_id):
                raise HeadlessBackendError("the previous task is still running")
            command = self._build_command(session, text, attachments)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=session.cwd,
                    stdin=subprocess.PIPE if streams_input else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
            except OSError as exc:
                raise HeadlessBackendError(f"could not start {session.cli}: {exc}") from exc
            self._active[session.session_id] = process
            turn_input = _TurnInput(process.stdin) if streams_input else None
            if turn_input is not None:
                self._inputs[session.session_id] = turn_input

        # No steer can precede the prompt: the caller registers the turn only
        # after this returns. A failed write means Claude exited; the reader
        # reports that exit.
        if turn_input is not None and turn_input.send(self._attachment_prompt(text, attachments)) is None:
            LOGGER.warning("%s exited before reading its prompt", session.cli)
        turn_id = str(uuid.uuid4())
        threading.Thread(
            target=self._read_process,
            args=(session, turn_id, process, turn_input),
            name=f"{session.cli}-{session.session_id}",
            daemon=True,
        ).start()
        return turn_id

    def steer_turn(
        self,
        session_id: str,
        text: str,
        attachments: tuple[Attachment, ...] = (),
    ) -> Callable[[float], bool] | None:
        """Add a message to the session's running Claude process.

        Returns a function that waits up to the given seconds for Claude to confirm
        it queued the message, or None when the process takes no more input; the
        caller then queues the message as a new turn.
        """
        with self._lock:
            turn_input = self._inputs.get(session_id)
        if turn_input is None:
            return None
        message_id = turn_input.send(self._attachment_prompt(text, attachments), steer=True)
        if message_id is None:
            return None
        return lambda timeout: turn_input.wait_accepted(message_id, timeout)

    def compact_session(self, session: CliSession, timeout: float = 120) -> str:
        """Run Pi's native manual compaction through a one-shot RPC process.

        Pi's RPC mode reads JSON commands on stdin and writes JSON events; after a
        compact command the process exits by itself. The timeout guards against hangs.
        Returns a short result summary.
        """
        if session.cli != "pi":
            raise HeadlessBackendError("compact is only supported for pi")
        if not session.external_id:
            raise HeadlessBackendError("headless session has no external session ID")
        with self._lock:
            if self.is_active(session.session_id):
                raise HeadlessBackendError("the previous task is still running")
            command = [
                *self.commands["pi"],
                "--print",
                "--mode",
                "rpc",
                "--approve",
                "--session",
                session.external_id,
            ]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=session.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
            except OSError as exc:
                raise HeadlessBackendError(f"could not start {session.cli}: {exc}") from exc
            self._active[session.session_id] = process

        compact_request = json.dumps({"id": "compact", "type": "compact"}, ensure_ascii=False)
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            self._terminate_process(process)

        watchdog = threading.Timer(timeout, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(compact_request + "\n")
            process.stdin.flush()
            # Keep stdin open: Pi shuts down when stdin closes, which aborts a running
            # compaction. Leave only after compaction_end or the compact response.
            result_text = ""
            while True:
                raw_line = process.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, dict):
                    continue
                value_type = value.get("type")
                if value_type == "compaction_end":
                    if value.get("errorMessage"):
                        return tr("Compaction did not run: {reason}", reason=value["errorMessage"])
                    if value.get("aborted"):
                        return tr("Compaction aborted.")
                    self.on_event(session.session_id, "compact", "usage", {
                        "context_tokens": None, "external_id": session.external_id,
                    })
                    summary = value.get("result", {}).get("summary") if isinstance(value.get("result"), dict) else None
                    if summary:
                        result_text = str(summary)[:600]
                    else:
                        return tr("Context compaction started.")
                elif value_type == "response" and value.get("id") == "compact":
                    if not value.get("success"):
                        return tr("Compaction did not run: {reason}", reason=value.get("error") or "compaction failed")
                    self.on_event(session.session_id, "compact", "usage", {
                        "context_tokens": None, "external_id": session.external_id,
                    })
                    if result_text:
                        return tr("Compaction finished. Summary: {summary}", summary=result_text)
                    return tr("Context compaction started.")
            return_code = process.wait(timeout=5)
            if timed_out.is_set():
                raise HeadlessBackendError(tr("Pi context compaction timed out; the compaction process was stopped"))
            raise HeadlessBackendError(
                tr("{cli} RPC returned no compact result (exit {code})", cli=session.cli, code=return_code)
            )
        finally:
            watchdog.cancel()
            self._terminate_process(process)
            watchdog.join(timeout=6)
            with self._lock:
                if self._active.get(session.session_id) is process:
                    self._active.pop(session.session_id, None)
                self._interrupted.discard(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()

    def _attachment_prompt(self, text: str, attachments: tuple[Attachment, ...]) -> str:
        if not attachments:
            return text
        listed = "\n".join(f"- {item.name}: {item.path}" for item in attachments)
        return f"{text}\n\nThe user also uploaded these local files; read or view them as the request needs:\n{listed}"

    def _build_command(
        self, session: CliSession, text: str, attachments: tuple[Attachment, ...]
    ) -> list[str]:
        base = list(self.commands[session.cli])
        first_turn = session.turn_count == 0
        external_id = session.external_id or ""
        model_args = ["--model", session.model] if session.model else []
        if session.cli == "claude":
            # The prompt goes to stdin, which stays open for messages sent mid-turn.
            session_args = ["--session-id", external_id] if first_turn else ["--resume", external_id]
            effort_args = ["--effort", session.effort] if session.effort else []
            return [
                *base,
                *model_args,
                *effort_args,
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--dangerously-skip-permissions",
                *session_args,
            ]
        if session.cli == "grok":
            prompt = self._attachment_prompt(text, attachments)
            session_args = ["--session-id", external_id] if first_turn else ["--resume", external_id]
            effort_args = ["--reasoning-effort", session.effort] if session.effort else []
            return [
                *base,
                *model_args,
                *effort_args,
                "--single",
                prompt,
                "--output-format",
                "streaming-messages-json",
                "--include-partial-messages",
                "--always-approve",
                "--permission-mode",
                "bypassPermissions",
                "--cwd",
                session.cwd,
                *session_args,
            ]
        session_args = ["--session-id", external_id] if first_turn else ["--session", external_id]
        # Inlined audio, video or archives would flood the context, so like Claude and Grok they get paths.
        images = tuple(item for item in attachments if item.mime_type in PI_IMAGE_TYPES)
        others = tuple(item for item in attachments if item.mime_type not in PI_IMAGE_TYPES)
        file_args = [f"@{item.path}" for item in images]
        effort_args = ["--thinking", session.effort] if session.effort else []
        return [
            *base,
            "--print",
            "--mode",
            "json",
            "--approve",
            *model_args,
            *effort_args,
            *session_args,
            *file_args,
            self._attachment_prompt(text, others),
        ]

    def _read_process(
        self,
        session: CliSession,
        turn_id: str,
        process: subprocess.Popen[str],
        turn_input: _TurnInput | None = None,
    ) -> None:
        stderr_parts: list[str] = []

        def read_stderr() -> None:
            if not process.stderr:
                return
            for line in process.stderr:
                stderr_parts.append(line)
                if sum(map(len, stderr_parts)) > 8000:
                    del stderr_parts[:-20]

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        terminal_result: tuple[str, Any] | None = None
        # One Claude process answers each steer that arrived after its last tool
        # call in a follow-up turn, so every result names a final message. Hold
        # them until exit, when the turn's narration is known in full.
        final_messages: list[Any] = []
        emitted_text = False
        tool_json_parts: dict[int, list[str]] = {}
        message: dict[str, str] = {}

        def emit_final_messages() -> None:
            for data in final_messages:
                self.on_event(session.session_id, turn_id, "message_completed", data)
            final_messages.clear()

        try:
            if process.stdout:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        LOGGER.debug("ignored non-JSON output from %s", session.cli)
                        continue
                    if not isinstance(value, dict):
                        continue
                    events = self._parse_event(session.cli, value, tool_json_parts, emitted_text, message)
                    for kind, data in events:
                        if (kind == "delta" and data) or (kind == "message_delta" and data["delta"]):
                            emitted_text = True
                        if kind in {"input_capabilities", "input_lifecycle"}:
                            if turn_input is not None and kind == "input_capabilities":
                                turn_input.on_init(data)
                            elif turn_input is not None:
                                turn_input.on_lifecycle(*data)
                                if data[1] == "started":
                                    # A new turn began, so an earlier result (the no-op for
                                    # orphaned background tasks, or the answer before a
                                    # follow-up) must not report a later kill as completed.
                                    terminal_result = None
                            continue
                        if kind == "message_completed":
                            final_messages.append(data)
                            continue
                        if kind == "retrying":
                            # Pi may retry a provider failure internally. Its preceding
                            # message_end is not terminal when agent_end says willRetry.
                            terminal_result = None
                        elif kind in {"completed", "error", "interrupted"}:
                            priority = {"completed": 1, "interrupted": 2, "error": 3}
                            if terminal_result is None or priority[kind] >= priority[terminal_result[0]]:
                                terminal_result = (kind, data)
                            if turn_input is not None:
                                turn_input.on_result()
                        else:
                            if kind == "usage":
                                data = {**data, "external_id": session.external_id}
                            self.on_event(session.session_id, turn_id, kind, data)
            return_code = process.wait()
            stderr_thread.join(timeout=2)
            with self._lock:
                interrupted = process in self._interrupted
                self._interrupted.discard(process)
                if self._active.get(session.session_id) is process:
                    self._active.pop(session.session_id, None)
            emit_final_messages()
            if interrupted:
                self.on_event(session.session_id, turn_id, "interrupted", return_code)
            elif terminal_result:
                # Structured terminal state is authoritative. In particular, Pi JSON
                # mode can exit 0 after an assistant message with stopReason=error.
                self.on_event(session.session_id, turn_id, *terminal_result)
            elif return_code == 0:
                self.on_event(session.session_id, turn_id, "completed", None)
            else:
                # A nonzero exit without an error event: killed by a signal (gateway
                # restart or shutdown, OOM) or an abnormal CLI exit. Exit-code conventions
                # differ (Pi maps SIGTERM to 143, not a negative value), so the sign is not
                # trusted. Treat it as a recoverable interruption: keep in_flight and let
                # recovery decide whether to continue.
                self.on_event(session.session_id, turn_id, "interrupted", return_code)
        except Exception as exc:
            LOGGER.exception("failed while reading %s JSON stream", session.cli)
            emit_final_messages()
            self.on_event(session.session_id, turn_id, "error", str(exc))
        finally:
            with self._lock:
                self._interrupted.discard(process)
                if self._active.get(session.session_id) is process:
                    self._active.pop(session.session_id, None)
                if turn_input is not None and self._inputs.get(session.session_id) is turn_input:
                    self._inputs.pop(session.session_id, None)
            if turn_input is not None:
                turn_input.close()
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()

    def _parse_event(
        self,
        cli: str,
        value: dict[str, Any],
        tool_json_parts: dict[int, list[str]],
        emitted_text: bool,
        message: dict[str, str] | None = None,
    ) -> list[tuple[str, Any]]:
        usage = headless_usage(cli, value)
        events: list[tuple[str, Any]] = [("usage", usage)] if usage else []
        if cli in {"claude", "grok"}:
            # Only Claude's result has been verified to repeat just its last message.
            tracked = message if cli == "claude" else None
            return events + self._parse_anthropic_event(value, tool_json_parts, emitted_text, tracked)
        return events + self._parse_pi_event(value, emitted_text)

    def _parse_anthropic_event(
        self,
        value: dict[str, Any],
        tool_json_parts: dict[int, list[str]],
        emitted_text: bool,
        message: dict[str, str] | None = None,
    ) -> list[tuple[str, Any]]:
        output: list[tuple[str, Any]] = []
        value_type = value.get("type")
        if value_type == "system" and value.get("subtype") == "init":
            return [("input_capabilities", value.get("capabilities"))]
        if value_type == "command_lifecycle":
            command_uuid, state = value.get("command_uuid"), value.get("state")
            if isinstance(command_uuid, str) and isinstance(state, str):
                return [("input_lifecycle", (command_uuid, state))]
            return output
        if value_type == "stream_event":
            event = value.get("event")
            if not isinstance(event, dict):
                return output
            event_type = event.get("type")
            index = event.get("index") if isinstance(event.get("index"), int) else 0
            if event_type == "message_start" and message is not None:
                started = event.get("message")
                if isinstance(started, dict) and isinstance(started.get("id"), str):
                    message["id"] = started["id"]
            elif event_type == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str):
                        if message and message.get("id"):
                            output.append(("message_delta", {"id": message["id"], "delta": text}))
                        else:
                            output.append(("delta", text))
                elif isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                    partial = delta.get("partial_json")
                    if isinstance(partial, str):
                        # Tool input is valid JSON only once its block closes. Parsing the
                        # growing buffer on every fragment cost quadratic CPU on this reader
                        # thread (about 2 s for a 256 KiB Write).
                        tool_json_parts.setdefault(index, []).append(partial)
            elif event_type == "content_block_stop":
                parts = tool_json_parts.pop(index, None)
                if parts:
                    command = self._command_from_tool_json("".join(parts))
                    if command:
                        output.append(("command", command))
            elif event_type == "content_block_start":
                # Block indices restart for every assistant message/tool call.
                tool_json_parts.pop(index, None)
                block = event.get("content_block")
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name") or "tool")
                    tool_input = block.get("input")
                    output.append(("command", self._format_tool(name, tool_input)))
            return output
        if value_type == "assistant":
            message = value.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        output.append(
                            ("command", self._format_tool(str(block.get("name") or "tool"), block.get("input")))
                        )
            return output
        if value_type == "result":
            if value.get("is_error"):
                output.append(("error", self._anthropic_error_detail(value)))
            else:
                result = value.get("result")
                if message and message.get("id") and isinstance(result, str) and result:
                    # The result repeats only the last message: that is the final answer,
                    # and every earlier message was narration around tool calls.
                    output.append(("message_completed", {
                        "id": message["id"], "phase": "final_answer", "text": result,
                    }))
                elif not emitted_text and isinstance(result, str) and result:
                    output.append(("delta", result))
                output.append(("completed", None))
        return output

    @staticmethod
    def _anthropic_error_detail(value: dict[str, Any]) -> str:
        for key in ("result", "error"):
            detail = value.get(key)
            if isinstance(detail, str) and detail.strip():
                return detail
            if detail:
                return HeadlessBackend._stringify(detail)

        # Grok's Messages-compatible terminal result carries execution errors
        # in errors[] rather than result/error.
        errors = value.get("errors")
        if isinstance(errors, list):
            details = [item.strip() for item in errors if isinstance(item, str) and item.strip()]
            if details:
                return "\n".join(details)

        return "CLI task failed"

    def _parse_pi_event(
        self, value: dict[str, Any], emitted_text: bool = False
    ) -> list[tuple[str, Any]]:
        output: list[tuple[str, Any]] = []
        value_type = value.get("type")
        if value_type == "message_update":
            event = value.get("assistantMessageEvent")
            if isinstance(event, dict):
                event_type = event.get("type")
                delta = event.get("delta")
                if event_type == "text_delta" and isinstance(delta, str):
                    return [("delta", delta)]
                if event_type == "thinking_delta" and isinstance(delta, str):
                    return [("thinking", delta)]
        if value_type == "message_end":
            message = value.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return output
            if not emitted_text:
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text")
                            if isinstance(text, str) and text:
                                output.append(("delta", text))
            stop_reason = message.get("stopReason")
            if stop_reason == "stop":
                output.append(("completed", None))
            elif stop_reason == "error":
                output.append(("error", str(message.get("errorMessage") or "Pi task failed")))
            elif stop_reason == "length":
                output.append(("error", "Pi stopped before completing the answer because the model output limit was reached"))
            elif stop_reason == "aborted":
                output.append(("interrupted", None))
            return output
        if value_type == "tool_execution_start":
            return [("command", self._format_tool(str(value.get("toolName") or "tool"), value.get("args")))]
        if value_type == "tool_execution_update":
            result = value.get("partialResult")
            if result is not None:
                return [("command_output", self._stringify(result))]
        if value_type == "tool_execution_end":
            result = value.get("result")
            if result is not None:
                return [("command_output", self._stringify(result))]
        if value_type == "agent_end":
            if value.get("willRetry") is True:
                return [("retrying", None)]
            return [("completed", None)]
        return []

    def _command_from_tool_json(self, raw: str) -> str | None:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        for key in ("command", "cmd", "path", "query"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return self._stringify(value)

    def _format_tool(self, name: str, value: Any) -> str:
        if isinstance(value, dict):
            for key in ("command", "cmd"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
        rendered = self._stringify(value)
        return f"{name} {rendered}".strip()

    @staticmethod
    def _stringify(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            return str(value)

    def interrupt(self, session_id: str) -> bool:
        with self._lock:
            process = self._active.get(session_id)
            if process is not None:
                self._interrupted.add(process)
        if process is None:
            return False
        self._terminate_process(process)
        return True

    @staticmethod
    def _terminate_process(process: subprocess.Popen[Any]) -> None:
        # All processes created here own a fresh process group. Signal that
        # group so tools inheriting stdout cannot keep the reader alive forever.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)

    def close(self) -> None:
        with self._lock:
            session_ids = list(self._active)
        for session_id in session_ids:
            self.interrupt(session_id)
