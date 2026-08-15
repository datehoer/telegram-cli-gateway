from __future__ import annotations

import json
import socket
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterable

from .formatting import markdown_to_telegram_html, split_markdown, split_rich_markdown


class TelegramError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(body: object) -> float | None:
    if not isinstance(body, dict):
        return None
    parameters = body.get("parameters")
    value = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def split_message(text: str, limit: int = 3800) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = remaining.rfind("\n", 0, limit)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramClient:
    def __init__(self, token: str):
        self._base_url = f"https://api.telegram.org/bot{token}/"
        self._file_base_url = f"https://api.telegram.org/file/bot{token}/"

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        encoded_payload: dict[str, str] = {}
        for key, value in (payload or {}).items():
            encoded_payload[key] = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
        request = urllib.request.Request(
            self._base_url + method,
            data=urllib.parse.urlencode(encoded_payload).encode("utf-8"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
                detail = (
                    error_body.get("description", "HTTP error")
                    if isinstance(error_body, dict)
                    else f"HTTP {exc.code}"
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                error_body = None
                detail = f"HTTP {exc.code}"
            raise TelegramError(
                f"Telegram {method} failed: {detail}",
                retry_after=_retry_after(error_body),
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise TelegramError(f"Telegram {method} connection failed: {reason}") from exc
        except json.JSONDecodeError as exc:
            raise TelegramError(f"Telegram {method} returned invalid JSON") from exc

        if not body.get("ok"):
            raise TelegramError(
                f"Telegram {method} failed: {body.get('description', 'unknown error')}",
                retry_after=_retry_after(body),
            )
        return body.get("result")

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        return result if isinstance(result, list) else []

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> int | None:
        first_message_id: int | None = None
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            result = self._call(
                "sendMessage",
                payload,
            )
            if first_message_id is None and isinstance(result, dict):
                message_id = result.get("message_id")
                if isinstance(message_id, int):
                    first_message_id = message_id
        return first_message_id

    def send_markdown(
        self,
        chat_id: int,
        markdown: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        first_message_id: int | None = None
        chunks = split_markdown(markdown)
        for index, chunk in enumerate(chunks):
            html_text = markdown_to_telegram_html(chunk)
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            try:
                result = self._call("sendMessage", payload)
            except TelegramError:
                payload["text"] = chunk
                payload.pop("parse_mode", None)
                result = self._call("sendMessage", payload)
            if first_message_id is None and isinstance(result, dict):
                message_id = result.get("message_id")
                if isinstance(message_id, int):
                    first_message_id = message_id
        return first_message_id

    def send_rich_markdown(
        self,
        chat_id: int,
        markdown: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        first_message_id: int | None = None
        chunks = split_rich_markdown(markdown)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "rich_message": {"markdown": chunk},
            }
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            result = self._call("sendRichMessage", payload)
            if first_message_id is None and isinstance(result, dict):
                message_id = result.get("message_id")
                if isinstance(message_id, int):
                    first_message_id = message_id
        return first_message_id

    def edit_rich_markdown(
        self,
        chat_id: int,
        message_id: int,
        markdown: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = split_rich_markdown(markdown)
        if not chunks:
            return
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {"markdown": chunks[0]},
        }
        if reply_markup is not None and len(chunks) == 1:
            payload["reply_markup"] = reply_markup
        try:
            self._call("editMessageText", payload)
        except TelegramError as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        for index, chunk in enumerate(chunks[1:], start=1):
            extra: dict[str, Any] = {"chat_id": chat_id, "rich_message": {"markdown": chunk}}
            if reply_markup is not None and index == len(chunks) - 1:
                extra["reply_markup"] = reply_markup
            self._call("sendRichMessage", extra)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        try:
            self._call("editMessageText", payload)
        except TelegramError as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    def edit_markdown(
        self,
        chat_id: int,
        message_id: int,
        markdown: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.edit_message(
                chat_id,
                message_id,
                markdown_to_telegram_html(markdown),
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        except TelegramError:
            self.edit_message(chat_id, message_id, markdown, reply_markup=reply_markup)

    def edit_message_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any],
    ) -> None:
        self._call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": reply_markup,
            },
        )

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        self._call("answerCallbackQuery", payload)

    def get_file(self, file_id: str) -> dict[str, Any]:
        result = self._call("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not isinstance(result.get("file_path"), str):
            raise TelegramError("Telegram getFile did not return a file path")
        return result

    def download_file(self, file_id: str, destination: Path, max_bytes: int) -> int:
        metadata = self.get_file(file_id)
        reported_size = metadata.get("file_size")
        if isinstance(reported_size, int) and reported_size > max_bytes:
            raise TelegramError(f"文件超过大小限制（最大 {max_bytes // 1024 // 1024} MB）")
        request = urllib.request.Request(self._file_base_url + metadata["file_path"], method="GET")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as handle:
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise TelegramError(
                            f"文件超过大小限制（最大 {max_bytes // 1024 // 1024} MB）"
                        )
                    handle.write(chunk)
            os.chmod(temporary, 0o600)
            temporary.replace(destination)
        except TelegramError:
            temporary.unlink(missing_ok=True)
            raise
        except (OSError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            temporary.unlink(missing_ok=True)
            reason = getattr(exc, "reason", exc)
            raise TelegramError(f"Telegram 文件下载失败：{reason}") from exc
        return total

    def send_local_file(self, chat_id: int, path: Path, as_photo: bool = False) -> None:
        method = "sendPhoto" if as_photo else "sendDocument"
        field_name = "photo" if as_photo else "document"
        boundary = f"----telegram-cli-gateway-{uuid.uuid4().hex}"
        file_data = path.read_bytes()
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; "
                f"filename=\"{path.name.replace(chr(34), '_')}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8"),
            file_data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        request = urllib.request.Request(
            self._base_url + method,
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise TelegramError(f"Telegram {method} failed: {reason}") from exc
        except json.JSONDecodeError as exc:
            raise TelegramError(f"Telegram {method} returned invalid JSON") from exc
        if not body.get("ok"):
            raise TelegramError(f"Telegram {method} failed: {body.get('description', 'unknown error')}")

    def send_action(self, chat_id: int, action: str = "typing") -> None:
        self._call("sendChatAction", {"chat_id": chat_id, "action": action})

    def set_commands(self, commands: Iterable[tuple[str, str]]) -> None:
        payload = [{"command": command, "description": description} for command, description in commands]
        self._call("setMyCommands", {"commands": payload})
