from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from collections.abc import Callable
from typing import Any

from .attachments import Attachment


LOGGER = logging.getLogger("telegram-cli-gateway.codex")


class CodexBackendError(RuntimeError):
    pass


NotificationHandler = Callable[[str, dict[str, Any]], None]
ServerRequestHandler = Callable[[str | int, str, dict[str, Any]], None]


class CodexAppServer:
    """Small JSONL/JSON-RPC client for `codex app-server --stdio`."""

    def __init__(
        self,
        command: tuple[str, ...],
        on_notification: NotificationHandler,
        on_server_request: ServerRequestHandler,
    ) -> None:
        self.command = command
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._next_id = 1
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._closing = threading.Event()

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        self._closing.clear()
        try:
            self.process = subprocess.Popen(
                [*self.command, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise CodexBackendError(f"could not start codex app-server: {exc}") from exc

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="codex-app-server-reader",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name="codex-app-server-stderr",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()

        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "telegram-cli-gateway",
                    "title": "Telegram CLI Gateway",
                    "version": "0.1.0",
                }
            },
        )
        self.notify("initialized", {})

    def close(self) -> None:
        process = self.process
        if not process:
            return
        self._closing.set()
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self._reader_thread:
            self._reader_thread.join(timeout=2)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()
        self.process = None

    def begin_shutdown(self) -> None:
        """Mark an expected service shutdown before systemd signals child processes."""
        self._closing.set()

    def _send_message(self, message: dict[str, Any]) -> None:
        process = self.process
        if not process or process.poll() is not None or not process.stdin:
            raise CodexBackendError("codex app-server is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise CodexBackendError("codex app-server connection closed") from exc

    def request(self, method: str, params: dict[str, Any], timeout: float = 30) -> Any:
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        try:
            self._send_message({"id": request_id, "method": method, "params": params})
            try:
                response = response_queue.get(timeout=timeout)
            except queue.Empty as exc:
                raise CodexBackendError(f"codex app-server request timed out: {method}") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            detail = error.get("message", "unknown error") if isinstance(error, dict) else str(error)
            raise CodexBackendError(f"codex {method} failed: {detail}")
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send_message({"method": method, "params": params})

    def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        self._send_message({"id": request_id, "result": result})

    def _reader_loop(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                LOGGER.warning("ignored malformed line from codex app-server")
                continue
            request_id = message.get("id")
            method = message.get("method")
            if request_id is not None and isinstance(method, str):
                params = message.get("params")
                self.on_server_request(
                    request_id,
                    method,
                    params if isinstance(params, dict) else {},
                )
                continue
            if request_id is not None:
                with self._pending_lock:
                    response_queue = self._pending.get(request_id)
                if response_queue:
                    response_queue.put(message)
                continue
            if isinstance(method, str):
                params = message.get("params")
                self.on_notification(method, params if isinstance(params, dict) else {})
        if not self._closing.is_set():
            LOGGER.warning("codex app-server stdout closed unexpectedly")

    def _stderr_loop(self) -> None:
        process = self.process
        if not process or not process.stderr:
            return
        for line in process.stderr:
            line = line.strip()
            if line:
                LOGGER.debug("app-server: %s", line)

    def start_thread(
        self, cwd: str, ephemeral: bool = False, model: str | None = None
    ) -> str:
        params: dict[str, Any] = {
            "cwd": cwd,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "ephemeral": ephemeral,
        }
        if model:
            params["model"] = model
        result = self.request("thread/start", params)
        try:
            return str(result["thread"]["id"])
        except (KeyError, TypeError) as exc:
            raise CodexBackendError("thread/start response did not include a thread ID") from exc

    def resume_thread(self, thread_id: str, model: str | None = None) -> None:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if model:
            params["model"] = model
        self.request("thread/resume", params)

    def start_turn(
        self,
        thread_id: str,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        model: str | None = None,
        effort: str | None = None,
    ) -> str:
        inputs = self._turn_inputs(text, attachments)
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": inputs,
        }
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        result = self.request("turn/start", params)
        try:
            return str(result["turn"]["id"])
        except (KeyError, TypeError) as exc:
            raise CodexBackendError("turn/start response did not include a turn ID") from exc

    @staticmethod
    def _turn_inputs(
        text: str, attachments: tuple[Attachment, ...] = ()
    ) -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = [{"type": "text", "text": text}]
        documents: list[str] = []
        for attachment in attachments:
            if attachment.is_image:
                inputs.append({"type": "localImage", "path": str(attachment.path)})
            else:
                documents.append(f"- {attachment.name}: {attachment.path}")
        if documents:
            inputs.append(
                {
                    "type": "text",
                    "text": "用户同时上传了以下本地文件，请按请求读取它们：\n" + "\n".join(documents),
                }
            )
        return inputs

    def steer_turn(
        self,
        thread_id: str,
        turn_id: str,
        text: str,
        attachments: tuple[Attachment, ...] = (),
    ) -> str:
        result = self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": self._turn_inputs(text, attachments),
            },
        )
        try:
            returned_turn_id = result.get("turnId") or result["turn"]["id"]
            return str(returned_turn_id)
        except (KeyError, TypeError) as exc:
            raise CodexBackendError("turn/steer response did not include a turn ID") from exc

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def compact_thread(self, thread_id: str) -> None:
        """手动压缩一个线程的上下文（原生 thread/compact/start）。"""
        self.request("thread/compact/start", {"threadId": thread_id})

    def archive_thread(self, thread_id: str) -> None:
        """归档一个线程，从活跃列表里收起（/clear 开新对话时归档旧线程）。"""
        self.request("thread/archive", {"threadId": thread_id})
