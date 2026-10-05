"""The reading-page server (stdlib only). nginx terminates TLS and proxies here.

Unauthenticated requests never see page content: /p/<id> answers with the same login
page whether or not the id exists, and raw/files answer 404.
"""
from __future__ import annotations

import hashlib
import logging
import math
import mimetypes
import re
import threading
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import store, telegram, templates
from .auth import (
    SESSION_COOKIE,
    SESSION_SECONDS,
    FailureLimiter,
    Users,
    check_session,
    load_secret,
    make_session,
)
from .config import PROJECT_DIR, Settings

LOGGER = logging.getLogger("relay-pages")
PAGE_ROUTE = re.compile(r"^/p/([a-z0-9]{16})(/raw|/files/([0-9]{1,3}\.[a-z]{3,4}))?/?$")
NEXT_PATH = re.compile(r"^/(p/[a-z0-9]{16})?$")
FORM_MAX_BYTES = 4096
PRUNE_INTERVAL = 3600
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def asset_map(settings: Settings) -> dict[str, Path]:
    design = PROJECT_DIR / "design"
    assets = {
        "tokens.css": design / "tokens.css",
        "relay-pages.css": design / "relay-pages.css",
    }
    for path in sorted(settings.static_dir.rglob("*")):
        if path.is_file() and path.suffix in {".css", ".js", ".svg"}:
            assets[path.relative_to(settings.static_dir).as_posix()] = path
    return assets


class RelayPagesApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.secret = load_secret(settings.secret_path)
        self.users = Users(settings.users_path)
        self.failures = FailureLimiter()
        self.assets = asset_map(settings)
        digest = hashlib.sha256()
        for name in sorted(self.assets):
            digest.update(self.assets[name].read_bytes())
        templates.ASSET_VERSION["value"] = digest.hexdigest()[:10]


