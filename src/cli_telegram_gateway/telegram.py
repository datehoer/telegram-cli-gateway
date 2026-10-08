from __future__ import annotations

import json
import math
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable

from .formatting import markdown_to_telegram_html, split_markdown, split_rich_markdown
from .i18n import tr
from .telegram_metrics import TelegramMetrics


# A write-side flood wait is shared across publishers, while long polling and
# inbound file metadata stay available so the gateway can keep receiving updates.
FLOOD_WAIT_GRACE_SECONDS = 1.0
FLOOD_WAIT_EXEMPT_METHODS = frozenset({"getUpdates", "getFile"})

# Telegram measures small video uploads itself, but a large MP4 sent without its size
# is stored as a 320x320 clip with no duration and no thumbnail, so clients draw a square
# box until the file is downloaded. ffprobe and ffmpeg are optional: without them, or on
# any failure, the video is sent as before.
VIDEO_TOOL_TIMEOUT_SECONDS = 30
# Bot API thumbnail limits: JPEG, under 200 kB, at most 320 px per side.
VIDEO_THUMBNAIL_MAX_SIDE = 320
VIDEO_THUMBNAIL_MAX_BYTES = 200 * 1000


class TelegramError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None, *, fallback_allowed: bool = True) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.message_ids: list[int] = []
        self.fallback_allowed = fallback_allowed


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


