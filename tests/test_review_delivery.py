from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_app
from cli_telegram_gateway.__main__ import check_config
from cli_telegram_gateway.config import ConfigError, _positive_float
from cli_telegram_gateway.telegram import TelegramClient, TelegramError


class DeliveryRegressionTests(unittest.TestCase):
    make_app = test_app.GatewayEventTests.make_app

    def test_partial_long_answer_retries_reuse_confirmed_message_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            app = self.make_app(project)
            session = app.sessions.create_headless("pi", project, 1)
            view = app._register_turn(session, "turn")
            view.status = "completed"
            answer = "x" * 65000
            sent = []
            edited = []
            fail_once = True

            def call(method, payload):
                nonlocal fail_once
                if method == "sendRichMessage":
                    if len(sent) == 1 and fail_once:
                        fail_once = False
                        raise TelegramError("rate limited", retry_after=1)
                    sent.append(payload["rich_message"]["markdown"])
                    return {"message_id": 100 + len(sent)}
                if method == "editMessageText":
                    edited.append(payload["message_id"])
                    return True
                self.fail(method)

            app.telegram._call = call
            with self.assertRaises(TelegramError):
                app._publish_final_turn(view, answer, with_artifacts=False)
            self.assertEqual(view.message_ids, [101])
            app._publish_final_turn(view, answer, with_artifacts=False)
            self.assertEqual(view.message_ids, [101, 102, 103])
            self.assertEqual(edited, [101])
            self.assertEqual("".join(sent), answer)

    def test_partial_edit_retains_newly_sent_overflow_chunks(self) -> None:
        client = TelegramClient("test")
        sends = 0

        def call(method, _payload):
            nonlocal sends
            if method == "sendRichMessage":
                sends += 1
                if sends == 2:
                    raise TelegramError("rate limited", retry_after=1)
                return {"message_id": 12}
            return True

        client._call = call
        with self.assertRaises(TelegramError) as caught:
            client.edit_rich_markdown(1, 11, "x" * 65000)
        self.assertEqual(caught.exception.message_ids, [11, 12])

    def test_plain_markdown_partial_failure_retains_sent_ids(self) -> None:
        client = TelegramClient("test")
        replies = iter([{"message_id": 1}, TelegramError("rate limited", retry_after=1)])

        def call(*_args):
            result = next(replies)
            if isinstance(result, Exception):
                raise result
            return result

        client._call = call
        with self.assertRaises(TelegramError) as caught:
            client.send_markdown_parts(1, "x" * 8000)
        self.assertEqual(caught.exception.message_ids, [1])

    def test_transport_failure_does_not_trigger_immediate_format_retry(self) -> None:
        client = TelegramClient("test")
        with patch("urllib.request.urlopen", side_effect=ConnectionResetError("fixture")) as request:
            with self.assertRaises(TelegramError) as caught:
                client.send_markdown(1, "hello")
        self.assertFalse(caught.exception.fallback_allowed)
        self.assertEqual(request.call_count, 1)

    def test_non_object_response_and_invalid_encoding_are_telegram_errors(self) -> None:
        for content in (b"[]", b"null", b"\xff"):
            with self.subTest(content=content):
                response = MagicMock()
                response.__enter__.return_value = response
                response.read.return_value = content
                with patch("urllib.request.urlopen", return_value=response):
                    with self.assertRaises(TelegramError):
                        TelegramClient("test")._call("sendMessage", {"text": "hello"})

    def test_nonfinite_poll_intervals_are_rejected(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                _positive_float({"INTERVAL": value}, "INTERVAL", 1)

    def test_headless_config_check_does_not_require_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary), enabled_clis=("pi",))
            with patch("shutil.which", return_value=None), patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(check_config(app.config), 0)
