from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from . import __version__
from .app import GatewayApp
from .config import Config, ConfigError


def check_config(config: Config) -> int:
    problems: list[str] = []
    print(f"project: {config.project_dir}")
    print(f"telegram bots: configured ({len(config.telegram_bots)})")
    print(f"allowed users: {len(config.allowed_user_ids)}")
    print(f"allowed roots: {', '.join(str(path) for path in config.allowed_roots)}")
    print(f"default workdir: {config.default_workdir}")

    print(f"enabled CLIs: {', '.join(config.enabled_clis)}")
    for cli in config.enabled_clis:
        command = config.cli_commands[cli]
        executable = shutil.which(command[0]) if not Path(command[0]).is_absolute() else command[0]
        exists = bool(executable and Path(executable).exists())
        print(f"{cli}: {executable if exists else 'MISSING'}")
        model = config.default_model_for(cli)
        effort = config.default_effort_for(cli)
        if model or effort:
            print(
                f"{cli} defaults: model={model or 'CLI default'}, "
                f"effort={effort or 'CLI default'}"
            )
        if not exists:
            problems.append(f"{cli} executable is missing")

    command_dirs = config.direct_command_roots
    print(f"direct command dirs: {', '.join(str(path) for path in command_dirs) or 'none'}")
    if command_dirs:
        from .commands import load_direct_commands

        warnings: list[str] = []
        commands = load_direct_commands(config, warn=warnings.append)
        for warning in warnings:
            print(f"WARN: {warning}")
        for command in sorted(commands.values(), key=lambda item: item.command):
            executable = command.spawn_argv()[0]
            resolved = shutil.which(executable) if not Path(executable).is_absolute() else executable
            state = resolved if resolved and Path(resolved).exists() else "MISSING"
            print(f"/{command.command}: {command.name} -> {executable} ({state}), cwd={command.cwd}")
            if state == "MISSING":
                problems.append(f"extension /{command.command} executable is missing")
        if not commands:
            print("direct commands: none loaded")

    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    print("configuration check passed")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram gateway for persistent AI CLI sessions")
    parser.add_argument("--check", action="store_true", help="validate local configuration without contacting Telegram")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = Config.load()
    except ConfigError as exc:
        parser.error(str(exc))

    if args.check:
        raise SystemExit(check_config(config))
    GatewayApp(config).run()


if __name__ == "__main__":
    main()
