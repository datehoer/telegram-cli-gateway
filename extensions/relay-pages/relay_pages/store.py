"""Pages on disk: .runtime/pages/<id>/{meta.json, body.html, source.md, files/}.

Everything is private to the service user (0700 dirs, 0600 files); only the server,
after authentication, reads it back. Expired pages lose their content at the next
prune but keep meta.json for EXPIRED_META_DAYS so the link still says it expired.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .render import RenderError, copy_images, render_markdown

PAGE_ID = re.compile(r"^[a-z0-9]{16}$")
FILE_NAME = re.compile(r"^[0-9]{1,3}\.(png|jpg|jpeg|gif|webp|avif)$")
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
SOURCE_MAX_BYTES = 2 * 1024 * 1024
EXPIRED_META_DAYS = 30


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def new_page_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(16))


def _write_private(path: Path, data: str) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def page_dir(settings: Settings, page_id: str) -> Path | None:
    return settings.pages_dir / page_id if PAGE_ID.match(page_id) else None


def publish(
    settings: Settings,
    source: str,
    *,
    title: str | None = None,
    ttl_seconds: int | None,
    bot: dict[str, Any] | None = None,
    author: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    if len(source.encode("utf-8")) > SOURCE_MAX_BYTES:
        raise RenderError(f"Markdown is larger than {SOURCE_MAX_BYTES // 1024 // 1024} MB")
    if not source.strip():
        raise RenderError("Markdown is empty")
    settings.pages_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    page_id = new_page_id()
    while (settings.pages_dir / page_id).exists():
        page_id = new_page_id()
    rendered = render_markdown(source, page_id=page_id, title=title, base_dir=base_dir)
    created = now_utc()
    meta = {
        "id": page_id,
        "title": rendered.title,
        "created_at": iso(created),
        "expires_at": iso(created + timedelta(seconds=ttl_seconds)) if ttl_seconds else None,
        "bot": bot,
        "author": author,
        "reading_minutes": rendered.reading_minutes,
        "description": rendered.description,
        "toc": rendered.toc,
        "chars": len(rendered.text),
        "purged": False,
    }
    staging = Path(tempfile.mkdtemp(dir=settings.pages_dir, prefix=".new-"))
    try:
        os.chmod(staging, 0o700)
        copy_images(rendered.images, staging / "files")
        _write_private(staging / "source.md", source)
        _write_private(staging / "body.html", rendered.body_html)
        _write_private(staging / "meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
        os.replace(staging, settings.pages_dir / page_id)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return meta


def load_meta(settings: Settings, page_id: str) -> dict[str, Any] | None:
    directory = page_dir(settings, page_id)
    if directory is None:
        return None
    try:
        return json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_expired(meta: dict[str, Any], moment: datetime | None = None) -> bool:
    expires = parse_iso(meta.get("expires_at"))
    return bool(meta.get("purged")) or (expires is not None and expires <= (moment or now_utc()))


def read_text(settings: Settings, page_id: str, name: str) -> str | None:
    directory = page_dir(settings, page_id)
    if directory is None or name not in {"body.html", "source.md"}:
        return None
    try:
        return (directory / name).read_text(encoding="utf-8")
    except OSError:
        return None


def file_path(settings: Settings, page_id: str, name: str) -> Path | None:
    directory = page_dir(settings, page_id)
    if directory is None or not FILE_NAME.match(name):
        return None
    path = directory / "files" / name
    return path if path.is_file() else None


def list_pages(settings: Settings) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    if not settings.pages_dir.is_dir():
        return pages
    for directory in settings.pages_dir.iterdir():
        meta = load_meta(settings, directory.name)
        if meta is not None:
            pages.append(meta)
    pages.sort(key=lambda meta: meta.get("created_at") or "", reverse=True)
    return pages


def delete_page(settings: Settings, page_id: str) -> bool:
    directory = page_dir(settings, page_id)
    if directory is None or not directory.is_dir():
        return False
    shutil.rmtree(directory)
    return True


def prune(settings: Settings, moment: datetime | None = None) -> tuple[int, int]:
    """Drop the content of expired pages; forget them EXPIRED_META_DAYS later."""
    moment = moment or now_utc()
    purged = removed = 0
    for meta in list_pages(settings):
        expires = parse_iso(meta.get("expires_at"))
        if expires is None or expires > moment:
            continue
        directory = settings.pages_dir / meta["id"]
        if moment - expires > timedelta(days=EXPIRED_META_DAYS):
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
            continue
        if not meta.get("purged"):
            for name in ("body.html", "source.md"):
                (directory / name).unlink(missing_ok=True)
            shutil.rmtree(directory / "files", ignore_errors=True)
            meta["purged"] = True
            _write_private(directory / "meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
            purged += 1
    return purged, removed
