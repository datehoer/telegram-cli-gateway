"""Read numeric usage from one native Claude Code session, without inference."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .usage import headless_usage, token_count


MAX_HISTORY_BYTES = 8 * 1024 * 1024


def _session_path(projects: Path, external_id: str, cwd: str) -> Path | None:
    # Claude encodes punctuation in project paths as hyphens, including dots.
    for directory in (cwd, str(Path(cwd).resolve())):
        candidate = projects / re.sub(r"[^a-zA-Z0-9]", "-", directory) / f"{external_id}.jsonl"
        if candidate.is_file():
            return candidate
    # A moved directory or native path-encoding change must not select another
    # conversation: search only this UUID's filename and reject ambiguity.
    matches = projects.glob(f"*/{external_id}.jsonl")
    first = next(matches, None)
    return first if next(matches, None) is None else None


def read_claude_usage(external_id: str, cwd: str) -> dict[str, Any]:
    try:
        if str(uuid.UUID(external_id)) != external_id.lower():
            return {}
    except (ValueError, AttributeError, TypeError):
        return {}
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    try:
        path = _session_path(root / "projects", external_id, cwd)
        if path is None:
            return {}
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            offset = max(0, size - MAX_HISTORY_BYTES)
            stream.seek(offset)
            lines = stream.read(MAX_HISTORY_BYTES).splitlines()
        if offset:
            lines = lines[1:]  # The first record may start outside the bounded tail.
    except OSError:
        return {}

    windows: dict[str, int] = {}
    following_chain = False
    parent: str | None = None
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue  # Includes a partial record while Claude is still writing.
        if not isinstance(value, dict) or value.get("isSidechain") or value.get("parent_tool_use_id"):
            continue
        if value.get("sessionId", external_id) != external_id:
            continue
        kind = value.get("type")
        if kind == "cost-state":
            models = value.get("modelUsage")
            if isinstance(models, dict):
                for model, metrics in models.items():
                    if isinstance(metrics, dict) and (window := token_count(metrics.get("contextWindow"))):
                        windows.setdefault(model, window)
            continue
        if kind not in {"assistant", "user", "system", "attachment", "progress"}:
            continue
        record_id = value.get("uuid")
        if isinstance(record_id, str) and "parentUuid" in value:
            if following_chain and record_id != parent:
                continue
            following_chain = True
            parent = value.get("parentUuid")
        elif following_chain:
            continue

        snapshot = headless_usage("claude", value)
        if "context_tokens" in snapshot:
            if snapshot["context_tokens"] is not None:
                snapshot["context_basis"] = "history"
                usage = value["message"]["usage"]
                for source, target in (
                    ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                    ("cache_read_input_tokens", "cache_read_tokens"),
                    ("cache_creation_input_tokens", "cache_write_tokens"),
                ):
                    if (count := token_count(usage.get(source))) is not None:
                        snapshot[target] = count
            snapshot["reported_at"] = value.get("timestamp")
            if windows:
                snapshot["model_windows"] = windows
            return snapshot
        if following_chain and not parent:
            break
    return {}
