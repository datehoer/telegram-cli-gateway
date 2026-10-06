from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cli_telegram_gateway.commands import (
    EXTENSION_DIR_TOKEN,
    CommandError,
    CommandManifestError,
    DirectCommand,
    DirectCommandRunner,
    load_direct_commands,
    parse_manifest,
)
from cli_telegram_gateway.config import Config


def manifest(**overrides: object) -> str:
    payload = {
        "schema_version": 1,
        "id": "demo",
        "name": "Demo",
        "command": "demo",
        "usage": "/demo <text>",
        "description": "demo extension",
        "exec": ["python3", "main.py"],
    }
    payload.update(overrides)
    return json.dumps(payload)


class ParseManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.extensions = self.project / "extensions"
        self.extension = self.extensions / "demo"
        self.extension.mkdir(parents=True)
        self.config = Config(
            project_dir=self.project,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(self.project,),
            default_workdir=self.project,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
        )

    def write_manifest(self, text: str) -> Path:
        path = self.extension / "manifest.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_parses_a_valid_manifest(self) -> None:
        command = parse_manifest(self.write_manifest(manifest()), self.config)
        self.assertEqual(command.command, "demo")
        self.assertEqual(command.argv, ("python3", "main.py"))
        self.assertEqual(command.cwd, self.project)
        self.assertEqual(command.timeout_seconds, 30 * 60)
        self.assertEqual(command.max_output_bytes, 16384)
        self.assertEqual(command.manifest_path, (self.extension / "manifest.json").resolve())

    def test_relative_cwd_resolves_against_the_extension_directory(self) -> None:
        child = self.extension / "work"
        child.mkdir()
        command = parse_manifest(self.write_manifest(manifest(cwd="work")), self.config)
        self.assertEqual(command.cwd, child.resolve())

    def test_rejects_cwd_outside_allowed_workdirs(self) -> None:
        with self.assertRaises(CommandManifestError) as caught:
            parse_manifest(self.write_manifest(manifest(cwd="/etc")), self.config)
        self.assertIn("ALLOWED_WORKDIRS", str(caught.exception))

    def test_rejects_bad_schema_version(self) -> None:
        with self.assertRaises(CommandManifestError) as caught:
            parse_manifest(self.write_manifest(manifest(schema_version=2)), self.config)
        self.assertIn("schema_version", str(caught.exception))

    def test_rejects_invalid_command_name(self) -> None:
        for bad in ("Demo", "1demo", "demo-x", "demo x", ""):
            with self.subTest(command=bad), self.assertRaises(CommandManifestError):
                parse_manifest(self.write_manifest(manifest(command=bad)), self.config)

    def test_rejects_empty_exec_and_non_string_argv(self) -> None:
        for bad in ([], ["python3", ""], ["python3", 5]):
            with self.subTest(exec=bad), self.assertRaises(CommandManifestError):
                parse_manifest(self.write_manifest(manifest(exec=bad)), self.config)

    def test_clamps_timeout_and_output_window(self) -> None:
        with self.assertRaises(CommandManifestError):
            parse_manifest(self.write_manifest(manifest(timeout_seconds=999999)), self.config)
        with self.assertRaises(CommandManifestError):
            parse_manifest(self.write_manifest(manifest(max_output_bytes=1 << 30)), self.config)
        command = parse_manifest(
            self.write_manifest(manifest(timeout_seconds=5, max_output_bytes=100)),
            self.config,
        )
        self.assertEqual((command.timeout_seconds, command.max_output_bytes), (5, 100))

    def test_rejects_malformed_json(self) -> None:
        with self.assertRaises(CommandManifestError):
            parse_manifest(self.write_manifest("{not json"), self.config)


class LoadDirectCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.extensions = self.project / "extensions"
        self.extensions.mkdir(parents=True)
        self.config = Config(
            project_dir=self.project,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(self.project,),
            default_workdir=self.project,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
        )

    def add(self, directory: str, text: str) -> None:
        target = self.extensions / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text(text, encoding="utf-8")

    def test_loads_valid_extensions_and_warns_about_broken_ones(self) -> None:
        self.add("demo", manifest())
        self.add("broken", "{not json")
        warnings: list[str] = []
        loaded = load_direct_commands(self.config, warn=warnings.append)
        self.assertEqual(sorted(loaded), ["demo"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("broken", warnings[0])

    def test_skips_disabled_extensions_without_warning(self) -> None:
        self.add("demo", manifest(enabled=False))
        warnings: list[str] = []
        loaded = load_direct_commands(self.config, warn=warnings.append)
        self.assertEqual(loaded, {})
        self.assertEqual(warnings, [])

    def test_builtin_command_collision_is_rejected(self) -> None:
        self.add("demo", manifest(command="new"))
        warnings: list[str] = []
        loaded = load_direct_commands(
            self.config, reserved=frozenset({"new"}), warn=warnings.append
        )
        self.assertEqual(loaded, {})
        self.assertIn("内置命令冲突", warnings[0])

    def test_duplicate_command_keeps_the_first(self) -> None:
        self.add("a-demo", manifest())
        self.add("b-demo", manifest())
        warnings: list[str] = []
        loaded = load_direct_commands(self.config, warn=warnings.append)
        self.assertEqual(sorted(loaded), ["demo"])
        self.assertIn("a-demo", str(loaded["demo"].manifest_path))
        self.assertEqual(len(warnings), 1)


class DirectCommandRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)

    def make_runner(self, script: str, **overrides: object) -> DirectCommandRunner:
        extension = self.project / "extensions" / "demo"
        extension.mkdir(parents=True, exist_ok=True)
        target = extension / "main.py"
        target.write_text(script, encoding="utf-8")
        payload = {
            "schema_version": 1,
            "id": "demo",
            "command": "demo",
            "exec": ["python3", f"{EXTENSION_DIR_TOKEN}/main.py"],
            "cwd": str(self.project),
        }
        payload.update(overrides)
        manifest_path = extension / "manifest.json"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        config = Config(
            project_dir=self.project,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(self.project,),
            default_workdir=self.project,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
        )
        command = parse_manifest(manifest_path, config)
        return DirectCommandRunner(config, {command.command: command})

    @staticmethod
    def wait_for(runner: DirectCommandRunner, turn_id: str, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = runner.get(turn_id)
            if run is not None and run.status != "running":
                return run
            time.sleep(0.02)
        raise AssertionError("command did not finish in time")

    def test_stdout_progress_stderr_and_environment(self) -> None:
        script = (
            "import os, sys\n"
            "print('@@PROGRESS half way', flush=True)\n"
            "print('args=' + repr(sys.argv[1:]))\n"
            "print('cwd=' + os.getcwd())\n"
            "print('json=' + os.environ['TG_ARGS_JSON'])\n"
            "print('user=' + os.environ['TG_USER_ID'])\n"
            "print('warned', file=sys.stderr)\n"
        )
        runner = self.make_runner(script)
        # args[0] 是子命令（走 TG_ARGV0），其余按顺序进 argv。
        run = runner.start(1, "demo", ["a b", "--flag"], raw_args="a b --flag", user_id=7)
        finished = self.wait_for(runner, run.turn_id)
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.progress, "half way")
        output = finished.output_text()
        self.assertIn("args=['--flag']", output)
        self.assertIn(f"cwd={self.project}", output)
        self.assertIn('json=["a b", "--flag"]', output)
        self.assertIn("user=7", output)
        self.assertIn("warned", finished.error_text())
        self.assertNotIn("@@PROGRESS", output)

    def test_non_zero_exit_code_fails_and_keeps_stderr(self) -> None:
        script = "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n"
        runner = self.make_runner(script)
        run = runner.start(1, "demo", [])
        finished = self.wait_for(runner, run.turn_id)
        self.assertEqual(finished.status, "failed")
        self.assertIn("退出码 3", finished.error_text())
        self.assertIn("boom", finished.error_text())

    def test_second_run_of_the_same_command_in_one_chat_is_rejected(self) -> None:
        runner = self.make_runner("import time\ntime.sleep(5)\n")
        run = runner.start(1, "demo", [])
        try:
            with self.assertRaises(CommandError):
                runner.start(1, "demo", [])
        finally:
            runner.interrupt(run.turn_id)

    def test_timeout_kills_the_process_group(self) -> None:
        runner = self.make_runner("import time\ntime.sleep(30)\n", timeout_seconds=1)
        run = runner.start(1, "demo", [])
        finished = self.wait_for(runner, run.turn_id, timeout=15.0)
        self.assertEqual(finished.status, "interrupted")
        self.assertTrue(finished.timed_out)
        self.assertIn("已终止", finished.error_text())

    def test_interrupt_marks_the_run_interrupted(self) -> None:
        runner = self.make_runner("import time\ntime.sleep(30)\n")
        run = runner.start(1, "demo", [])
        time.sleep(0.3)
        self.assertTrue(runner.interrupt(run.turn_id))
        time.sleep(0.3)
        self.assertEqual(runner.get(run.turn_id).status, "interrupted")
        self.assertFalse(runner.interrupt(run.turn_id))

    def test_extension_dir_token_resolves_in_argv(self) -> None:
        runner = self.make_runner("print('token ok')\n")
        command = runner.commands["demo"]
        expected = str(command.directory / "main.py")
        self.assertEqual(command.resolved_argv(), ("python3", expected))
        run = runner.start(1, "demo", [])
        finished = self.wait_for(runner, run.turn_id)
        self.assertEqual(finished.status, "completed")
        self.assertIn("token ok", finished.output_text())

    def test_relative_exec_without_token_uses_the_manifest_directory(self) -> None:
        script = "print('relative ok')\n"
        runner = self.make_runner(script)
        manifest_path = runner.commands["demo"].manifest_path
        payload = dict(json.loads(manifest_path.read_text(encoding="utf-8")))
        payload["exec"] = ["python3", "main.py"]
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        reloaded = DirectCommandRunner(
            runner.config, {"demo": parse_manifest(manifest_path, runner.config)}
        )
        run = reloaded.start(1, "demo", [])
        finished = self.wait_for(reloaded, run.turn_id)
        self.assertEqual(finished.status, "completed")
        self.assertIn("relative ok", finished.output_text())

    def test_unknown_command_is_rejected(self) -> None:
        runner = self.make_runner("print('x')\n")
        with self.assertRaises(CommandError):
            runner.start(1, "nope", [])

    def test_running_for_chat_is_scoped_to_the_bot(self) -> None:
        runner = self.make_runner("import time\ntime.sleep(5)\n")
        run = runner.start(1, "demo", [], bot_key="default")
        try:
            self.assertEqual(len(runner.running_for_chat(1, "default")), 1)
            self.assertEqual(runner.running_for_chat(1, "worker"), [])
            self.assertEqual(runner.running_for_chat(2, "default"), [])
        finally:
            runner.interrupt(run.turn_id)


class CommandConfigTests(unittest.TestCase):
    def test_command_dir_must_be_inside_allowed_workdirs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            allowed = project / "projects"
            allowed.mkdir()
            (project / ".env").write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=123:test",
                        "TELEGRAM_ALLOWED_USERS=100",
                        f"DEFAULT_WORKDIR={allowed}",
                        f"ALLOWED_WORKDIRS={allowed}",
                        "COMMAND_DIR=/etc",
                    ]
                ),
                encoding="utf-8",
            )
            keys = {
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_ALLOWED_USERS",
                "DEFAULT_WORKDIR",
                "ALLOWED_WORKDIRS",
                "COMMAND_DIR",
                "CLI_CLAUDE",
                "CLI_CODEX",
                "CLI_GROK",
                "CLI_PI",
            }
            clean = {key: value for key, value in os.environ.items() if key not in keys}
            with patch.dict(os.environ, clean, clear=True):
                with self.assertRaises(Exception) as caught:
                    Config.load(project)
                self.assertIn("ALLOWED_WORKDIRS", str(caught.exception))

    def test_repo_extensions_directory_is_discovered_by_default(self) -> None:
        config = Config(
            project_dir=Path("/srv/projects/telegram-cli-gateway"),
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(Path("/srv/projects"),),
            default_workdir=Path("/srv/projects"),
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
        )
        roots = config.direct_command_roots
        self.assertIn(Path("/srv/projects/telegram-cli-gateway/extensions"), roots)


