"""Username and password login for the reading pages.

Accounts live in .runtime/users.json with scrypt hashes and are managed with
`relay-page user …`. A successful login sets a signed, HttpOnly session cookie for
90 days. The cookie carries a fingerprint of the account's password hash, so a new
password logs that account out everywhere; deleting .runtime/secret.key logs out
every browser. Failed logins are limited per client address and site-wide.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SESSION_COOKIE = "rp_session"
SESSION_SECONDS = 90 * 86400
USERNAME = re.compile(r"^[a-z0-9_-]{1,32}$")
PASSWORD_MIN_LENGTH = 8
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
FAILURE_WINDOW = 15 * 60
FAILURES_PER_ADDRESS = 5
FAILURES_SITE_WIDE = 50
GENERATED_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def load_secret(path: Path) -> bytes:
    try:
        secret = path.read_bytes()
        if len(secret) >= 32:
            return secret
    except OSError:
        pass
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    return secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: bytes, message: str) -> str:
    return _b64(hmac.new(secret, message.encode(), hashlib.sha256).digest())


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(stored: str | None, password: str) -> bool:
    if not stored:
        hash_password(password)  # same cost for unknown users, so timing says nothing
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        candidate = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
            maxmem=SCRYPT_MAXMEM, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def fingerprint(stored: str) -> str:
    return hashlib.sha256(stored.encode()).hexdigest()[:12]


def generate_password() -> str:
    """Four groups of five unambiguous characters (about 115 bits)."""
    return "-".join(
        "".join(secrets.choice(GENERATED_ALPHABET) for _ in range(5)) for _ in range(4)
    )


class Users:
    """users.json, read on every check so `relay-page user …` applies without a restart."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        users = data.get("users") if isinstance(data, dict) else None
        return users if isinstance(users, dict) else {}

    def _save(self, users: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".users-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"users": users}, handle, indent=1)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def names(self) -> list[str]:
        return sorted(self._load())

    def hash_of(self, username: str) -> str | None:
        record = self._load().get(username)
        return str(record.get("hash")) if isinstance(record, dict) and record.get("hash") else None

    def set_password(self, username: str, password: str) -> None:
        if not USERNAME.match(username):
            raise ValueError("username: 1-32 characters of a-z, 0-9, _ or -")
        if len(password) < PASSWORD_MIN_LENGTH:
            raise ValueError(f"password: at least {PASSWORD_MIN_LENGTH} characters")
        with self._lock:
            users = self._load()
            users[username] = {
                "hash": hash_password(password),
                "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            self._save(users)

    def delete(self, username: str) -> bool:
        with self._lock:
            users = self._load()
            if users.pop(username, None) is None:
                return False
            self._save(users)
            return True

    def authenticate(self, username: str, password: str) -> bool:
        stored = self.hash_of(username) if USERNAME.match(username) else None
        return verify_password(stored, password)


def make_session(secret: bytes, username: str, stored_hash: str, now: float | None = None) -> str:
    expires = int((time.time() if now is None else now) + SESSION_SECONDS)
    payload = f"s2.{username}.{expires}.{fingerprint(stored_hash)}"
    return f"{payload}.{_sign(secret, payload)}"


def check_session(secret: bytes, value: str | None, users: Users, now: float | None = None) -> str | None:
    """The account behind a valid cookie; a deleted account or new password ends it."""
    if not value:
        return None
    parts = value.split(".")
    if len(parts) != 5 or parts[0] != "s2" or not USERNAME.match(parts[1]):
        return None
    payload = ".".join(parts[:4])
    if not hmac.compare_digest(_sign(secret, payload), parts[4]):
        return None
    try:
        expires = int(parts[2])
    except ValueError:
        return None
    if expires <= (time.time() if now is None else now):
        return None
    stored = users.hash_of(parts[1])
    if stored is None or not hmac.compare_digest(fingerprint(stored), parts[3]):
        return None
    return parts[1]


class FailureLimiter:
    """Failed logins per client address, plus a site-wide ceiling."""

    def __init__(
        self,
        per_address: int = FAILURES_PER_ADDRESS,
        site_wide: int = FAILURES_SITE_WIDE,
        window: float = FAILURE_WINDOW,
    ) -> None:
        self.per_address = per_address
        self.site_wide = site_wide
        self.window = window
        self._by_address: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._all and now - self._all[0] >= self.window:
            self._all.popleft()
        for address in list(self._by_address):
            failures = self._by_address[address]
            while failures and now - failures[0] >= self.window:
                failures.popleft()
            if not failures:
                del self._by_address[address]

    def retry_after(self, address: str, now: float | None = None) -> float:
        """0 when a login attempt is allowed, else seconds to wait."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            failures = self._by_address.get(address)
            if failures and len(failures) >= self.per_address:
                return self.window - (now - failures[0])
            if len(self._all) >= self.site_wide:
                return self.window - (now - self._all[0])
            return 0.0

    def record(self, address: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._by_address.setdefault(address, deque()).append(now)
            self._all.append(now)

    def clear(self, address: str) -> None:
        with self._lock:
            self._by_address.pop(address, None)
