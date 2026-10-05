from __future__ import annotations

import json
import stat
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from relay_pages import store
from relay_pages.auth import (
    FailureLimiter,
    Users,
    check_session,
    generate_password,
    hash_password,
    load_secret,
    make_session,
    verify_password,
)
from relay_pages.config import Settings, parse_ttl


def make_settings(root: Path) -> Settings:
    return Settings(
        base_url="https://pages.test",
        host="127.0.0.1",
        port=0,
        runtime_dir=root / ".runtime",
        static_dir=Path(__file__).resolve().parent.parent / "static",
        gateway_dir=root / "gateway",
        default_ttl=7 * 86400,
        bot_tokens={"default": "token-default", "2": "token-2"},
    )


class PasswordTests(unittest.TestCase):
    def test_scrypt_hashes_are_salted_and_verify(self) -> None:
        first, second = hash_password("correct horse"), hash_password("correct horse")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("scrypt$32768$8$1$"))
        self.assertTrue(verify_password(first, "correct horse"))
        self.assertFalse(verify_password(first, "correct horsE"))
        self.assertFalse(verify_password(None, "anything"))
        self.assertFalse(verify_password("md5$abc", "anything"))

    def test_generated_passwords_are_long_and_unambiguous(self) -> None:
        password = generate_password()
        self.assertRegex(password, r"^[a-zA-Z2-9]{5}(-[a-zA-Z2-9]{5}){3}$")
        self.assertFalse(set(password) & set("0O1lI"))
        self.assertNotEqual(password, generate_password())

    def test_user_store_is_private_and_validates_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            users = Users(Path(temporary) / "rt" / "users.json")
            users.set_password("owner", "long enough")
            self.assertEqual(users.names(), ["owner"])
            self.assertEqual(stat.S_IMODE(users.path.stat().st_mode), 0o600)
            self.assertNotIn("long enough", users.path.read_text())
            self.assertTrue(users.authenticate("owner", "long enough"))
            self.assertFalse(users.authenticate("owner", "wrong one!"))
            self.assertFalse(users.authenticate("nobody", "long enough"))
            self.assertFalse(users.authenticate("../etc", "long enough"))
            with self.assertRaises(ValueError):
                users.set_password("Bad Name", "long enough")
            with self.assertRaises(ValueError):
                users.set_password("owner", "short")
            self.assertTrue(users.delete("owner"))
            self.assertFalse(users.delete("owner"))


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.users = Users(Path(self.temporary.name) / "users.json")
        self.users.set_password("owner", "first password")
        self.secret = b"k" * 32

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cookie(self, now: float = 1000) -> str:
        return make_session(self.secret, "owner", self.users.hash_of("owner") or "", now=now)

    def test_valid_cookie_tampering_and_expiry(self) -> None:
        cookie = self.cookie()
        self.assertEqual(check_session(self.secret, cookie, self.users, now=1001), "owner")
        self.users.set_password("other", "other password")
        self.assertIsNone(check_session(self.secret, cookie.replace("s2.owner.", "s2.other."), self.users, now=1001))
        self.assertIsNone(check_session(b"x" * 32, cookie, self.users, now=1001))
        self.assertIsNone(check_session(self.secret, cookie, self.users, now=1000 + 91 * 86400))
        self.assertIsNone(check_session(self.secret, "garbage", self.users))
        self.assertIsNone(check_session(self.secret, None, self.users))

    def test_new_password_or_deleted_account_ends_sessions(self) -> None:
        cookie = self.cookie()
        self.users.set_password("owner", "second password")
        self.assertIsNone(check_session(self.secret, cookie, self.users, now=1001))
        fresh = self.cookie()
        self.assertEqual(check_session(self.secret, fresh, self.users, now=1001), "owner")
        self.users.delete("owner")
        self.assertIsNone(check_session(self.secret, fresh, self.users, now=1001))

    def test_failed_logins_are_limited_per_address_and_site_wide(self) -> None:
        limiter = FailureLimiter(per_address=3, site_wide=5, window=900)
        for second in range(3):
            self.assertEqual(limiter.retry_after("1.1.1.1", now=second), 0)
            limiter.record("1.1.1.1", now=second)
        self.assertAlmostEqual(limiter.retry_after("1.1.1.1", now=10), 890)
        self.assertEqual(limiter.retry_after("2.2.2.2", now=10), 0)
        limiter.record("2.2.2.2", now=11)
        limiter.record("3.3.3.3", now=12)
        self.assertGreater(limiter.retry_after("4.4.4.4", now=13), 0)  # site-wide ceiling
        self.assertEqual(limiter.retry_after("1.1.1.1", now=901), 0)  # window passed
        limiter.record("5.5.5.5", now=902)
        limiter.clear("5.5.5.5")
        self.assertEqual(limiter.retry_after("5.5.5.5", now=903), 0)

    def test_secret_is_created_private_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rt" / "secret.key"
            first = load_secret(path)
            self.assertEqual(load_secret(path), first)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class StoreTests(unittest.TestCase):
    def test_publish_writes_private_files_and_lists_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = make_settings(Path(temporary))
            first = store.publish(settings, "# One\n\nbody", ttl_seconds=60)
            second = store.publish(settings, "# Two\n\nbody", ttl_seconds=None)
            self.assertRegex(first["id"], r"^[a-z0-9]{16}$")
            directory = settings.pages_dir / first["id"]
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for name in ("meta.json", "body.html", "source.md"):
                self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)
            self.assertEqual(store.read_text(settings, first["id"], "source.md"), "# One\n\nbody")
            self.assertIsNone(second["expires_at"])
            listed = [meta["id"] for meta in store.list_pages(settings)]
            self.assertEqual(set(listed), {first["id"], second["id"]})
            self.assertIsNone(store.read_text(settings, "../../etc", "source.md"))
            self.assertIsNone(store.file_path(settings, first["id"], "../meta.json"))

    def test_prune_drops_content_then_forgets_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = make_settings(Path(temporary))
            meta = store.publish(settings, "# Old\n\nbody", ttl_seconds=60)
            later = store.parse_iso(meta["created_at"]) + timedelta(seconds=61)
            self.assertTrue(store.is_expired(meta, later))
            self.assertEqual(store.prune(settings, later), (1, 0))
            kept = json.loads((settings.pages_dir / meta["id"] / "meta.json").read_text())
            self.assertTrue(kept["purged"])
            self.assertIsNone(store.read_text(settings, meta["id"], "body.html"))
            self.assertEqual(store.prune(settings, later + timedelta(days=31)), (0, 1))
            self.assertFalse((settings.pages_dir / meta["id"]).exists())

    def test_empty_and_oversized_sources_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = make_settings(Path(temporary))
            with self.assertRaises(ValueError):
                store.publish(settings, "  \n", ttl_seconds=60)
            with self.assertRaises(ValueError):
                store.publish(settings, "x" * (2 * 1024 * 1024 + 1), ttl_seconds=60)

    def test_ttl_parsing(self) -> None:
        self.assertEqual(parse_ttl("7d"), 7 * 86400)
        self.assertEqual(parse_ttl("12h"), 12 * 3600)
        self.assertIsNone(parse_ttl("forever"))
        with self.assertRaises(ValueError):
            parse_ttl("soon")


if __name__ == "__main__":
    unittest.main()
