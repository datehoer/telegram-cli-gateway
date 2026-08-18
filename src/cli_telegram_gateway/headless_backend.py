from __future__ import annotations

import json
import logging
import subprocess
import threading
import uuid
from collections.abc import Callable
from typing import Any

from .attachments import Attachment
from .sessions import CliSession


LOGGER = logging.getLogger("telegram-cli-gateway.headless")


class HeadlessBackendError(RuntimeError):
    pass


EventHandler = Callable[[str, str, str, Any], None]


class HeadlessBackend:
    """Runs Claude, Grok and Pi in their JSON-producing non-interactive modes."""

    def __init__(self, commands: dict[str, tuple[str, ...]], on_event: EventHandler) -> None:
        self.commands = commands
        self.on_event = on_event
        self._lock = threading.RLock()
        self._active: dict[str, subprocess.Popen[str]] = {}

    def is_active(self, session_id: str) -> bool:
        with self._lock:
            process = self._active.get(session_id)
            return bool(process and process.poll() is None)

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
        with self._lock:
            if self.is_active(session.session_id):
                raise HeadlessBackendError("the previous task is still running")
            command = self._build_command(session, text, attachments)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=session.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                raise HeadlessBackendError(f"could not start {session.cli}: {exc}") from exc
            self._active[session.session_id] = process

        turn_id = str(uuid.uuid4())
        threading.Thread(
            target=self._read_process,
            args=(session, turn_id, process),
            name=f"{session.cli}-{session.session_id}",
            daemon=True,
        ).start()
        return turn_id

    def compact_session(self, session: CliSession) -> str:
        """通过一次性 RPC 进程触发 Pi 的原生手动压缩（compact 命令），返回结果摘要。

        Pi 的 RPC 模式读 stdin 上的 JSON 命令并输出 JSON 事件；发完 compact 后
        进程会自己退出。这里用超时兜底，避免意外挂住。
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
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                raise HeadlessBackendError(f"could not start {session.cli}: {exc}") from exc

        compact_request = json.dumps({"id": "compact", "type": "compact"}, ensure_ascii=False)
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(compact_request + "\n")
            process.stdin.flush()
            # 保持 stdin 打开：pi 在 stdin 关闭时触发 shutdown，会中止进行中的压缩。
            # 等 compaction_end / compact 响应出现后再退出。
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
                value_type = value.get("type")
                if value_type == "compaction_end":
                    if value.get("errorMessage"):
                        return f"压缩未执行：{value['errorMessage']}"
                    if value.get("aborted"):
                        return "压缩已中止。"
                    summary = value.get("result", {}).get("summary") if isinstance(value.get("result"), dict) else None
                    if summary:
                        result_text = str(summary)[:600]
                    else:
                        return "已触发上下文压缩。"
                elif value_type == "response" and value.get("id") == "compact":
                    if not value.get("success"):
                        return f"压缩未执行：{value.get('error') or 'compaction failed'}"
                    if result_text:
                        return f"压缩完成。摘要：{result_text}"
                    return "已触发上下文压缩。"
            return_code = process.wait(timeout=5)
            raise HeadlessBackendError(
                f"{session.cli} RPC 未返回 compact 结果"
                f"（exit {return_code}）"
            )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()

    def _attachment_prompt(self, text: str, attachments: tuple[Attachment, ...]) -> str:
        if not attachments:
            return text
        listed = "\n".join(f"- {item.name}: {item.path}" for item in attachments)
        return f"{text}\n\n用户同时上传了以下本地文件，请按请求读取或查看：\n{listed}"

    def _build_command(
        self, session: CliSession, text: str, attachments: tuple[Attachment, ...]
    ) -> list[str]:
        base = list(self.commands[session.cli])
        first_turn = session.turn_count == 0
        external_id = session.external_id or ""
        model_args = ["--model", session.model] if session.model else []
        if session.cli == "claude":
            prompt = self._attachment_prompt(text, attachments)
            session_args = ["--session-id", external_id] if first_turn else ["--resume", external_id]
            effort_args = ["--effort", session.effort] if session.effort else []
            return [
                *base,
                *model_args,
                *effort_args,
                "-p",
                prompt,
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
        file_args = [f"@{item.path}" for item in attachments]
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
            text,
        ]

    def _read_process(
        self, session: CliSession, turn_id: str, process: subprocess.Popen[str]
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
        emitted_text = False
        tool_json_parts: dict[int, list[str]] = {}
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
                    events = self._parse_event(session.cli, value, tool_json_parts, emitted_text)
                    for kind, data in events:
                        if kind == "delta" and data:
                            emitted_text = True
                        if kind == "retrying":
                            # Pi may retry a provider failure internally. Its preceding
                            # message_end is not terminal when agent_end says willRetry.
                            terminal_result = None
                        elif kind in {"completed", "error", "interrupted"}:
                            priority = {"completed": 1, "interrupted": 2, "error": 3}
                            if terminal_result is None or priority[kind] >= priority[terminal_result[0]]:
                                terminal_result = (kind, data)
                        else:
                            self.on_event(session.session_id, turn_id, kind, data)
            return_code = process.wait()
            stderr_thread.join(timeout=2)
            with self._lock:
                if self._active.get(session.session_id) is process:
                    self._active.pop(session.session_id, None)
            if terminal_result:
                # Structured terminal state is authoritative. In particular, Pi JSON
                # mode can exit 0 after an assistant message with stopReason=error.
                self.on_event(session.session_id, turn_id, *terminal_result)
            elif return_code == 0:
                self.on_event(session.session_id, turn_id, "completed", None)
            else:
                # 非零退出且没有明确的 error 事件：可能是被信号杀死（网关重启/关机
                # 连累、OOM）或 CLI 异常退出。CLI 的退出码约定不统一（例如 pi 把
                # SIGTERM 转成 143 而不是负值），所以不按符号判断，统一视为可恢复
                # 的中断：不清 in_flight，留给恢复机制决定是否续跑。
                self.on_event(session.session_id, turn_id, "interrupted", return_code)
        except Exception as exc:
            LOGGER.exception("failed while reading %s JSON stream", session.cli)
            self.on_event(session.session_id, turn_id, "error", str(exc))
        finally:
            with self._lock:
                if self._active.get(session.session_id) is process:
                    self._active.pop(session.session_id, None)
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()

    def _parse_event(
        self,
        cli: str,
        value: dict[str, Any],
        tool_json_parts: dict[int, list[str]],
        emitted_text: bool,
    ) -> list[tuple[str, Any]]:
        if cli in {"claude", "grok"}:
            return self._parse_anthropic_event(value, tool_json_parts, emitted_text)
        return self._parse_pi_event(value, emitted_text)

    def _parse_anthropic_event(
        self,
        value: dict[str, Any],
        tool_json_parts: dict[int, list[str]],
        emitted_text: bool,
    ) -> list[tuple[str, Any]]:
        output: list[tuple[str, Any]] = []
        value_type = value.get("type")
        if value_type == "stream_event":
            event = value.get("event")
            if not isinstance(event, dict):
                return output
            event_type = event.get("type")
            index = event.get("index") if isinstance(event.get("index"), int) else 0
            if event_type == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str):
                        output.append(("delta", text))
                elif isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                    partial = delta.get("partial_json")
                    if isinstance(partial, str):
                        tool_json_parts.setdefault(index, []).append(partial)
                        command = self._command_from_partial_json("".join(tool_json_parts[index]))
                        if command:
                            output.append(("command", command))
            elif event_type == "content_block_start":
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
                output.append(("error", str(value.get("result") or value.get("error") or "CLI task failed")))
            else:
                result = value.get("result")
                if not emitted_text and isinstance(result, str) and result:
                    output.append(("delta", result))
                output.append(("completed", None))
        return output

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

    def _command_from_partial_json(self, raw: str) -> str | None:
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
        if not process or process.poll() is not None:
            return False
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return True

    def close(self) -> None:
        with self._lock:
            session_ids = list(self._active)
        for session_id in session_ids:
            self.interrupt(session_id)
