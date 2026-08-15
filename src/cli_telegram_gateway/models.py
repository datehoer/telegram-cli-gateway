"""Per-CLI model and effort catalog.

The gateway does not own model routing; it only surfaces what each native CLI
already offers and passes the user's selection through verbatim at turn start.
Listing is best-effort: a CLI without a machine-readable catalog (Claude) gets a
fixed alias list, and any listing failure degrades to an empty list so the user
can still type a model name directly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

LOGGER = logging.getLogger("telegram-cli-gateway.models")

# Claude Code resolves these aliases to the latest model (see `claude --help`).
CLAUDE_MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")

# Reasoning/effort levels each CLI accepts. Values are passed through verbatim;
# the CLI reports an error if the selected model does not support a level.
EFFORTS: dict[str, tuple[str, ...]] = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "grok": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    "pi": ("off", "minimal", "low", "medium", "high", "xhigh", "max"),
}

_MODEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def list_efforts(cli: str) -> list[str]:
    return list(EFFORTS.get(cli, ()))


def list_models(cli: str, command: tuple[str, ...]) -> list[str]:
    """Return model identifiers the given CLI offers, or [] if unavailable."""
    if cli == "claude":
        return list(CLAUDE_MODEL_ALIASES)
    if cli == "codex":
        return _list_codex_models()
    if cli == "grok":
        return _list_grok_models(command)
    if cli == "pi":
        return _list_pi_models(command)
    return []


def _run_listing(command: tuple[str, ...], timeout: float = 20.0) -> str:
    try:
        result = subprocess.run(
            [*command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.warning("model listing failed for %s: %s", command[0], exc)
        return ""
    if result.stderr:
        LOGGER.debug("%s model listing stderr: %s", command[0], result.stderr.strip()[:400])
    return result.stdout


def _codex_cache_path() -> Path:
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(home) / "models_cache.json"


def _list_codex_models() -> list[str]:
    path = _codex_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("could not read Codex model cache %s: %s", path, exc)
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    slugs: list[str] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        # "hide" entries are non-interactive helpers (auto-review, watermark).
        if isinstance(slug, str) and item.get("visibility", "list") == "list" and slug:
            slugs.append(slug)
    return slugs


def _list_grok_models(command: tuple[str, ...]) -> list[str]:
    output = _run_listing((*command, "models"))
    models: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(
            ("Default model", "Available models", "You are logged")
        ):
            continue
        candidate = stripped.lstrip("*").lstrip("-").strip()
        candidate = re.sub(r"\s*\(.*\)\s*$", "", candidate).strip()
        if _MODEL_TOKEN_RE.fullmatch(candidate) and candidate not in models:
            models.append(candidate)
    return models


def _list_pi_models(command: tuple[str, ...]) -> list[str]:
    output = _run_listing((*command, "--list-models"))
    models: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("provider"):
            continue
        tokens = stripped.split()
        if len(tokens) < 2:
            continue
        provider, model = tokens[0], tokens[1]
        # pi's --model accepts "provider/id"; keeping the provider prefix makes
        # the selection unambiguous when several providers offer the same id.
        candidate = f"{provider}/{model}"
        if _MODEL_TOKEN_RE.fullmatch(candidate) and candidate not in models:
            models.append(candidate)
    return models
