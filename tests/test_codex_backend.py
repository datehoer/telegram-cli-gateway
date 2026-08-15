from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from cli_telegram_gateway.attachments import Attachment
from cli_telegram_gateway.codex_backend import CodexAppServer


class CodexTurnInputTests(unittest.TestCase):
    def test_images_are_native_inputs_and_documents_are_named_paths(self) -> None:
        server = CodexAppServer(("codex",), lambda *_: None, lambda *_: None)
        captured: dict[str, object] = {}

        def fake_request(method: str, params: dict[str, object], timeout: float = 30) -> object:
            captured["method"] = method
            captured["params"] = params
            return {"turn": {"id": "turn-1"}}

        server.request = fake_request  # type: ignore[method-assign]
        image = Attachment(Path("/tmp/image.png"), "image.png", "image/png", True)
        document = Attachment(Path("/tmp/notes.txt"), "notes.txt", "text/plain", False)
        self.assertEqual(server.start_turn("thread-1", "inspect", (image, document)), "turn-1")
        self.assertEqual(captured["method"], "turn/start")
        inputs = captured["params"]["input"]  # type: ignore[index]
        self.assertIn({"type": "localImage", "path": "/tmp/image.png"}, inputs)
        self.assertIn("/tmp/notes.txt", inputs[-1]["text"])

    def test_start_turn_passes_model_and_effort(self) -> None:
        server = CodexAppServer(("codex",), lambda *_: None, lambda *_: None)
        captured: dict[str, object] = {}

        def fake_request(method: str, params: dict[str, object], timeout: float = 30) -> object:
            captured["method"] = method
            captured["params"] = params
            return {"turn": {"id": "turn-1"}}

        server.request = fake_request  # type: ignore[method-assign]
        server.start_turn("thread-1", "go", model="gpt-5.5", effort="low")
        self.assertEqual(captured["params"]["model"], "gpt-5.5")  # type: ignore[index]
        self.assertEqual(captured["params"]["effort"], "low")  # type: ignore[index]

    def test_start_turn_omits_unset_model_and_effort(self) -> None:
        server = CodexAppServer(("codex",), lambda *_: None, lambda *_: None)
        captured: dict[str, object] = {}

        def fake_request(method: str, params: dict[str, object], timeout: float = 30) -> object:
            captured["params"] = params
            return {"turn": {"id": "turn-1"}}

        server.request = fake_request  # type: ignore[method-assign]
        server.start_turn("thread-1", "go")
        self.assertNotIn("model", captured["params"])  # type: ignore[index]
        self.assertNotIn("effort", captured["params"])  # type: ignore[index]

    def test_start_thread_passes_model(self) -> None:
        server = CodexAppServer(("codex",), lambda *_: None, lambda *_: None)
        captured: dict[str, object] = {}

        def fake_request(method: str, params: dict[str, object], timeout: float = 30) -> object:
            captured["params"] = params
            return {"thread": {"id": "thread-1"}}

        server.request = fake_request  # type: ignore[method-assign]
        server.start_thread("/tmp", model="gpt-5.5")
        self.assertEqual(captured["params"]["model"], "gpt-5.5")  # type: ignore[index]

    def test_steer_targets_the_active_turn_and_reuses_input_builder(self) -> None:
        server = CodexAppServer(("codex",), lambda *_: None, lambda *_: None)
        captured: dict[str, object] = {}

        def fake_request(method: str, params: dict[str, object], timeout: float = 30) -> object:
            captured["method"] = method
            captured["params"] = params
            return {"turnId": "turn-active"}

        server.request = fake_request  # type: ignore[method-assign]
        self.assertEqual(
            server.steer_turn("thread-1", "turn-active", "new direction"),
            "turn-active",
        )
        self.assertEqual(captured["method"], "turn/steer")
        self.assertEqual(captured["params"]["expectedTurnId"], "turn-active")  # type: ignore[index]


@unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
class CodexAppServerIntegrationTests(unittest.TestCase):
    def test_initialize_and_start_ephemeral_thread(self) -> None:
        notifications: list[str] = []
        requests: list[str] = []
        server = CodexAppServer(
            (shutil.which("codex") or "codex",),
            lambda method, _params: notifications.append(method),
            lambda _request_id, method, _params: requests.append(method),
        )
        try:
            server.start()
            with tempfile.TemporaryDirectory() as temporary:
                thread_id = server.start_thread(temporary, ephemeral=True)
            self.assertTrue(thread_id)
            self.assertEqual(requests, [])
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
