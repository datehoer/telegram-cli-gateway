from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from cli_telegram_gateway.telegram import TelegramClient, TelegramError, split_message


class TelegramTests(unittest.TestCase):
    def test_http_error_exposes_telegram_retry_after(self) -> None:
        client = TelegramClient("test")
        body = json.dumps({
            "ok": False,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 7},
        }).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://api.telegram.org/test",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(body),
        )
        with patch("cli_telegram_gateway.telegram.time.monotonic", return_value=100):
            with patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(TelegramError) as caught:
                    client._call("sendMessage", {"chat_id": 1, "text": "hello"})
        self.assertEqual(caught.exception.retry_after, 8)
        today = client.metrics_snapshot()["today"]
        self.assertEqual(today["network_requests"], 1)
        self.assertEqual(today["failed_requests"], 1)
        self.assertEqual(today["rate_limited"], 1)

    def test_retry_after_defers_other_writes_but_not_update_polling(self) -> None:
        client = TelegramClient("test")
        body = json.dumps({
            "ok": False,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 60},
        }).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://api.telegram.org/test",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(body),
        )
        with patch("cli_telegram_gateway.telegram.time.monotonic", return_value=100):
            with patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(TelegramError):
                    client._call("editMessageText", {"chat_id": 1, "text": "one"})

            with patch("urllib.request.urlopen") as blocked_request:
                with self.assertRaises(TelegramError) as caught:
                    client._call("sendRichMessage", {"chat_id": 1, "text": "two"})
            blocked_request.assert_not_called()
            self.assertEqual(caught.exception.retry_after, 61)

            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b'{"ok": true, "result": []}'
            with patch("urllib.request.urlopen", return_value=response) as polling_request:
                self.assertEqual(client.get_updates(None, 1), [])
            polling_request.assert_called_once()
        today = client.metrics_snapshot()["today"]
        self.assertEqual(today["network_requests"], 1)
        self.assertEqual(today["rate_limited"], 1)
        self.assertEqual(today["local_deferrals"], 1)

    def test_successful_write_call_is_counted(self) -> None:
        client = TelegramClient("test")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true, "result": {"message_id": 7}}'
        with patch("urllib.request.urlopen", return_value=response):
            client._call("sendMessage", {"chat_id": 1, "text": "hello"})
        today = client.metrics_snapshot()["today"]
        self.assertEqual(today["network_requests"], 1)
        self.assertEqual(today["successful_requests"], 1)
        self.assertEqual(today["new_messages"], 1)

    def test_successful_file_upload_is_counted(self) -> None:
        client = TelegramClient("test")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true, "result": {"message_id": 7}}'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "photo.jpg"
            path.write_bytes(b"image")
            with patch("urllib.request.urlopen", return_value=response):
                client.send_local_file(1, path, as_photo=True)
        today = client.metrics_snapshot()["today"]
        self.assertEqual(today["new_messages"], 1)
        self.assertEqual(today["methods"]["sendPhoto"]["success"], 1)

    def test_markdown_fallback_does_not_retry_rate_limit_immediately(self) -> None:
        client = TelegramClient("test")
        calls: list[str] = []

        def fake_call(method: str, _payload: object = None) -> object:
            calls.append(method)
            raise TelegramError("rate limited", retry_after=30)

        client._call = fake_call  # type: ignore[method-assign]
        with self.assertRaises(TelegramError):
            client.send_markdown(1, "**hello**")
        self.assertEqual(calls, ["sendMessage"])

    def test_markdown_edit_fallback_does_not_retry_rate_limit_immediately(self) -> None:
        client = TelegramClient("test")
        calls: list[str] = []

        def fake_edit(*_args: object, **_kwargs: object) -> None:
            calls.append("edit")
            raise TelegramError("rate limited", retry_after=30)

        client.edit_message = fake_edit  # type: ignore[method-assign]
        with self.assertRaises(TelegramError):
            client.edit_markdown(1, 2, "**hello**")
        self.assertEqual(calls, ["edit"])

    def test_split_message_prefers_newline(self) -> None:
        chunks = split_message("a" * 6 + "\n" + "b" * 6, limit=10)
        self.assertEqual(chunks, ["aaaaaa", "bbbbbb"])

    def test_split_message_hard_splits_long_line(self) -> None:
        chunks = split_message("abcdefghijk", limit=5)
        self.assertEqual(chunks, ["abcde", "fghij", "k"])

    def test_send_message_returns_first_message_id(self) -> None:
        client = TelegramClient("test")
        calls: list[str] = []

        def fake_call(method: str, _payload: object = None) -> object:
            calls.append(method)
            return {"message_id": len(calls)}

        client._call = fake_call  # type: ignore[method-assign]
        self.assertEqual(client.send_message(1, "hello"), 1)
        self.assertEqual(calls, ["sendMessage"])

    def test_copy_message_returns_new_message_id(self) -> None:
        client = TelegramClient("test")
        calls: list[tuple[str, object]] = []

        def fake_call(method: str, payload: object = None) -> object:
            calls.append((method, payload))
            return {"message_id": 99}

        client._call = fake_call  # type: ignore[method-assign]
        self.assertEqual(client.copy_message(1, 1, 88), 99)
        self.assertEqual(calls[0][0], "copyMessage")
        self.assertEqual(
            calls[0][1],
            {"chat_id": 1, "from_chat_id": 1, "message_id": 88},
        )

    def test_session_keyboard_is_sent_as_reply_markup(self) -> None:
        client = TelegramClient("test")
        payloads: list[object] = []

        def fake_call(_method: str, payload: object = None) -> object:
            payloads.append(payload)
            return {"message_id": 1}

        client._call = fake_call  # type: ignore[method-assign]
        markup = {"inline_keyboard": [[{"text": "one", "callback_data": "use:one"}]]}
        client.send_message(1, "sessions", reply_markup=markup)
        self.assertEqual(payloads[0]["reply_markup"], markup)  # type: ignore[index]

    def test_send_markdown_uses_safe_html_parse_mode(self) -> None:
        client = TelegramClient("test")
        payloads: list[object] = []

        def fake_call(_method: str, payload: object = None) -> object:
            payloads.append(payload)
            return {"message_id": 1}

        client._call = fake_call  # type: ignore[method-assign]
        client.send_markdown(1, "**hello** <world>")
        self.assertEqual(payloads[0]["parse_mode"], "HTML")  # type: ignore[index]
        self.assertEqual(payloads[0]["text"], "<b>hello</b> &lt;world&gt;")  # type: ignore[index]

    def test_send_rich_markdown_passes_gfm_table_to_rich_message_api(self) -> None:
        client = TelegramClient("test")
        calls: list[tuple[str, object]] = []

        def fake_call(method: str, payload: object = None) -> object:
            calls.append((method, payload))
            return {"message_id": 7}

        client._call = fake_call  # type: ignore[method-assign]
        table = "| Name | Value |\n|---|---|\n| one | two |"
        self.assertEqual(client.send_rich_markdown(1, table), 7)
        self.assertEqual(calls[0][0], "sendRichMessage")
        self.assertEqual(calls[0][1]["rich_message"], {"markdown": table})  # type: ignore[index]

    def test_edit_rich_markdown_replaces_fallback_message_with_rich_content(self) -> None:
        client = TelegramClient("test")
        calls: list[tuple[str, object]] = []
        client._call = (  # type: ignore[method-assign]
            lambda method, payload=None: calls.append((method, payload)) or {"message_id": 1}
        )
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        client.edit_rich_markdown(1, 9, table)
        self.assertEqual(calls[0][0], "editMessageText")
        self.assertEqual(calls[0][1]["message_id"], 9)  # type: ignore[index]
        self.assertEqual(calls[0][1]["rich_message"], {"markdown": table})  # type: ignore[index]

    def test_edit_rich_markdown_tolerates_unchanged_message(self) -> None:
        client = TelegramClient("test")
        client._call = (  # type: ignore[method-assign]
            lambda _method, _payload=None: (_ for _ in ()).throw(
                TelegramError("message is not modified")
            )
        )
        client.edit_rich_markdown(1, 9, "same content")  # 不应抛出


if __name__ == "__main__":
    unittest.main()