class BundledExampleTests(unittest.TestCase):
    def test_bundled_example_manifest_is_valid(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        example = repo / "extensions" / "example" / "manifest.json"
        if not example.exists():
            self.skipTest("bundled example extension is not present")
        config = Config(
            project_dir=repo,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(repo.parent,),
            default_workdir=repo.parent,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
        )
        command = parse_manifest(example, config)
        self.assertEqual(command.command, "example")
        self.assertTrue((example.parent / "main.py").exists(), "example entry point is missing")
        self.assertIsInstance(command, DirectCommand)


class RawArgsTests(unittest.TestCase):
    """直接命令不做 shell 分词。

    Cookie、JSON、rpdid=example'cookie-marker 这类内容里的引号是数据不是
    语法，shlex 会直接报 "No closing quotation"。网关必须把原文交给扩展。
    """

    COOKIE_WITH_APOSTROPHE = (
        "rpdid=example'cookie-marker; SESSDATA=abc%2Cdef; bili_jct=xyz"
    )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        extension = self.project / "extensions" / "demo"
        extension.mkdir(parents=True)
        (extension / "main.py").write_text(
            "import os, sys\n"
            "print('argv0=' + os.environ.get('TG_ARGV0', ''))\n"
            "print('args=' + repr(sys.argv[1:]))\n"
            "print('raw=' + os.environ.get('TG_RAW_ARGS', ''))\n",
            encoding="utf-8",
        )
        (extension / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "demo",
                    "command": "demo",
                    "exec": ["python3", "main.py"],
                    "cwd": str(self.project),
                }
            ),
            encoding="utf-8",
        )
        self.config = Config(
            project_dir=self.project,
            bot_token="test",
            allowed_user_ids=frozenset({1}),
            allow_groups=False,
            allowed_roots=(self.project,),
            default_workdir=self.project,
            cli_commands={name: ("/bin/sh",) for name in ("claude", "codex", "grok", "pi")},
            poll_timeout=1,
        )

    def run_with(self, raw_args: str) -> tuple[str, str]:
        command = parse_manifest(
            self.project / "extensions" / "demo" / "manifest.json", self.config
        )
        runner = DirectCommandRunner(self.config, {"demo": command})
        args = [line.strip() for line in raw_args.strip().splitlines()]
        args = [line for line in args if line]
        run = runner.start(1, "demo", args, raw_args=raw_args)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and run.status == "running":
            time.sleep(0.02)
        runner.shutdown()
        return run.output_text(), run.error_text()

    def test_cookie_with_apostrophe_survives_verbatim(self) -> None:
        # 单行输入：argv0 是整行，扩展自己从 TG_RAW_ARGS 里切子命令
        raw = f"login cookie {self.COOKIE_WITH_APOSTROPHE}"
        output, error = self.run_with(raw)
        self.assertEqual(error, "")
        self.assertIn("argv0=login cookie", output)
        # 关键：整段原文一字不差地传到了扩展里
        self.assertIn(self.COOKIE_WITH_APOSTROPHE, output)

    def test_shlex_would_have_failed_on_that_text(self) -> None:
        """记录这个 regression 的根因，避免有人再改成 shlex.split。"""
        import shlex

        with self.assertRaises(ValueError) as caught:
            shlex.split(self.COOKIE_WITH_APOSTROPHE)
        self.assertIn("quotation", str(caught.exception))

    def test_multiline_args_become_separate_arguments(self) -> None:
        raw = "login\nBVDATA\nSESSDATA=abc"
        output, _error = self.run_with(raw)
        self.assertIn("argv0=login", output)
        self.assertIn("args=['BVDATA', 'SESSDATA=abc']", output)

    def test_telegram_max_file_bytes_is_passed_to_the_extension(self) -> None:
        config = replace(self.config, telegram_max_file_bytes=12345678)
        command = parse_manifest(
            self.project / "extensions" / "demo" / "manifest.json", config
        )
        (self.project / "extensions" / "demo" / "main.py").write_text(
            "import os\nprint('limit=' + os.environ.get('TELEGRAM_MAX_FILE_BYTES', ''))\n",
            encoding="utf-8",
        )
        runner = DirectCommandRunner(config, {"demo": command})
        with patch.dict(os.environ, {}, clear=True):
            run = runner.start(1, "demo", [], raw_args="")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and run.status == "running":
                time.sleep(0.02)
        runner.shutdown()
        self.assertIn("limit=12345678", run.output_text())