class Handler(BaseHTTPRequestHandler):
    server_version = "RelayPages"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: RelayPagesApp  # set on the subclass made by make_server

    # ---- plumbing -------------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:
        line = re.sub(r"\?[^\s\"]*", "", format % args)
        LOGGER.info("%s %s", self._address(), line)

    def _address(self) -> str:
        # The server only listens on loopback behind nginx, which sets X-Real-IP from
        # Cloudflare's client address.
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _send(
        self,
        status: int,
        body: bytes | str,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
        cache: str = "private, no-store",
    ) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # same-origin: no Referer leaves the site, and form posts keep their Origin.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _user(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            cookie = SimpleCookie(raw)
        except CookieError:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return check_session(self.app.secret, morsel.value if morsel else None, self.app.users)

    def _not_found(self) -> None:
        self._send(
            HTTPStatus.NOT_FOUND,
            templates.notice("Page not found", "The link may be wrong, or the page was deleted.", link=("/", "All pages")),
        )

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return origin in (None, "null") or origin.rstrip("/") == self.app.settings.base_url

    def _read_form(self) -> dict[str, str] | None:
        if not (self.headers.get("Content-Type") or "").startswith("application/x-www-form-urlencoded"):
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > FORM_MAX_BYTES:
            return None
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return {key: values[0] for key, values in parse_qs(raw, keep_blank_values=True).items()}

    # ---- routes ---------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send(HTTPStatus.OK, "ok\n", "text/plain; charset=utf-8")
            return
        if path.startswith("/assets/"):
            self._asset(path.removeprefix("/assets/"))
            return
        if path == "/":
            if self._user() is None:
                self._send(HTTPStatus.UNAUTHORIZED, templates.login("/"))
                return
            self._send(HTTPStatus.OK, templates.index(store.list_pages(self.app.settings)))
            return
        route = PAGE_ROUTE.match(path)
        if route is None:
            self._not_found()
            return
        page_id, suffix, file_name = route.group(1), route.group(2), route.group(3)
        if self._user() is None:
            if suffix:
                self._not_found()
            else:
                self._send(HTTPStatus.UNAUTHORIZED, templates.login(f"/p/{page_id}"))
            return
        self._page(page_id, suffix, file_name)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/auth/login":
            self._login()
        elif path == "/auth/logout":
            self._logout()
        else:
            self._not_found()

    # ---- handlers -------------------------------------------------------------
    def _asset(self, name: str) -> None:
        path = self.app.assets.get(name)
        if path is None:
            self._not_found()
            return
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, path.read_bytes(), content_type, cache="public, max-age=86400")

    def _page(self, page_id: str, suffix: str | None, file_name: str | None) -> None:
        settings = self.app.settings
        meta = store.load_meta(settings, page_id)
        if meta is None:
            self._not_found()
            return
        if store.is_expired(meta):
            if suffix:
                self._not_found()
            else:
                self._send(HTTPStatus.GONE, templates.expired(meta))
            return
        if suffix == "/raw":
            source = store.read_text(settings, page_id, "source.md")
            if source is None:
                self._not_found()
                return
            self._send(HTTPStatus.OK, source, "text/plain; charset=utf-8")
            return
        if file_name:
            path = store.file_path(settings, page_id, file_name)
            if path is None:
                self._not_found()
                return
            content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, path.read_bytes(), content_type, cache="private, max-age=3600")
            return
        body = store.read_text(settings, page_id, "body.html")
        source = store.read_text(settings, page_id, "source.md")
        if body is None or source is None:
            self._send(HTTPStatus.GONE, templates.expired(meta))
            return
        self._send(HTTPStatus.OK, templates.page(meta, body, source))

    def _login(self) -> None:
        if not self._same_origin():
            self._send(HTTPStatus.FORBIDDEN, templates.login("/", error="Please sign in from the sign-in page."))
            return
        form = self._read_form()
        if form is None:
            self._send(HTTPStatus.BAD_REQUEST, templates.login("/", error="Please sign in from the sign-in page."))
            return
        username = form.get("username", "").strip().lower()
        password = form.get("password", "")
        next_path = form.get("next", "/")
        next_path = next_path if NEXT_PATH.match(next_path) else "/"
        address = self._address()
        wait = self.app.failures.retry_after(address)
        if wait:
            minutes = max(1, math.ceil(wait / 60))
            self._send(
                HTTPStatus.TOO_MANY_REQUESTS,
                templates.login(next_path, error=f"Too many attempts. Try again in {minutes} minute{'' if minutes == 1 else 's'}.", username=username),
                headers={"Retry-After": str(math.ceil(wait))},
            )
            return
        stored = self.app.users.hash_of(username) if username else None
        if stored is None or not self.app.users.authenticate(username, password):
            self.app.failures.record(address)
            LOGGER.warning("failed login from %s", address)
            self._send(
                HTTPStatus.UNAUTHORIZED,
                templates.login(next_path, error="Wrong username or password.", username=username),
            )
            return
        self.app.failures.clear(address)
        cookie = (
            f"{SESSION_COOKIE}={make_session(self.app.secret, username, stored)}; Path=/; "
            f"Max-Age={SESSION_SECONDS}; HttpOnly; Secure; SameSite=Lax"
        )
        self._send(HTTPStatus.SEE_OTHER, "", headers={"Location": next_path, "Set-Cookie": cookie})

    def _logout(self) -> None:
        if not self._same_origin():
            self._send(HTTPStatus.FORBIDDEN, templates.login("/"))
            return
        cookie = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
        self._send(HTTPStatus.SEE_OTHER, "", headers={"Location": "/", "Set-Cookie": cookie})


def _prune_loop(settings: Settings, stop: threading.Event) -> None:
    while not stop.wait(PRUNE_INTERVAL):
        try:
            purged, removed = store.prune(settings)
            if purged or removed:
                LOGGER.info("pruned: %d expired, %d forgotten", purged, removed)
        except OSError:
            LOGGER.exception("prune failed")


def make_server(settings: Settings) -> ThreadingHTTPServer:
    app = RelayPagesApp(settings)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    server.daemon_threads = True
    return server


def serve(settings: Settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store.prune(settings)
    try:
        telegram.bot_identities(settings, refresh=True)
    except OSError:
        LOGGER.warning("could not refresh bot identities")
    server = make_server(settings)
    if not server.RequestHandlerClass.app.users.names():
        LOGGER.warning("no accounts yet: run `relay-page user set <name>`")
    stop = threading.Event()
    threading.Thread(target=_prune_loop, args=(settings, stop), name="prune", daemon=True).start()
    LOGGER.info("relay-pages listening on %s:%d for %s", settings.host, settings.port, settings.base_url)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
