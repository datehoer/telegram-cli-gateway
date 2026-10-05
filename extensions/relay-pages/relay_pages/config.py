"""Settings for the publisher and the server.

Bot tokens and the Bot API endpoint come from the gateway's private .env; they are
only used to show which bot published a page.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
# extensions/relay-pages lives inside the gateway checkout.
GATEWAY_DIR = PROJECT_DIR.parent.parent

# Gateway env names → the gateway's bot keys.
BOT_TOKEN_KEYS = (
    ("default", "TELEGRAM_BOT_TOKEN"),
    ("2", "TELEGRAM_BOT_TOKEN_2"),
    ("3", "TELEGRAM_BOT_TOKEN_3"),
    ("4", "TELEGRAM_BOT_TOKEN_4"),
    ("5", "TELEGRAM_BOT_TOKEN_5"),
)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def parse_ttl(raw: str) -> int | None:
    """'7d' / '12h' / '30m' / 'forever' → seconds (None = never expires)."""
    raw = raw.strip().lower()
    if raw in {"forever", "never", "permanent", "0"}:
        return None
    units = {"m": 60, "h": 3600, "d": 86400}
    if raw[-1:] in units and raw[:-1].isdigit() and int(raw[:-1]) > 0:
        return int(raw[:-1]) * units[raw[-1]]
    raise ValueError(f"invalid TTL {raw!r}; use e.g. 7d, 12h, 30m or forever")


@dataclass(frozen=True)
class Settings:
    base_url: str
    host: str
    port: int
    runtime_dir: Path
    static_dir: Path
    gateway_dir: Path
    default_ttl: int | None
    bot_tokens: dict[str, str] = field(repr=False)
    bot_api_base: str = "https://api.telegram.org"

    @property
    def pages_dir(self) -> Path:
        return self.runtime_dir / "pages"

    @property
    def secret_path(self) -> Path:
        return self.runtime_dir / "secret.key"

    @property
    def users_path(self) -> Path:
        return self.runtime_dir / "users.json"

    def page_url(self, page_id: str) -> str:
        return f"{self.base_url}/p/{page_id}"


def load_settings(project_dir: Path = PROJECT_DIR) -> Settings:
    local = read_env_file(project_dir / ".env")

    def setting(name: str, default: str) -> str:
        return os.environ.get(name) or local.get(name) or default

    gateway_dir = Path(setting("RELAY_PAGES_GATEWAY_DIR", str(GATEWAY_DIR)))
    gateway = read_env_file(gateway_dir / ".env")
    tokens = {key: gateway[name] for key, name in BOT_TOKEN_KEYS if gateway.get(name)}
    return Settings(
        base_url=setting("RELAY_PAGES_BASE_URL", "http://127.0.0.1:8787").rstrip("/"),
        host=setting("RELAY_PAGES_HOST", "127.0.0.1"),
        port=int(setting("RELAY_PAGES_PORT", "8787")),
        runtime_dir=Path(setting("RELAY_PAGES_RUNTIME_DIR", str(project_dir / ".runtime"))),
        static_dir=project_dir / "static",
        gateway_dir=gateway_dir,
        default_ttl=parse_ttl(setting("RELAY_PAGES_DEFAULT_TTL", "7d")),
        bot_tokens=tokens,
        bot_api_base=(gateway.get("TELEGRAM_LOCAL_API_URL") or "https://api.telegram.org").rstrip("/"),
    )
