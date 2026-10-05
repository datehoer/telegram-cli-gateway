"""Markdown → sanitized Relay Pages body HTML.

Bot output is untrusted. Raw HTML is disabled in markdown-it (it is escaped, and
unsafe link schemes are dropped), the result is cleaned by nh3 against an allowlist,
and the server's CSP forbids inline script. Components follow design/components/*.md.
"""
from __future__ import annotations

import html
import math
import re
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import nh3
from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.gfm import gfm_plugin
from pygments import lex
from pygments.lexers import get_lexer_by_name
from pygments.token import Comment, Keyword, Number, String
from pygments.util import ClassNotFound

TITLE_FALLBACK_CHARS = 40
DESCRIPTION_CHARS = 120
READING_UNITS_PER_MINUTE = 400  # CJK characters a minute (PageHeader.md); an English word counts as 1.6
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}
IMAGE_MAX_BYTES = 10 * 1024 * 1024
IMAGE_MAX_COUNT = 30

# GitHub alert kind → (callout class, label, icon). Callout.md maps NOTE/TIP/WARNING/CAUTION.
CALLOUTS = {
    "NOTE": ("", "Note", "info"),
    "IMPORTANT": ("", "Important", "info"),
    "TIP": ("ok", "Tip", "check"),
    "WARNING": ("warn", "Warning", "warn"),
    "CAUTION": ("danger", "Caution", "danger"),
}

NUMERIC_CELL = re.compile(
    r"^[-+−~≈<>≤≥]?\s*[$¥€£]?\d[\d,.]*\s*(?:%|[A-Za-zµ°/]+)?"
    r"(?:\s+\d[\d,.]*\s*(?:%|[A-Za-zµ°/]+)?)*$"
)
CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")
WORD = re.compile(r"[A-Za-z0-9_]+")

ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "code", "del", "div", "em", "h2", "h3", "h4", "hr",
    "i", "img", "input", "kbd", "li", "mark", "ol", "p", "pre", "s", "section", "span",
    "strong", "sub", "sup", "table", "tbody", "td", "th", "thead", "tr", "ul",
}
ALLOWED_ATTRIBUTES = {
    "*": {"class", "id"},
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title", "loading"},
    "input": {"type", "checked", "disabled"},
    "div": {"data-lines"},
    "span": {"aria-hidden"},
    "ol": {"start"},
}


class RenderError(ValueError):
    pass


@dataclass
class Rendered:
    title: str
    body_html: str
    toc: list[dict[str, Any]]
    text: str
    reading_minutes: int
    description: str
    images: list[tuple[Path, str]] = field(default_factory=list)


def _mark_plugin(md: MarkdownIt) -> None:
    """==text== → <mark> (DESIGN.md Markdown mapping)."""

    def mark(state: Any, silent: bool) -> bool:
        start = state.pos
        src = state.src
        if not src.startswith("==", start) or src.startswith("===", start):
            return False
        end = src.find("==", start + 2)
        if end < 0 or end + 2 > state.posMax:
            return False
        inner = src[start + 2 : end]
        if not inner.strip() or inner[0].isspace() or inner[-1].isspace():
            return False
        if not silent:
            old_max = state.posMax
            state.pos = start + 2
            state.posMax = end
            state.push("mark_open", "mark", 1)
            state.md.inline.tokenize(state)
            state.push("mark_close", "mark", -1)
            state.posMax = old_max
        state.pos = end + 2
        return True

    md.inline.ruler.before("emphasis", "mark", mark)


def highlight(code: str, lang: str) -> str:
    """Four colours only (CodeBlock.md): comment, keyword, string, number."""
    try:
        lexer = get_lexer_by_name(lang, stripnl=False, ensurenl=False) if lang else None
    except ClassNotFound:
        lexer = None
    if lexer is None:
        return html.escape(code, quote=False)
    runs: list[list[str]] = []  # [class, text]; adjacent tokens of one colour merge
    for token_type, value in lex(code, lexer):
        if token_type in Comment:
            css = "tok-c"
        elif token_type in Keyword:
            css = "tok-k"
        elif token_type in String:
            css = "tok-s"
        elif token_type in Number:
            css = "tok-n"
        else:
            css = ""
        if runs and runs[-1][0] == css:
            runs[-1][1] += value
        else:
            runs.append([css, value])
    return "".join(
        f'<span class="{css}">{html.escape(text, quote=False)}</span>' if css else html.escape(text, quote=False)
        for css, text in runs
    )


