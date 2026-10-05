"""relay-page: publish Markdown as a private reading page and print its URL."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import origin, store, telegram
from .auth import Users, generate_password
from .config import load_settings, parse_ttl
from .render import RenderError


def _bot_info(settings, requested: str | None, detected: str | None) -> dict | None:
    identities = telegram.bot_identities(settings)
    key = None
    if requested:
        wanted = requested.lstrip("@").lower()
        key = next(
            (k for k, v in identities.items() if k == wanted or v.get("username", "").lower() == wanted),
            None,
        )
        if key is None:
            raise SystemExit(f"relay-page: unknown bot {requested!r}; known: {', '.join(identities) or 'none'}")
    key = key or (detected if detected in identities else None) or next(iter(identities), None)
    if key is None:
        return None
    return {"key": key, **identities[key]}


def cmd_publish(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.file in (None, "-"):
        source, base_dir = sys.stdin.read(), None
    else:
        path = Path(args.file).expanduser()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"relay-page: cannot read {path}: {exc}", file=sys.stderr)
            return 2
        base_dir = path.resolve().parent
    try:
        ttl = parse_ttl(args.ttl) if args.ttl else settings.default_ttl
    except ValueError as exc:
        print(f"relay-page: {exc}", file=sys.stderr)
        return 2
    detected_bot, detected_cli = origin.detect(settings.gateway_dir)
    try:
        meta = store.publish(
            settings,
            source,
            title=args.title,
            ttl_seconds=ttl,
            bot=_bot_info(settings, args.bot, detected_bot),
            author=args.author or detected_cli,
            base_dir=base_dir,
        )
    except RenderError as exc:
        print(f"relay-page: {exc}", file=sys.stderr)
        return 2
    url = settings.page_url(meta["id"])
    if args.json:
        print(json.dumps({"url": url, **{k: meta[k] for k in ("id", "title", "expires_at", "reading_minutes")}}, ensure_ascii=False))
    else:
        print(url)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    settings = load_settings()
    for meta in store.list_pages(settings):
        state = "expired" if store.is_expired(meta) else (meta.get("expires_at") or "forever")
        print(f"{meta['id']}  {meta.get('created_at')}  {state:20}  {meta.get('title')}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    settings = load_settings()
    ok = all(store.delete_page(settings, page_id) for page_id in args.ids)
    return 0 if ok else 1


def cmd_prune(args: argparse.Namespace) -> int:
    purged, removed = store.prune(load_settings())
    print(f"expired content dropped: {purged}; forgotten: {removed}")
    return 0


def _users(settings) -> Users:
    return Users(settings.users_path)


def cmd_user_set(args: argparse.Namespace) -> int:
    """Create an account or replace its password (which logs it out everywhere)."""
    settings = load_settings()
    if args.generate:
        password = generate_password()
    elif args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Again: ") != password:
            print("relay-page: passwords differ", file=sys.stderr)
            return 2
    try:
        _users(settings).set_password(args.username, password)
    except ValueError as exc:
        print(f"relay-page: {exc}", file=sys.stderr)
        return 2
    if args.generate:
        print(password)
    return 0


def cmd_user_delete(args: argparse.Namespace) -> int:
    return 0 if _users(load_settings()).delete(args.username) else 1


def cmd_user_list(args: argparse.Namespace) -> int:
    for name in _users(load_settings()).names():
        print(name)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(load_settings())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay-page", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    publish = commands.add_parser("publish", help="publish Markdown (file or stdin) and print the page URL")
    publish.add_argument("file", nargs="?", help="Markdown file; '-' or omitted reads stdin")
    publish.add_argument("--title", help="page title (default: the first '# ' heading)")
    publish.add_argument("--ttl", help="lifetime: 7d, 12h, 30m or forever (default 7d)")
    publish.add_argument("--bot", help="bot key or @username for the header (default: detected)")
    publish.add_argument("--author", help="name shown in the header (default: the CLI, e.g. Claude)")
    publish.add_argument("--json", action="store_true", help="print JSON instead of the bare URL")
    publish.set_defaults(func=cmd_publish)

    commands.add_parser("list", help="list pages").set_defaults(func=cmd_list)
    delete = commands.add_parser("delete", help="delete pages by id")
    delete.add_argument("ids", nargs="+")
    delete.set_defaults(func=cmd_delete)
    commands.add_parser("prune", help="drop expired content now").set_defaults(func=cmd_prune)
    user = commands.add_parser("user", help="manage login accounts").add_subparsers(dest="action", required=True)
    user_set = user.add_parser("set", help="create an account or set its password")
    user_set.add_argument("username")
    source = user_set.add_mutually_exclusive_group()
    source.add_argument("--generate", action="store_true", help="generate a strong password and print it")
    source.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    user_set.set_defaults(func=cmd_user_set)
    user_delete = user.add_parser("delete", help="delete an account")
    user_delete.add_argument("username")
    user_delete.set_defaults(func=cmd_user_delete)
    user.add_parser("list", help="list accounts").set_defaults(func=cmd_user_list)
    commands.add_parser("serve", help="run the HTTP server").set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
