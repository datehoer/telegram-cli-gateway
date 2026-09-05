from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


def load_env_file(path: Path) -> dict[str, str]:
    """Parse the small KEY=VALUE subset used by this project."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"{path}:{line_number}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            raise ConfigError(f"{path}:{line_number}: invalid environment key")
        try:
            parsed = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise ConfigError(f"{path}:{line_number}: {exc}") from exc
        values[key] = " ".join(parsed) if parsed else ""
    return values


def _positive_int(values: dict[str, str], key: str, default: int) -> int:
    try:
        value = int(values.get(key, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be positive")
    return value


def _positive_float(values: dict[str, str], key: str, default: float) -> float:
    try:
        value = float(values.get(key, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be positive")
    return value


def _boolean(values: dict[str, str], key: str, default: bool) -> bool:
    raw = values.get(key, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be true or false")


def _auto_send_mode(values: dict[str, str], key: str, default: str) -> str:
    raw = values.get(key, default).strip().lower()
    if raw not in {"off", "images", "all"}:
        raise ConfigError(f"{key} must be one of off, images, all")
    return raw


@dataclass(frozen=True)
class Config:
    project_dir: Path
    bot_token: str
    allowed_user_ids: frozenset[int]
    allow_groups: bool
    allowed_roots: tuple[Path, ...]
    default_workdir: Path
    cli_commands: dict[str, tuple[str, ...]]
    poll_timeout: int
    output_poll_interval: float
    output_max_bytes: int
    tmux_socket_name: str
    enabled_clis: tuple[str, ...] = ("claude", "codex", "grok", "pi")
    stream_update_interval: float = 10.0
    telegram_max_file_bytes: int = 20 * 1024 * 1024
    auto_send_artifacts: str = "images"
    auto_resume: bool = False
    bot_tokens: tuple[tuple[str, str], ...] = ()
    cli_default_models: dict[str, str] = field(default_factory=dict)
    cli_default_efforts: dict[str, str] = field(default_factory=dict)

    @property
    def telegram_bots(self) -> tuple[tuple[str, str], ...]:
        """Configured Bot API entrances, keyed without exposing their tokens."""
        return self.bot_tokens or (("default", self.bot_token),)

    @property
    def runtime_dir(self) -> Path:
        return self.project_dir / ".runtime"

    def default_model_for(self, cli: str) -> str | None:
        return self.cli_default_models.get(cli)

    def default_effort_for(self, cli: str) -> str | None:
        return self.cli_default_efforts.get(cli)

    def resolve_workdir(self, requested: str | None) -> Path:
        candidate = Path(requested).expanduser() if requested else self.default_workdir
        candidate = candidate.resolve()
        if not candidate.is_dir():
            raise ConfigError(f"directory does not exist: {candidate}")
        if not any(candidate == root or candidate.is_relative_to(root) for root in self.allowed_roots):
            raise ConfigError(f"directory is outside ALLOWED_WORKDIRS: {candidate}")
        return candidate

    @classmethod
    def load(cls, project_dir: Path | None = None) -> "Config":
        project_dir = (project_dir or Path(__file__).resolve().parents[2]).resolve()
        file_values = load_env_file(project_dir / ".env")
        values = {**file_values, **os.environ}

        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        extra_tokens = [
            (match.group(1).lower(), value.strip())
            for key, value in values.items()
            if (match := re.fullmatch(r"TELEGRAM_BOT_TOKEN_([A-Za-z0-9_]+)", key))
            and value.strip()
        ]
        extra_tokens.sort(key=lambda item: item[0])
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is missing")
        configured_tokens = [("default", token), *extra_tokens]
        bot_keys = [bot_key for bot_key, _configured_token in configured_tokens]
        if len(bot_keys) != len(set(bot_keys)):
            raise ConfigError("Telegram Bot token names must be unique")
        raw_tokens = [configured_token for _bot_key, configured_token in configured_tokens]
        if len(raw_tokens) != len(set(raw_tokens)):
            raise ConfigError("the same Telegram Bot token is configured more than once")
        # Preserve the legacy field for callers that construct Config directly.
        token = configured_tokens[0][1]

        raw_users = values.get("TELEGRAM_ALLOWED_USERS", "")
        try:
            allowed_users = frozenset(
                int(item.strip()) for item in raw_users.replace(" ", ",").split(",") if item.strip()
            )
        except ValueError as exc:
            raise ConfigError("TELEGRAM_ALLOWED_USERS must contain numeric Telegram user IDs") from exc
        if not allowed_users:
            raise ConfigError("TELEGRAM_ALLOWED_USERS is empty")

        raw_roots = values.get("ALLOWED_WORKDIRS", "/srv/projects")
        allowed_roots = tuple(
            Path(item).expanduser().resolve()
            for item in raw_roots.split(os.pathsep)
            if item.strip()
        )
        if not allowed_roots:
            raise ConfigError("ALLOWED_WORKDIRS is empty")
        for root in allowed_roots:
            if not root.is_dir():
                raise ConfigError(f"allowed root does not exist: {root}")

        default_workdir = Path(values.get("DEFAULT_WORKDIR", str(allowed_roots[0]))).expanduser().resolve()
        if not default_workdir.is_dir():
            raise ConfigError(f"DEFAULT_WORKDIR does not exist: {default_workdir}")
        if not any(
            default_workdir == root or default_workdir.is_relative_to(root) for root in allowed_roots
        ):
            raise ConfigError("DEFAULT_WORKDIR must be inside ALLOWED_WORKDIRS")

        commands: dict[str, tuple[str, ...]] = {}
        known_clis = ("claude", "codex", "grok", "pi")
        for cli_name in known_clis:
            raw_command = values.get(f"CLI_{cli_name.upper()}", cli_name)
            command = tuple(shlex.split(raw_command))
            if not command:
                raise ConfigError(f"CLI_{cli_name.upper()} is empty")
            commands[cli_name] = command

        default_models = {
            cli_name: raw_value
            for cli_name in known_clis
            if (raw_value := values.get(f"DEFAULT_{cli_name.upper()}_MODEL", "").strip())
        }
        default_efforts = {
            cli_name: raw_value.lower()
            for cli_name in known_clis
            if (raw_value := values.get(f"DEFAULT_{cli_name.upper()}_EFFORT", "").strip())
        }

        raw_enabled_clis = values.get("ENABLED_CLIS", ",".join(known_clis))
        enabled_clis = tuple(
            dict.fromkeys(
                item.strip().lower()
                for item in raw_enabled_clis.replace(",", " ").split()
                if item.strip()
            )
        )
        if not enabled_clis:
            raise ConfigError("ENABLED_CLIS is empty")
        unknown_clis = [cli for cli in enabled_clis if cli not in known_clis]
        if unknown_clis:
            raise ConfigError(f"ENABLED_CLIS contains unsupported CLI: {', '.join(unknown_clis)}")

        socket_name = values.get("TMUX_SOCKET_NAME", "telegram-cli-gateway").strip()
        if not socket_name or not all(ch.isalnum() or ch in "-_" for ch in socket_name):
            raise ConfigError("TMUX_SOCKET_NAME may contain only letters, numbers, '-' and '_'")

        return cls(
            project_dir=project_dir,
            bot_token=token,
            allowed_user_ids=allowed_users,
            allow_groups=_boolean(values, "TELEGRAM_ALLOW_GROUPS", False),
            allowed_roots=allowed_roots,
            default_workdir=default_workdir,
            cli_commands=commands,
            poll_timeout=_positive_int(values, "TELEGRAM_POLL_TIMEOUT", 10),
            output_poll_interval=_positive_float(values, "OUTPUT_POLL_INTERVAL", 1.0),
            output_max_bytes=_positive_int(values, "OUTPUT_MAX_BYTES", 65536),
            tmux_socket_name=socket_name,
            enabled_clis=enabled_clis,
            stream_update_interval=_positive_float(values, "STREAM_UPDATE_INTERVAL", 10.0),
            telegram_max_file_bytes=_positive_int(
                values, "TELEGRAM_MAX_FILE_BYTES", 20 * 1024 * 1024
            ),
            auto_send_artifacts=_auto_send_mode(values, "AUTO_SEND_ARTIFACTS", "images"),
            auto_resume=_boolean(values, "AUTO_RESUME", False),
            bot_tokens=tuple(configured_tokens),
            cli_default_models=default_models,
            cli_default_efforts=default_efforts,
        )
