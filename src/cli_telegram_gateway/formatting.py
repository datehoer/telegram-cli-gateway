from __future__ import annotations

import html
import re
from urllib.parse import urlsplit


FENCE_RE = re.compile(r"```([A-Za-z0-9_+.-]*)[ \t]*\n([\s\S]*?)(?:```|\Z)")
FENCE_MARKER_RE = re.compile(r"```([A-Za-z0-9_+.-]*)[^\n]*")
INLINE_TOKEN_RE = re.compile(r"`([^`\n]+)`|\[([^\]\n]+)\]\(([^\s)]+)\)")


def stream_segment(text: str, start: int = 0, limit: int = 1500) -> tuple[str, int]:
    """Return one short, fenced Markdown segment and its raw source cursor.

    The cursor counts Python characters, whereas the budget counts UTF-16 units.
    Added code fences must never count toward the consumed source text.
    """
    if limit < 32:
        raise ValueError("stream segment limit must be at least 32")
    start = min(max(0, start), len(text))
    if start == len(text):
        return "", start
    language: str | None = None
    for marker in FENCE_MARKER_RE.finditer(text, 0, start):
        language = None if language is not None else marker.group(1)[:64]
    prefix = f"```{language}\n" if language is not None else ""
    # Reserve room for a closing fence, including when the source opens one.
    budget = limit - len(prefix.encode("utf-16-le")) // 2 - 4
    end = start
    units = 0
    while end < len(text):
        size = 2 if ord(text[end]) > 0xFFFF else 1
        if units + size > budget:
            break
        units += size
        end += 1
    if end < len(text):
        paragraph = text.rfind("\n\n", start, end)
        newline = text.rfind("\n", start, end)
        cut = paragraph + 2 if paragraph >= start + (end - start) // 2 else newline + 1
        if cut >= start + (end - start) // 2:
            end = cut
        # Do not cut a fence marker or its language name in two.
        for marker in FENCE_MARKER_RE.finditer(text, start):
            if marker.start() >= end:
                break
            if marker.start() < end < marker.end() and marker.start() > start:
                end = marker.start()
                break
    raw = text[start:end]
    for marker in FENCE_MARKER_RE.finditer(raw):
        language = None if language is not None else marker.group(1)[:64]
    suffix = "\n```" if language is not None else ""
    return prefix + raw + suffix, end


def _safe_link(url: str) -> str | None:
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return None
    return url if scheme in {"http", "https", "tg", "mailto"} else None


def _render_emphasis(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"~~(.+?)~~", r"<s>\1</s>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", escaped)
    return escaped


def _render_inline(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in INLINE_TOKEN_RE.finditer(text):
        parts.append(_render_emphasis(text[cursor : match.start()]))
        inline_code, label, url = match.groups()
        if inline_code is not None:
            parts.append(f"<code>{html.escape(inline_code, quote=False)}</code>")
        else:
            safe_url = _safe_link(url or "")
            if safe_url:
                parts.append(
                    f'<a href="{html.escape(safe_url, quote=True)}">'
                    f"{_render_emphasis(label or '')}</a>"
                )
            else:
                parts.append(_render_emphasis(match.group(0)))
        cursor = match.end()
    parts.append(_render_emphasis(text[cursor:]))
    return "".join(parts)


def _render_text_block(text: str) -> str:
    rendered: list[str] = []
    quote_lines: list[str] = []

    def flush_quote() -> None:
        if quote_lines:
            rendered.append("<blockquote>" + "\n".join(quote_lines) + "</blockquote>")
            quote_lines.clear()

    for line in text.splitlines(keepends=True):
        has_newline = line.endswith("\n")
        body = line[:-1] if has_newline else line
        quote = re.match(r"^\s*>\s?(.*)$", body)
        if quote:
            quote_lines.append(_render_inline(quote.group(1)))
            continue
        flush_quote()
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+)$", body)
        bullet = re.match(r"^(\s*)[-+*]\s+(.+)$", body)
        if heading:
            rendered.append(f"<b>{_render_inline(heading.group(1))}</b>")
        elif bullet:
            rendered.append(f"{bullet.group(1)}• {_render_inline(bullet.group(2))}")
        else:
            rendered.append(_render_inline(body))
        if has_newline:
            rendered.append("\n")
    flush_quote()
    return "".join(rendered)


def markdown_to_telegram_html(markdown: str) -> str:
    """Convert a safe, streaming-friendly Markdown subset to Telegram HTML."""
    output: list[str] = []
    cursor = 0
    for match in FENCE_RE.finditer(markdown):
        output.append(_render_text_block(markdown[cursor : match.start()]))
        language = match.group(1)
        code = html.escape(match.group(2).rstrip("\n"), quote=False)
        if language:
            safe_language = re.sub(r"[^A-Za-z0-9_+.-]", "", language)
            output.append(f'<pre><code class="language-{safe_language}">{code}</code></pre>')
        else:
            output.append(f"<pre>{code}</pre>")
        cursor = match.end()
    output.append(_render_text_block(markdown[cursor:]))
    return "".join(output)


def split_markdown(markdown: str, limit: int = 3400) -> list[str]:
    """Split Markdown while balancing fenced code blocks across Telegram messages."""
    from .telegram import split_message

    raw_chunks = split_message(markdown, limit=limit)
    chunks: list[str] = []
    in_fence = False
    language = ""
    for raw_chunk in raw_chunks:
        prefix = f"```{language}\n" if in_fence else ""
        for marker in FENCE_MARKER_RE.finditer(raw_chunk):
            if in_fence:
                in_fence = False
                language = ""
            else:
                in_fence = True
                language = marker.group(1)
        suffix = "\n```" if in_fence else ""
        chunks.append(prefix + raw_chunk + suffix)
    return chunks


def split_rich_markdown(markdown: str, limit: int = 30000) -> list[str]:
    """Keep rich messages below Telegram's 32768-character limit."""
    return split_markdown(markdown, limit=limit)


def harden_rich_markdown(text: str) -> str:
    """Mark single newlines as hard breaks so an extension's own layout survives.

    Telegram's Rich Message renderer treats a single \n inside a paragraph as a soft
    break and joins the lines (observed: multi-line help text became
    "Usage: /bili <URL> /bili login /bili whoami"). Two trailing spaces or a trailing
    backslash render as a real line break. Code blocks keep their newlines and are skipped.
    """
    if not text:
        return text
    lines = text.split("\n")
    rendered: list[str] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            rendered.append(line)
            continue
        if not in_fence and line.strip():
            rendered.append(line.rstrip() + "  ")
        else:
            rendered.append(line)
    return "\n".join(rendered)
