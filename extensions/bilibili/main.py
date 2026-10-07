#!/usr/bin/env python3
"""Bilibili direct command extension.

Contract (see telegram-cli-gateway/extensions/README.md):
- Arguments come from argv; the gateway uses no shell.
- `@@PROGRESS <text>` lines on stdout are progress; the gateway updates the card in place.
- Absolute local paths on stdout inside the allowed directories get "send image/video/file" buttons.
- Exit code 0 means success, anything else failure; the reason goes to stderr.
- TG_LANGUAGE (en or zh) selects the message language.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bili
from bili import N_, tr

PROGRESS = "@@PROGRESS"
MANIFEST_DIR = Path(os.environ.get("TG_MANIFEST_DIR") or Path(__file__).resolve().parent)
WORKDIR = Path(os.environ.get("TG_WORKDIR") or os.getcwd())
COOKIE_FILE = bili.cookie_path(MANIFEST_DIR)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
# Retries on backup CDNs after one stream request fails.
RETRY_HOSTS = 3

# No line here may start with "@@PROGRESS": the gateway would consume it as a progress
# line and the first line of HELP would silently vanish.
HELP = N_("""Usage:
  /bili <URL|BV|av|ep|ss id> [quality] [options]
  /bili login cookie <full cookie string>   # sign in with SESSDATA and other cookies
  /bili login                               # sign in with a QR code
  /bili logout
  /bili whoami

Quality: 8k 4k 1080p60 1080p+ 1080p 720p 480p 360p (default: the best your account allows)
Options: -p <part number>  --audio-only  --full
Output directory: {workdir}

Note: signed out, Bilibili limits DASH streams to 480P; only the single-file durl stream reaches 720P.
      --full uses durl for that resolution at 3-4 times the size. Signed in, DASH reaches 1080P.