def _video_metadata(path: Path) -> dict[str, int]:
    """Return a video's displayed width and height and its duration, or {} if ffprobe can't tell."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    try:
        completed = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "V:0", "-show_entries",
                (
                    "stream=width,height,sample_aspect_ratio:stream_side_data=rotation"
                    ":stream_tags=rotate:format=duration"
                ),
                "-of", "json", str(path),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=VIDEO_TOOL_TIMEOUT_SECONDS,
            check=True,
        )
        probe = json.loads(completed.stdout)
        stream = probe["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        # Clients size the message from these numbers, so report what the player shows:
        # widen non-square pixels and swap the sides of a video turned a quarter turn.
        numerator, _, denominator = str(stream.get("sample_aspect_ratio", "")).partition(":")
        if numerator.isdigit() and denominator.isdigit() and int(numerator) and int(denominator):
            width = round(width * int(numerator) / int(denominator))
        rotation = stream.get("tags", {}).get("rotate", 0)  # ffprobe before 5.0
        for side_data in stream.get("side_data_list", []):
            rotation = side_data.get("rotation", rotation)
        if round(float(rotation)) % 180 == 90:
            width, height = height, width
        duration = probe.get("format", {}).get("duration")
    except (OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError, AttributeError):
        return {}
    if width <= 0 or height <= 0:
        return {}
    metadata = {"width": width, "height": height}
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        seconds = 0.0
    if math.isfinite(seconds) and seconds > 0:
        metadata["duration"] = max(1, round(seconds))
    return metadata


def _video_thumbnail(path: Path, metadata: dict[str, int]) -> bytes | None:
    """Return one JPEG frame within the Bot API thumbnail limits, or None if ffmpeg can't make it."""
    ffmpeg = shutil.which("ffmpeg")
    width, height = metadata.get("width", 0), metadata.get("height", 0)
    if not ffmpeg or width <= 0 or height <= 0:
        return None
    scale = min(1.0, VIDEO_THUMBNAIL_MAX_SIDE / max(width, height))
    size = f"{max(1, round(width * scale))}:{max(1, round(height * scale))}"
    # One second in skips a fade from black; a shorter clip uses its first frame.
    offset = "1" if metadata.get("duration", 0) >= 2 else "0"
    try:
        completed = subprocess.run(
            [
                ffmpeg, "-v", "error", "-nostdin", "-ss", offset, "-i", str(path),
                "-map", "0:V:0", "-frames:v", "1", "-vf", f"scale={size},setsar=1",
                "-q:v", "4", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=VIDEO_TOOL_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    thumbnail = completed.stdout
    return thumbnail if 0 < len(thumbnail) < VIDEO_THUMBNAIL_MAX_BYTES else None


class TelegramClient:
    def __init__(
        self, token: str, metrics_path: Path | None = None, *, local_api_url: str | None = None
    ):
        origin = (local_api_url or "https://api.telegram.org").rstrip("/")
        self._base_url = f"{origin}/bot{token}/"
        self._file_base_url = f"{origin}/file/bot{token}/"
        self._local_api = bool(local_api_url)
        self._flood_wait_lock = threading.Lock()
        self._flood_wait_until = 0.0
        self._metrics = TelegramMetrics(metrics_path)

    def _check_flood_wait(self, method: str) -> None:
        if method in FLOOD_WAIT_EXEMPT_METHODS:
            return
        with self._flood_wait_lock:
            remaining = self._flood_wait_until - time.monotonic()
        if remaining > 0:
            raise TelegramError(
                f"Telegram {method} deferred by local flood control",
                retry_after=remaining,
            )

    def _record_flood_wait(self, method: str, retry_after: float | None) -> float | None:
        if retry_after is None or method in FLOOD_WAIT_EXEMPT_METHODS:
            return retry_after
        deadline = time.monotonic() + retry_after + FLOOD_WAIT_GRACE_SECONDS
        with self._flood_wait_lock:
            self._flood_wait_until = max(self._flood_wait_until, deadline)
            return max(1.0, self._flood_wait_until - time.monotonic())

    def _begin_api_call(self, method: str) -> None:
        try:
            self._check_flood_wait(method)
        except TelegramError:
            self._metrics.record(method, "local_deferred")
            raise

    def flush_metrics(self) -> None:
        self._metrics.flush()

    def metrics_snapshot(self) -> dict[str, Any]:
        snapshot = self._metrics.snapshot()
        with self._flood_wait_lock:
            remaining = max(0.0, self._flood_wait_until - time.monotonic())
        snapshot["flood_wait_seconds"] = int(remaining + 0.999)
        return snapshot

    def _call(
        self, method: str, payload: dict[str, Any] | None = None, *, timeout: float = 45
    ) -> Any:
        self._begin_api_call(method)
        encoded_payload: dict[str, str] = {}
        for key, value in (payload or {}).items():
            encoded_payload[key] = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
        request = urllib.request.Request(
            self._base_url + method,
            data=urllib.parse.urlencode(encoded_payload).encode("utf-8"),
            method="POST",
        )
        return self._send_request(method, request, timeout=timeout)

    def _send_request(
        self,
        method: str,
        request: urllib.request.Request,
        *,
        timeout: float,
        connection_error: str = "connection failed",
    ) -> Any:
        """Send one Bot API request and record its outcome; returns `result`."""
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
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
            raw_retry_after = _retry_after(error_body)
            self._metrics.record(
                method, "rate_limited" if raw_retry_after is not None else "failed"
            )
            raise TelegramError(
                f"Telegram {method} failed: {detail}",
                retry_after=self._record_flood_wait(method, raw_retry_after),
                fallback_allowed=exc.code in {400, 404},
            ) from exc
        except (OSError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            self._metrics.record(method, "failed")
            reason = getattr(exc, "reason", exc)
            raise TelegramError(
                f"Telegram {method} {connection_error}: {reason}", fallback_allowed=False
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._metrics.record(method, "failed")
            raise TelegramError(f"Telegram {method} returned invalid JSON", fallback_allowed=False) from exc

        if not isinstance(body, dict):
            self._metrics.record(method, "failed")
            raise TelegramError(f"Telegram {method} returned a non-object response", fallback_allowed=False)
        if not body.get("ok"):
            raw_retry_after = _retry_after(body)
            self._metrics.record(
                method, "rate_limited" if raw_retry_after is not None else "failed"
            )
            raise TelegramError(
                f"Telegram {method} failed: {body.get('description', 'unknown error')}",
                retry_after=self._record_flood_wait(method, raw_retry_after),
                fallback_allowed=body.get("error_code") in {400, 404},
            )
        self._metrics.record(method, "success")
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
        last_message_id: int | None = None
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
            if isinstance(result, dict):
                message_id = result.get("message_id")
                if isinstance(message_id, int):
                    last_message_id = message_id
        return last_message_id

    def copy_message(
        self, chat_id: int, from_chat_id: int, message_id: int
    ) -> int | None:
        result = self._call(
            "copyMessage",
            {
                "chat_id": chat_id,
                "from_chat_id": from_chat_id,
                "message_id": message_id,
            },
        )
        copied_id = result.get("message_id") if isinstance(result, dict) else None
        return copied_id if isinstance(copied_id, int) and copied_id > 0 else None

    def send_markdown(
        self,
        chat_id: int,
        markdown: str,
        reply_markup: dict[str, Any] | None = None,
        max_chunks: int | None = None,
    ) -> int | None:
        ids = self.send_markdown_parts(
            chat_id, markdown, reply_markup=reply_markup, max_chunks=max_chunks
        )
        return ids[-1] if ids else None

    def send_markdown_parts(
        self,
        chat_id: int,
        markdown: str,
        reply_markup: dict[str, Any] | None = None,
        max_chunks: int | None = None,
        disable_notification: bool = False,
    ) -> list[int]:
        chunks = split_markdown(markdown)
        if max_chunks is not None:
            chunks = chunks[: max(0, max_chunks)]
        ids: list[int] = []
        try:
            for index, chunk in enumerate(chunks):
                html_text = markdown_to_telegram_html(chunk)
                payload: dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": html_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                if disable_notification:
                    payload["disable_notification"] = True
                if reply_markup is not None and index == len(chunks) - 1:
                    payload["reply_markup"] = reply_markup
                try:
                    result = self._call("sendMessage", payload)
                except TelegramError as exc:
                    if exc.retry_after is not None or not exc.fallback_allowed:
                        raise
                    payload["text"] = chunk
                    payload.pop("parse_mode", None)
                    result = self._call("sendMessage", payload)
                if isinstance(result, dict):
                    message_id = result.get("message_id")
                    if isinstance(message_id, int):
                        ids.append(message_id)
        except TelegramError as exc:
            exc.message_ids = list(ids)
            raise
        return ids

    def send_rich_markdown(
        self,
        chat_id: int,
        markdown: str,
        reply_markup: dict[str, Any] | None = None,
        max_chunks: int | None = None,
    ) -> int | None:
        ids = self.send_rich_markdown_parts(
            chat_id, markdown, reply_markup=reply_markup, max_chunks=max_chunks
        )
        return ids[-1] if ids else None

    def send_rich_markdown_parts(
        self,
        chat_id: int,
        markdown: str,
        reply_markup: dict[str, Any] | None = None,
        max_chunks: int | None = None,
        disable_notification: bool = False,
    ) -> list[int]:
        chunks = split_rich_markdown(markdown)
        if max_chunks is not None:
            chunks = chunks[: max(0, max_chunks)]
        ids: list[int] = []
        try:
            for index, chunk in enumerate(chunks):
                payload: dict[str, Any] = {
                    "chat_id": chat_id,
                    "rich_message": {"markdown": chunk},
                }
                if disable_notification:
                    payload["disable_notification"] = True
                if reply_markup is not None and index == len(chunks) - 1:
                    payload["reply_markup"] = reply_markup
                result = self._call("sendRichMessage", payload)
                if isinstance(result, dict):
                    message_id = result.get("message_id")
                    if isinstance(message_id, int):
                        ids.append(message_id)
        except TelegramError as exc:
            exc.message_ids = list(ids)
            raise
        return ids

    def edit_rich_markdown(
        self,
        chat_id: int,
        message_id: int,
        markdown: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        extra_message_ids: list[int] | None = None,
        allow_split: bool = True,
    ) -> list[int]:
        chunks = split_rich_markdown(markdown)
        if not chunks:
            return []
        if not allow_split:
            chunks = chunks[:1]
        return self._edit_or_send_chunks(
            chat_id,
            message_id,
            chunks,
            reply_markup=reply_markup,
            extra_message_ids=extra_message_ids,
            rich=True,
        )

    def _edit_or_send_chunks(
        self,
        chat_id: int,
        message_id: int,
        chunks: list[str],
        reply_markup: dict[str, Any] | None,
        extra_message_ids: list[int] | None,
        rich: bool,
    ) -> list[int]:
        existing = [message_id]
        for extra_id in extra_message_ids or []:
            if extra_id > 0 and extra_id not in existing:
                existing.append(extra_id)
        used: list[int] = []
        try:
            for index, chunk in enumerate(chunks):
                markup = reply_markup if index == len(chunks) - 1 else None
                if index < len(existing):
                    target = existing[index]
                    if rich:
                        payload: dict[str, Any] = {
                            "chat_id": chat_id,
                            "message_id": target,
                            "rich_message": {"markdown": chunk},
                        }
                        if markup is not None:
                            payload["reply_markup"] = markup
                        try:
                            self._call("editMessageText", payload)
                        except TelegramError as exc:
                            if "message is not modified" not in str(exc).lower():
                                raise
                    else:
                        try:
                            self.edit_message(
                                chat_id,
                                target,
                                markdown_to_telegram_html(chunk),
                                reply_markup=markup,
                                parse_mode="HTML",
                            )
                        except TelegramError as exc:
                            if exc.retry_after is not None or not exc.fallback_allowed:
                                raise
                            self.edit_message(chat_id, target, chunk, reply_markup=markup)
                    used.append(target)
                    continue
                if rich:
                    extra: dict[str, Any] = {
                        "chat_id": chat_id,
                        "rich_message": {"markdown": chunk},
                    }
                    if markup is not None:
                        extra["reply_markup"] = markup
                    result = self._call("sendRichMessage", extra)
                else:
                    sent = self.send_markdown(chat_id, chunk, reply_markup=markup, max_chunks=1)
                    result = {"message_id": sent} if sent is not None else {}
                new_id = result.get("message_id") if isinstance(result, dict) else None
                if isinstance(new_id, int):
                    used.append(new_id)
        except TelegramError as exc:
            exc.message_ids = list(existing + [item for item in used if item not in existing])
            raise
        return used

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
        *,
        extra_message_ids: list[int] | None = None,
        allow_split: bool = True,
    ) -> list[int]:
        chunks = split_markdown(markdown)
        if not chunks:
            return []
        if not allow_split:
            chunks = chunks[:1]
        return self._edit_or_send_chunks(
            chat_id,
            message_id,
            chunks,
            reply_markup=reply_markup,
            extra_message_ids=extra_message_ids,
            rich=False,
        )

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
            raise TelegramError(tr("File exceeds the size limit (max {limit} MB)", limit=max_bytes // 1024 // 1024))
        file_path = metadata["file_path"]
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        total = 0
        try:
            if self._local_api and Path(file_path).is_absolute():
                source = Path(file_path).open("rb")
            else:
                request = urllib.request.Request(self._file_base_url + file_path, method="GET")
                source = urllib.request.urlopen(request, timeout=60)
            with source as response, temporary.open("wb") as handle:
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise TelegramError(
                            tr("File exceeds the size limit (max {limit} MB)", limit=max_bytes // 1024 // 1024)
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
            raise TelegramError(tr("Telegram file download failed: {reason}", reason=reason)) from exc
        return total

    def send_local_file(
        self, chat_id: int, path: Path, as_photo: bool = False, as_video: bool = False
    ) -> None:
        if as_photo and as_video:
            raise ValueError("a file cannot be sent as both photo and video")
        if as_photo:
            method, field_name = "sendPhoto", "photo"
        elif as_video:
            method, field_name = "sendVideo", "video"
        else:
            method, field_name = "sendDocument", "document"
        fields: dict[str, Any] = {"chat_id": chat_id}
        files: list[tuple[str, str, str, bytes]] = []
        if as_video:
            metadata = _video_metadata(path)
            fields.update(supports_streaming="true", **metadata)
            thumbnail = _video_thumbnail(path, metadata)
            if thumbnail is not None:
                files.append(("thumbnail", "thumbnail.jpg", "image/jpeg", thumbnail))
        if self._local_api:
            try:
                resolved = path.resolve(strict=True)
                with resolved.open("rb"):
                    pass
            except OSError as exc:
                self._metrics.record(method, "failed")
                raise TelegramError(tr("Could not read the file to send: {error}", error=exc)) from exc
            # The --local server recognizes local inputs by the file:/ prefix;
            # a bare absolute path is interpreted as a remote file identifier.
            # It reads the file itself without buffering it in the gateway.
            fields[field_name] = resolved.as_uri()
            if not files:
                self._call(method, fields, timeout=3600)
                return
            # The thumbnail travels in the request body, so the server never needs
            # to read a temporary file of the gateway.
            self._begin_api_call(method)
            self._send_multipart(method, fields, files, timeout=3600)
            return
        self._begin_api_call(method)
        content_type = "video/mp4" if as_video else "application/octet-stream"
        try:
            file_data = path.read_bytes()
        except OSError as exc:
            self._metrics.record(method, "failed")
            raise TelegramError(tr("Could not read the file to send: {error}", error=exc)) from exc
        files.insert(0, (field_name, path.name.replace('"', "_"), content_type, file_data))
        self._send_multipart(method, fields, files, timeout=90, connection_error="failed")

    def _send_multipart(
        self,
        method: str,
        fields: dict[str, Any],
        files: list[tuple[str, str, str, bytes]],
        *,
        timeout: float,
        connection_error: str = "connection failed",
    ) -> Any:
        """Send form fields and (field, filename, content type, data) file parts."""
        boundary = f"----telegram-cli-gateway-{uuid.uuid4().hex}"
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            for name, value in fields.items()
        ]
        for name, filename, content_type, data in files:
            parts.extend(
                [
                    (
                        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                        f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n"
                    ).encode("utf-8"),
                    data,
                    b"\r\n",
                ]
            )
        parts.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            self._base_url + method,
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        return self._send_request(method, request, timeout=timeout, connection_error=connection_error)

    def send_action(self, chat_id: int, action: str = "typing") -> None:
        self._call("sendChatAction", {"chat_id": chat_id, "action": action})

    def set_commands(self, commands: Iterable[tuple[str, str]]) -> None:
        payload = [{"command": command, "description": description} for command, description in commands]
        self._call("setMyCommands", {"commands": payload})