def _render_code(code: str, info: str) -> str:
    lang = (info.strip().split() or [""])[0]
    code = code.removesuffix("\n")
    lines = code.count("\n") + 1
    label = html.escape(lang or "text")
    return (
        f'<div class="rp-code" data-lines="{lines}">'
        f'<div class="rp-code-bar"><span class="rp-code-lang">{label}</span></div>'
        f"<pre><code>{highlight(code, lang.lower())}</code></pre></div>\n"
    )


@lru_cache(maxsize=1)
def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "breaks": True, "typographer": False})
    md.use(gfm_plugin)
    md.use(_mark_plugin)
    rules = md.add_render_rule

    def fence(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        return _render_code(tokens[idx].content, tokens[idx].info)

    def code_block(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        return _render_code(tokens[idx].content, "")

    def alert_open(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        kind = str(tokens[idx].meta.get("kind", "NOTE")).upper()
        css, label, icon = CALLOUTS.get(kind, CALLOUTS["NOTE"])
        classes = f"rp-callout {css}".strip()
        return (
            f'<div class="{classes}"><div class="rp-callout-label">'
            f'<span class="rp-icon rp-icon-{icon}" aria-hidden="true"></span>{label}</div>\n'
        )

    def alert_close(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        return "</div>\n"

    def table_open(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        return '<div class="rp-table-wrap"><table class="rp-table">\n'

    def table_close(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        return "</table></div>\n"

    def link_open(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        href = str(tokens[idx].attrGet("href") or "")
        if href.startswith(("http://", "https://")):
            tokens[idx].attrSet("target", "_blank")
        return self.renderToken(tokens, idx, options, env)

    def image(self: Any, tokens: list[Token], idx: int, options: Any, env: Any) -> str:
        token = tokens[idx]
        src = html.escape(str(token.attrGet("src") or ""))
        alt = html.escape(self.renderInlineAsText(token.children or [], options, env))
        return f'<img src="{src}" alt="{alt}" loading="lazy">'

    rules("fence", fence)
    rules("code_block", code_block)
    rules("alert_open", alert_open)
    rules("alert_close", alert_close)
    rules("table_open", table_open)
    rules("table_close", table_close)
    rules("link_open", link_open)
    rules("image", image)
    return md


def _inline_text(token: Token) -> str:
    parts: list[str] = []
    for child in token.children or []:
        if child.type in {"text", "code_inline"}:
            parts.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append(" ")
    return "".join(parts).strip()


def _drop_alert_titles(tokens: list[Token]) -> list[Token]:
    """The callout label replaces markdown-it's English "Warning" title paragraph."""
    kept: list[Token] = []
    skipping = False
    for token in tokens:
        if token.type == "alert_title_open":
            skipping = True
            continue
        if token.type == "alert_title_close":
            skipping = False
            continue
        if not skipping:
            kept.append(token)
    return kept


def _take_title(tokens: list[Token], explicit: str | None) -> tuple[str | None, list[Token]]:
    """First `# ` heading becomes the page title and leaves the body (PageHeader.md)."""
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag != "h1":
            continue
        if explicit is not None and index != 0:
            break
        title = _inline_text(tokens[index + 1])
        return (explicit or title or None), tokens[:index] + tokens[index + 3 :]
    return explicit, tokens


def _number_headings(tokens: list[Token]) -> list[dict[str, Any]]:
    toc: list[dict[str, Any]] = []
    count = 0
    for index, token in enumerate(tokens):
        if token.type not in {"heading_open", "heading_close"}:
            continue
        level = int(token.tag[1])
        level = 2 if level == 1 else min(level, 4)  # one h1 per page; h5/h6 render as h4
        token.tag = f"h{level}"
        if token.type == "heading_open":
            count += 1
            token.attrSet("id", f"s{count}")
            if level in {2, 3}:
                toc.append({"level": level, "text": _inline_text(tokens[index + 1]), "id": f"s{count}"})
    return toc


def _style_tables(tokens: list[Token]) -> None:
    """Alignment classes instead of inline styles (the CSP blocks style attributes);
    numeric columns get .num (Table.md)."""
    index = 0
    while index < len(tokens):
        if tokens[index].type != "table_open":
            index += 1
            continue
        end = index
        while tokens[end].type != "table_close":
            end += 1
        cells: list[tuple[Token, int, bool, str]] = []
        column = 0
        for position in range(index, end):
            token = tokens[position]
            if token.type == "tr_open":
                column = 0
            elif token.type in {"th_open", "td_open"}:
                text = _inline_text(tokens[position + 1])
                cells.append((token, column, token.type == "th_open", text))
                column += 1
        numeric: dict[int, bool] = {}
        for _token, col, is_header, text in cells:
            if is_header or not text:
                continue
            numeric[col] = numeric.get(col, True) and bool(NUMERIC_CELL.match(text))
        for token, col, _is_header, _text in cells:
            style = str(token.attrGet("style") or "")
            classes = []
            if "right" in style or numeric.get(col):
                classes.append("num")
            elif "center" in style:
                classes.append("center")
            token.attrs.pop("style", None)
            if classes:
                token.attrSet("class", " ".join(classes))
        index = end + 1


def _collect_images(
    tokens: list[Token], page_id: str, base_dir: Path | None
) -> list[tuple[Path, str]]:
    """Local images are copied into the page so the browser can load them."""
    images: list[tuple[Path, str]] = []
    seen: dict[Path, str] = {}

    def visit(children: list[Token]) -> None:
        for child in children:
            if child.children:
                visit(child.children)
            if child.type != "image":
                continue
            src = unquote(str(child.attrGet("src") or ""))  # markdown-it percent-encodes
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", src) or src.startswith("//"):
                continue  # remote (http/https/data…) stays as written
            path = Path(src) if src.startswith("/") else (base_dir / src if base_dir else None)
            try:
                path = path.resolve(strict=True) if path is not None else None
            except OSError:
                path = None
            if path is not None and path in seen:
                child.attrSet("src", f"/p/{page_id}/files/{seen[path]}")
                continue
            if (
                path is None
                or not path.is_file()
                or path.suffix.lower() not in IMAGE_SUFFIXES
                or path.stat().st_size > IMAGE_MAX_BYTES
                or len(images) >= IMAGE_MAX_COUNT
            ):
                # A local path the browser could never load: keep the alt text only.
                alt = "".join(item.content for item in child.children or []) or Path(src).name
                child.type, child.tag, child.attrs, child.children = "text", "", {}, None
                child.content = f"[image: {alt}]"
                continue
            name = f"{len(images) + 1}{path.suffix.lower()}"
            seen[path] = name
            images.append((path, name))
            child.attrSet("src", f"/p/{page_id}/files/{name}")

    for token in tokens:
        if token.type == "inline" and token.children:
            visit(token.children)
    return images


def _plain_text(tokens: list[Token]) -> str:
    parts: list[str] = []
    for token in tokens:
        if token.type == "inline":
            parts.append(_inline_text(token))
        elif token.type in {"fence", "code_block"}:
            parts.append(token.content)
    return "\n".join(part for part in parts if part)


def reading_minutes(text: str) -> int:
    cjk = len(CJK.findall(text))
    words = len(WORD.findall(CJK.sub(" ", text)))
    return max(1, math.ceil((cjk + words * 1.6) / READING_UNITS_PER_MINUTE))


def _fallback_title(text: str) -> str:
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line if len(line) <= TITLE_FALLBACK_CHARS else line[: TITLE_FALLBACK_CHARS - 1] + "…"
    return "Untitled"


def sanitize(body: str) -> str:
    return nh3.clean(
        body,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


def render_markdown(
    source: str,
    *,
    page_id: str,
    title: str | None = None,
    base_dir: Path | None = None,
) -> Rendered:
    md = _markdown()
    env: dict[str, Any] = {}
    tokens = _drop_alert_titles(md.parse(source, env))
    found_title, tokens = _take_title(tokens, title.strip() if title and title.strip() else None)
    toc = _number_headings(tokens)
    _style_tables(tokens)
    images = _collect_images(tokens, page_id, base_dir)
    body = sanitize(md.renderer.render(tokens, md.options, env))
    text = _plain_text(tokens)
    flat = re.sub(r"\s+", " ", text).strip()
    description = flat if len(flat) <= DESCRIPTION_CHARS else flat[: DESCRIPTION_CHARS - 1] + "…"
    return Rendered(
        title=found_title or _fallback_title(text),
        body_html=body,
        toc=toc,
        text=text,
        reading_minutes=reading_minutes(text),
        description=description,
        images=images,
    )


def copy_images(images: list[tuple[Path, str]], destination: Path) -> None:
    if not images:
        return
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    for source, name in images:
        target = destination / name
        shutil.copyfile(source, target)
        target.chmod(0o600)