""")


@dataclass
class Args:
    ref: str = ""
    quality: int = 80
    quality_text: str = ""
    page: int = 1
    audio_only: bool = False
    # Signed out, DASH stops at 480P; single-file durl reaches 720P but is much larger.
    full: bool = False


SUBCOMMANDS = frozenset({"login", "logout", "whoami", "status"})

HELP_WORDS = frozenset({"-h", "--help", "help"})


def dispatch(argv: list[str]) -> tuple[str, list[str]]:
    """Split the raw input into (subcommand, argument lines).

    The gateway deliberately skips shell tokenizing (quotes in cookies break shlex), so it
    happens here:
      * input starting with login/logout/whoami selects that subcommand, which parses
        the raw text itself;
      * anything else is a download, and each line is split into tokens on spaces.
    """
    raw = os.environ.get("TG_RAW_ARGS", "")
    if not raw:
        raw = " ".join(argv) if argv else os.environ.get("TG_ARGV0", "")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return "", []
    first = lines[0].split()[0].lower()
    if first in HELP_WORDS:
        return "help", lines
    if first in SUBCOMMANDS:
        return first, lines
    return "download", lines


def flatten(lines: list[str]) -> list[str]:
    """Split argument lines into tokens on spaces (download arguments have no values with spaces)."""
    tokens: list[str] = []
    for line in lines:
        tokens.extend(line.split())
    return tokens


def parse_args(argv: list[str]) -> Args | None:
    args = Args()
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in {"-p", "--page"}:
            index += 1
            if index >= len(argv) or not argv[index].isdigit():
                raise bili.BiliError(tr("-p needs a part number, for example -p 2"))
            args.page = int(argv[index])
        elif item == "--audio-only":
            args.audio_only = True
        elif item in {"--full", "--durl"}:
            args.full = True
        elif item in {"-h", "--help"}:
            return None
        elif item.lower() in bili.QUALITY_ALIASES:
            args.quality = bili.QUALITY_ALIASES[item.lower()]
            args.quality_text = item
        elif item.startswith("-"):
            raise bili.BiliError(tr("Unknown option: {option}", option=item))
        else:
            rest.append(item)
        index += 1
    args.ref = rest[0] if rest else ""
    if not args.ref:
        raise bili.BiliError(tr("Missing a video URL or BV id"))
    return args


def progress(text: str) -> None:
    print(f"{PROGRESS} {text}", flush=True)


def human_size(size: int) -> str:
    if size <= 0:
        return tr("unknown")
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024.0
    return f"{size:.1f} GiB"


def sanitize(name: str, limit: int = 80) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:limit] or "bilibili").strip()


# --- Download ---


CHUNK = 1 << 20


def fetch_stream(stream: bili.Stream, target: Path, label: str) -> None:
    """Download one stream, retrying each CDN address in turn.

    The primary address (akamaized and others) has been seen to truncate silently: the
    connection closes normally but far fewer bytes than Content-Length arrive. So every
    pass checks the byte count and moves to the next address on a mismatch.
    """
    import urllib.request

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    urls = stream.all_urls()
    problems: list[str] = []

    for index, url in enumerate(urls):
        if index:
            progress(tr("{label}: retrying on backup address {current}/{total}", label=label, current=index + 1, total=len(urls)))
            part.unlink(missing_ok=True)
        done = 0
        total = stream.known_bytes
        started = time.monotonic()
        last_report = 0.0
        request = urllib.request.Request(
            url, headers={"User-Agent": bili.UA, "Referer": bili.REFERER, "Accept": "*/*"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                length = response.headers.get("Content-Length")
                if length and str(length).isdigit():
                    total = int(length)
                with part.open("wb") as handle:
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        now = time.monotonic()
                        if now - last_report >= 2.0:
                            last_report = now
                            speed = human_size(int(done / max(0.1, now - started)))
                            if total:
                                progress(
                                    f"{label} {done * 100 // total}% "
                                    f"({human_size(done)}/{human_size(total)}, {speed}/s)"
                                )
                            else:
                                progress(f"{label} {human_size(done)}")
        except Exception as exc:  # noqa: BLE001 - any network or disk error moves to the next address
            problems.append(tr("address {number}: {error}", number=index + 1, error=exc))
            continue

        if total and done != total:
            problems.append(tr("address {number}: received only {done}/{total}", number=index + 1, done=human_size(done), total=human_size(total)))
            continue
        part.replace(target)
        return

    part.unlink(missing_ok=True)
    detail = "; ".join(problems) or tr("unknown reason")
    raise bili.BiliError(tr("Downloading the {label} failed; every address was incomplete ({detail})", label=label, detail=detail))


def has_ffmpeg() -> bool:
    return bool(shutil.which(FFMPEG))


def merge(video: Path, audio: Path | None, target: Path) -> None:
    if audio is None:
        video.replace(target)
        return
    if not has_ffmpeg():
        raise bili.BiliError(
            tr("ffmpeg is needed to merge audio and video; install ffmpeg or use --audio-only")
        )
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target),
    ]
    progress(tr("Merging…"))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-500:]
        raise bili.BiliError(tr("ffmpeg merge failed: {detail}", detail=detail))
    video.unlink(missing_ok=True)
    audio.unlink(missing_ok=True)


# --- Subcommands ---


def cmd_login(lines: list[str]) -> int:
    """Sign in. Both forms work:

        /bili login cookie SESSDATA=...; bili_jct=...     (subcommand and cookie on one line)
        /bili login                                        (QR code)
        /bili login
        cookie SESSDATA=...                                (cookie on the next line)
    """
    text = " ".join(line.strip() for line in lines).strip()
    if text.lower().startswith("login"):
        text = text[len("login") :].strip()

    if text.lower().startswith("cookie"):
        raw = text[len("cookie") :].strip()
        if not raw:
            raise bili.BiliError(tr("Usage: /bili login cookie <full cookie string>"))
        cookies = bili.parse_cookie_header(raw)
        probe = bili.account_info(cookies.as_header())
        if not probe.logged_in:
            raise bili.BiliError(tr("The cookie is invalid or expired (the nav API reports no sign-in)"))
        bili.save_cookies(COOKIE_FILE, cookies)
        print(tr("Signed in with the cookie: {user}", user=probe.uname or tr("(unknown user)")))
        if probe.vip_label:
            print(tr("Membership: {label}", label=probe.vip_label))
        print(tr("Quality cap: {cap}", cap=bili.quality_cap(probe)))
        print(tr("Saved to the local cookie file (mode 0600, not in .env)."))
        return 0
    if text:
        raise bili.BiliError(tr(
            "Unrecognized sign-in method: {text}\n"
            "Usage: /bili login (QR code) or /bili login cookie <full cookie string>",
            text=text[:60],
        ))

def cmd_logout() -> int:
    if bili.clear_cookies(COOKIE_FILE):
        print(tr("Deleted the local cookie; later downloads run signed out (480P cap)."))
    else:
        print(tr("No cookie was saved on this machine."))
    return 0


def cmd_whoami() -> int:
    cookies = bili.load_cookies(COOKIE_FILE)
    if not cookies.logged_in:
        print(tr("Not signed in."))
        print(tr("Quality cap: {cap}", cap=bili.quality_cap(bili.Account(logged_in=False))))
        print(tr("Use /bili login for a QR code, or /bili login cookie <cookie string>."))
        return 0
    account = bili.account_info(cookies.as_header())
    if not account.logged_in:
        print(tr("The saved cookie expired; run /bili login again."))
        return 1
    print(tr("Signed in: {user} (mid {mid})", user=account.uname, mid=account.mid))
    if account.vip_label:
        print(tr("Membership: {label}", label=account.vip_label))
    print(tr("Quality cap: {cap}", cap=bili.quality_cap(account)))
    return 0


def cmd_download(lines: list[str]) -> int:
    parsed = parse_args(flatten(lines))
    if parsed is None:
        print(tr(HELP, workdir=WORKDIR))
        return 0
    cookies = bili.load_cookies(COOKIE_FILE)
    cookie_header = cookies.as_header()
    # Short links (b23.tv and others) must be followed to get the BV id and part.
    target = parsed.ref
    if bili.is_short_link(target):
        progress(tr("Resolving the short link…"))
        target = bili.resolve_short_link(target)
        if parsed.page == 1:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)
            if "p" in query and query["p"][0].isdigit():
                parsed.page = int(query["p"][0])
    ref = bili.parse_ref(target, page=parsed.page)

    if ref.kind == "video":
        info = bili.video_info(ref)
        if parsed.page < 1 or parsed.page > len(info.pages):
            raise bili.BiliError(tr(
                "Part {page} does not exist; this video has {count} parts",
                page=parsed.page, count=len(info.pages),
            ))
        page = info.pages[parsed.page - 1]
        title = info.title
        if len(info.pages) > 1:
            title = f"{title} - P{parsed.page} {page.title}".strip()
        duration = page.duration or info.duration
        bvid = info.bvid
        aid = info.aid
        cid = page.cid
        ep_id = 0
    else:
        episode = bili.bangumi_info(ref)
        title = f"{episode.title} - {episode.long_title}".strip(" -")
        duration = episode.duration
        bvid = episode.bvid
        aid = episode.aid
        cid = episode.cid
        ep_id = episode.ep_id

    progress(tr("Resolved: {title}", title=title))
    data, accepted = bili.playurl(
        cid=cid, bvid=bvid, aid=aid, ep_id=ep_id, qn=parsed.quality, cookie=cookie_header
    )
    videos, audios = bili.streams_from_dash(data, parsed.quality)
    single_file = False
    durl_data: dict[str, Any] = {}

    # Signed out, DASH only delivers 480P/360P (accept_quality just lists what the video has).
    # When the user wants more, or passes --full, switch to the single-file durl stream.
    dash_quality = data.get("quality") or 0
    wants_higher = (
        parsed.quality in bili.QUALITY_ORDER
        and dash_quality in bili.QUALITY_ORDER
        and bili.QUALITY_ORDER.index(parsed.quality) < bili.QUALITY_ORDER.index(dash_quality)
    )
    if parsed.full or (not videos and not dash_quality) or (wants_higher and not cookie_header):
        try:
            durl_data, _durl_accepted = bili.playurl_durl(
                cid=cid, bvid=bvid, aid=aid, ep_id=ep_id, qn=parsed.quality, cookie=cookie_header
            )
        except bili.BiliError:
            durl_data = {}
        only = bili.durl_stream(durl_data, parsed.quality)
        if only is not None:
            videos, audios, single_file = [only], [], True

    if not videos:
        raise bili.BiliError(tr("No usable video stream; you may need to sign in, or the video is region-locked"))

    chosen = videos[0]
    delivered = chosen.quality
    if bili.QUALITY_ORDER.index(delivered) < bili.QUALITY_ORDER.index(parsed.quality):
        if not cookie_header:
            progress(tr(
                "Signed out, only {quality} is available; signed in, DASH reaches 1080P. "
                "Sign in with /bili login",
                quality=bili.quality_name(delivered),
            ))
        elif accepted:
            cap = bili.quality_name(max(accepted, key=bili.QUALITY_ORDER.index))
            progress(tr("Your account allows up to {cap}; downloading {quality}", cap=cap, quality=bili.quality_name(delivered)))
    mode = tr("durl single file") if single_file else "DASH"
    progress(tr(
        "Quality {quality} · {mode} · codec {codec}",
        quality=bili.quality_name(delivered), mode=mode, codec=chosen.codec or tr("unknown"),
    ))

    folder = (WORKDIR / sanitize(title)).resolve()
    stem = sanitize(title)
    # Report the size up front so a file over Telegram's send limit is no surprise.
    if chosen.known_bytes:
        estimate = chosen.known_bytes
    else:
        estimate = int(chosen.bandwidth * duration / 8)
        if audios:
            estimate += int(audios[0].bandwidth * duration / 8)
    if estimate:
        limit = int(os.environ.get("TELEGRAM_MAX_FILE_BYTES") or 0)
        note = ""
        if limit and estimate > limit:
            note = tr(" · over the {limit} send limit; only the local path will be returned", limit=human_size(limit))
        progress(tr("Estimated size ≈{size}{note}", size=human_size(estimate), note=note))
    if parsed.audio_only:
        if not audios:
            raise bili.BiliError(tr("No audio stream"))
        target = folder / f"{stem}.m4a"
    else:
        target = folder / f"{stem}.mp4"

    video_temp = folder / f".{stem}.video.m4a.part"
    audio_temp = folder / f".{stem}.audio.m4a.part"
    if not parsed.audio_only:
        video_temp = folder / f".{stem}.video.tmp"
        fetch_stream(chosen, video_temp, tr("video stream"))
    audio_stream = audios[0] if audios else None
    if audio_stream is not None:
        fetch_stream(audio_stream, audio_temp, tr("audio stream"))

    if parsed.audio_only:
        audio_temp.replace(target)
    elif single_file:
        video_temp.replace(target)
    else:
        merge(video_temp, audio_temp if audio_stream else None, target)

    size = target.stat().st_size
    print(tr("Done: {quality} · {size}", quality=bili.quality_name(chosen.quality), size=human_size(size)))
    print(tr("Duration: {minutes} min {seconds} s", minutes=duration // 60, seconds=duration % 60))
    print(tr("Output: {path}", path=target))
    limit = int(os.environ.get("TELEGRAM_MAX_FILE_BYTES") or 0)
    if limit and size > limit:
        print(tr("Note: over Telegram's send limit ({limit}); only the local path will be returned.", limit=human_size(limit)))
    return 0


def main(argv: list[str]) -> int:
    command, lines = dispatch(argv)
    if command in {"", "help"}:
        print(tr(HELP, workdir=WORKDIR))
        return 0
    if command == "login":
        return cmd_login(lines)
    if command == "logout":
        return cmd_logout()
    if command in {"whoami", "status"}:
        return cmd_whoami()
    return cmd_download(lines)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except bili.BiliError as error:
        progress(tr("Failed"))
        print(tr("Error: {error}", error=error), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        progress(tr("Cancelled"))
        raise SystemExit(130)
