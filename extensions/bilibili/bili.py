"""Bilibili API, WBI signing, cookies and QR sign-in. Standard library only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Messages follow the gateway language, passed as TG_LANGUAGE (en or zh).
LANGUAGE = "zh" if os.environ.get("TG_LANGUAGE") == "zh" else "en"


def tr(text: str, /, **values: Any) -> str:
    """Translate user-facing text with the ZH table below and fill its placeholders."""
    template = ZH.get(text, text) if LANGUAGE == "zh" else text
    return template.format(**values) if values else template


def N_(text: str) -> str:
    """Mark text for translation where it is defined; tr() translates it where shown."""
    return text

API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
# Observed: api.bilibili.com answers browser-like user agents with 412 (risk control)
# and lets others through, while the media CDNs reject generic clients such as curl/wget.
# "bilibili-cli-gateway/1.0" passes both, so it is used everywhere.
UA = "bilibili-cli-gateway/1.0"
REFERER = "https://www.bilibili.com"

# Reorder table for the WBI mixin key (matches bilibili-API-collect and upstream code).
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# code -> quality name. Bilibili returns a stream only when the account is entitled to it.
QUALITY_NAMES = {
    127: "8K",
    126: N_("Dolby Vision"),
    125: "HDR",
    120: "4K",
    116: "1080P60",
    112: "1080P+",
    100: N_("AI enhanced"),
    80: "1080P",
    74: "720P60",
    64: "720P",
    32: "480P",
    16: "360P",
    6: "240P",
}
QUALITY_ALIASES = {
    "8k": 127,
    "dolby": 126,
    "hdr": 125,
    "4k": 120,
    "1080p60": 116,
    "1080p+": 112,
    "1080p": 80,
    "720p60": 74,
    "720p": 64,
    "480p": 32,
    "360p": 16,
    "240p": 6,
}
QUALITY_ORDER = [127, 126, 125, 120, 116, 112, 100, 80, 74, 64, 32, 16, 6]
# Signed out or without entitlement, playurl returns no accept_quality; this is the fallback.
GUEST_QUALITIES = [32, 16, 6]

# fnval bit flags: DASH and the extra streams
FNVAL_DASH = 16
FNVAL_HDR = 64
FNVAL_4K = 128
FNVAL_DOLBY_AUDIO = 256
FNVAL_DOLBY_VISION = 512
FNVAL_8K = 1024
FNVAL_AV1 = 2048
# Bilibili wants every flag ORed together; a missing one can be refused with -400 or downgraded.
# 4048 = 16|64|128|256|512|1024|2048, a combination observed to be accepted.
FNVAL_DEFAULT = (
    FNVAL_DASH
    | FNVAL_HDR
    | FNVAL_4K
    | FNVAL_DOLBY_AUDIO
    | FNVAL_DOLBY_VISION
    | FNVAL_8K
    | FNVAL_AV1
)
assert FNVAL_DEFAULT == 4048, "Bilibili accepts only the complete fnval combination"

MAX_BVID_CANDIDATES = 4
QR_POLL_INTERVAL = 2.0
QR_TIMEOUT_SECONDS = 180


class BiliError(RuntimeError):
    """A Bilibili API or network failure; the message is meant for users."""


@dataclass
class Cookies:
    sessdata: str = ""
    bili_jct: str = ""
    dedeuserid: str = ""
    buvid3: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def as_header(self) -> str:
        pairs = {
            "SESSDATA": self.sessdata,
            "bili_jct": self.bili_jct,
            "DedeUserID": self.dedeuserid,
            "buvid3": self.buvid3,
            **{key: value for key, value in self.extra.items() if value},
        }
        return "; ".join(f"{key}={value}" for key, value in pairs.items() if value)

    @property
    def logged_in(self) -> bool:
        return bool(self.sessdata)

    def to_json(self) -> dict[str, Any]:
        return {
            "sessdata": self.sessdata,
            "bili_jct": self.bili_jct,
            "dedeuserid": self.dedeuserid,
            "buvid3": self.buvid3,
            "extra": self.extra,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Cookies:
        extra = payload.get("extra")
        return cls(
            sessdata=str(payload.get("sessdata") or ""),
            bili_jct=str(payload.get("bili_jct") or ""),
            dedeuserid=str(payload.get("dedeuserid") or ""),
            buvid3=str(payload.get("buvid3") or ""),
            extra=dict(extra) if isinstance(extra, dict) else {},
        )


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def http_get(url: str, *, cookie: str = "", timeout: float = 20.0) -> bytes:
    request = urllib.request.Request(url, headers=_headers(cookie))
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise BiliError(f"HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise BiliError(tr("Network error: {reason}", reason=exc.reason)) from exc


# b23.tv / bilibili.com short links carry no BV id in the URL; the redirect must be followed.
SHORT_LINK_HOSTS = ("b23.tv", "bili2233.cn", "bili22.cn", "bili23.cn")


def is_short_link(raw: str) -> bool:
    text = raw.strip()
    if not text.lower().startswith(("http://", "https://")):
        return False
    host = urllib.parse.urlparse(text).netloc.lower().split(":")[0]
    return any(host == item or host.endswith("." + item) for item in SHORT_LINK_HOSTS)


def resolve_short_link(raw: str) -> str:
    """Follow a short link and return the final URL.

    Bilibili puts the part in ?p=N, so take the first 302 Location instead of following to the end,
    and send Range: bytes=0-0 so the whole player page is not downloaded.
    """
    stripped = raw.strip()
    request = urllib.request.Request(
        stripped, headers={**_headers(), "Range": "bytes=0-0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            location = exc.headers.get("Location")
            if location:
                return urllib.parse.urljoin(stripped, location)
        raise BiliError(tr("Short link redirect failed: HTTP {code}", code=exc.code)) from exc
    except urllib.error.URLError as exc:
        raise BiliError(tr("Short link redirect failed: {reason}", reason=exc.reason)) from exc


def http_get_with_headers(
    url: str, *, cookie: str = "", timeout: float = 20.0
) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers=_headers(cookie))
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.read(), headers
    except urllib.error.HTTPError as exc:
        raise BiliError(f"HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise BiliError(tr("Network error: {reason}", reason=exc.reason)) from exc


def _headers(cookie: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": UA,
        "Referer": REFERER,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def api_json(url: str, *, cookie: str = "", timeout: float = 20.0) -> dict[str, Any]:
    raw = http_get(url, cookie=cookie, timeout=timeout)
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise BiliError(tr("The API did not return JSON: {raw}", raw=repr(raw[:200]))) from exc
    if not isinstance(payload, dict):
        raise BiliError(tr("The API returned an unexpected structure"))
    return payload


def require_ok(payload: dict[str, Any], what: str) -> dict[str, Any]:
    code = payload.get("code")
    if code != 0:
        message = payload.get("message") or payload.get("msg") or tr("unknown error")
        hint = {
            -404: N_(" (not found or deleted)"),
            -403: N_(" (no access; you may need to sign in or have premium)"),
            -400: N_(" (request parameters were rejected)"),
            -352: N_(" (risk control triggered; try again later)"),
            -509: N_(" (too many requests)"),
        }.get(code, "")
        raise BiliError(tr(
            "{what} failed: code={code} {message}{hint}",
            what=tr(what), code=code, message=message, hint=tr(hint) if hint else "",
        ))
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _wbi_encode(value: str) -> str:
    """Bilibili's WBI encoding: drop !'()* first, then percent-encode (the order matters)."""
    filtered = re.sub(r"[!'()*]", "", value)
    return urllib.parse.quote(filtered, safe="-_.~")


def wbi_query(params: dict[str, Any], img_key: str, sub_key: str, *, now: int | None = None) -> str:
    """Build a query string with wts/w_rid; keys are signed in sorted order."""
    mixin_key = "".join(
        (img_key + sub_key)[index] for index in MIXIN_KEY_ENC_TAB[:32]
    )
    merged = {key: str(value) for key, value in params.items()}
    merged["wts"] = str(now if now is not None else int(time.time()))
    query = "&".join(
        f"{_wbi_encode(key)}={_wbi_encode(merged[key])}" for key in sorted(merged)
    )
    sign = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return f"{query}&w_rid={sign}"


def wbi_keys(cookie: str = "") -> tuple[str, str]:
    """Fetch img_key / sub_key from the nav API.

    Signed out, nav returns code=-101 but data.wbi_img is still usable, so this must not
    use require_ok, or signed-out downloads would fail outright.
    """
    payload = api_json(f"{API}/x/web-interface/nav", cookie=cookie)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BiliError(tr("The nav API returned no data"))
    wbi_img = data.get("wbi_img") or {}
    img_url = str(wbi_img.get("img_url") or "")
    sub_url = str(wbi_img.get("sub_url") or "")
    img_key = img_url.rsplit("/", 1)[-1].rsplit(".", 1)[0] if img_url else ""
    sub_key = sub_url.rsplit("/", 1)[-1].rsplit(".", 1)[0] if sub_url else ""
    if not img_key or not sub_key:
        raise BiliError(tr("The nav API returned no wbi_img"))
    return img_key, sub_key


# --- URL and BV id parsing ---

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_EP_RE = re.compile(r"ep(\d+)", re.IGNORECASE)
_SS_RE = re.compile(r"ss(\d+)", re.IGNORECASE)


@dataclass
class VideoRef:
    kind: str  # "video" | "bangumi"
    bvid: str = ""
    aid: int = 0
    ep_id: int = 0
    season_id: int = 0
    page: int = 1


def normalize_ref(raw: str) -> str:
    """Turn bare digits into an av id and return anything else as is, for uniform matching."""
    if raw.isdigit():
        return f"av{raw}"
    return raw


def parse_ref(url: str, *, page: int = 1) -> VideoRef:
    text = normalize_ref(url.strip())
    if match := _BV_RE.search(text):
        return VideoRef(kind="video", bvid=match.group(1), page=page)
    if match := _AV_RE.search(text):
        return VideoRef(kind="video", aid=int(match.group(1)), page=page)
    if match := _EP_RE.search(text):
        return VideoRef(kind="bangumi", ep_id=int(match.group(1)))
    if match := _SS_RE.search(text):
        return VideoRef(kind="bangumi", season_id=int(match.group(1)))
    raise BiliError(tr("Could not find a BV/av/ep/ss id in “{url}”", url=url))


# --- Video info ---


@dataclass
class Page:
    index: int
    cid: int
    title: str
    duration: int


@dataclass
class VideoInfo:
    bvid: str
    aid: int
    title: str
    owner: str
    duration: int
    pages: list[Page]
    pic: str = ""


def video_info(ref: VideoRef) -> VideoInfo:
    params = {"bvid": ref.bvid} if ref.bvid else {"aid": str(ref.aid)}
    data = require_ok(
        api_json(f"{API}/x/web-interface/view?{urllib.parse.urlencode(params)}"),
        N_("Reading video info"),
    )
    pages_raw = data.get("pages")
    pages: list[Page] = []
    if isinstance(pages_raw, list):
        for item in pages_raw:
            if not isinstance(item, dict):
                continue
            try:
                pages.append(
                    Page(
                        index=int(item.get("page") or len(pages) + 1),
                        cid=int(item.get("cid") or 0),
                        title=str(item.get("part") or ""),
                        duration=int(item.get("duration") or 0),
                    )
                )
            except (TypeError, ValueError):
                continue
    if not pages:
        cid = data.get("cid")
        if not isinstance(cid, int):
            raise BiliError(tr("The video info has no usable cid"))
        pages = [Page(index=1, cid=cid, title=str(data.get("title") or ""), duration=0)]
    owner = data.get("owner") or {}
    return VideoInfo(
        bvid=str(data.get("bvid") or ref.bvid),
        aid=int(data.get("aid") or ref.aid or 0),
        title=str(data.get("title") or tr("Untitled")),
        owner=str(owner.get("name") or "") if isinstance(owner, dict) else "",
        duration=int(data.get("duration") or 0),
        pages=pages,
        pic=str(data.get("pic") or ""),
    )


@dataclass
class BangumiVideo:
    ep_id: int
    season_id: int
    title: str
    long_title: str
    cid: int
    aid: int
    bvid: str
    duration: int
    cover: str = ""


def bangumi_info(ref: VideoRef) -> BangumiVideo:
    if ref.ep_id:
        params = {"ep_id": str(ref.ep_id)}
    else:
        params = {"season_id": str(ref.season_id)}
    data = require_ok(
        api_json(f"{API}/pgc/view/web/season?{urllib.parse.urlencode(params)}"),
        N_("Reading series info"),
    )
    episodes = data.get("episodes")
    target: dict[str, Any] | None = None
    if ref.ep_id:
        if isinstance(episodes, list):
            for item in episodes:
                if isinstance(item, dict) and int(item.get("id") or 0) == ref.ep_id:
                    target = item
                    break
    elif isinstance(episodes, list) and episodes:
        first = episodes[0]
        target = first if isinstance(first, dict) else None
    if target is None:
        raise BiliError(tr("The series info has no matching episode"))
    try:
        cid = int(target.get("cid") or 0)
        aid = int(target.get("aid") or 0)
    except (TypeError, ValueError) as exc:
        raise BiliError(tr("The episode info is malformed")) from exc
    if not cid:
        raise BiliError(tr("The episode has no cid"))
    return BangumiVideo(
        ep_id=int(target.get("id") or ref.ep_id or 0),
        season_id=int(data.get("season_id") or ref.season_id or 0),
        title=str(data.get("title") or ""),
        long_title=str(target.get("long_title") or target.get("title") or ""),
        cid=cid,
        aid=aid,
        bvid=str(target.get("bvid") or ""),
        duration=int(target.get("duration") or 0),
        cover=str(data.get("cover") or ""),
    )


# --- Play URLs ---


@dataclass
class Stream:
    url: str
    quality: int
    codec: str
    bandwidth: int
    mime: str
    kind: str  # video | audio
    label: str = ""
    # durl gives exact byte counts; DASH only has a bitrate, so size is estimated from duration.
    known_bytes: int = 0
    # Backup CDN addresses. The primary may truncate silently, so the downloader tries each.
    alt_urls: list[str] = field(default_factory=list)

    def all_urls(self) -> list[str]:
        return [self.url, *self.alt_urls]

    @property
    def quality_name(self) -> str:
        return tr(QUALITY_NAMES.get(self.quality, str(self.quality)))

    @property
    def size_estimate(self) -> int:
        """Bytes estimated from the duration, used to warn about oversized files early."""
        return 0


def quality_name(code: int) -> str:
    return tr(QUALITY_NAMES.get(code, str(code)))


def pick_quality(accepted: list[int], requested: int) -> int:
    """Pick the highest quality the account allows that is not above requested."""
    allowed = sorted(
        (code for code in accepted if code in QUALITY_ORDER),
        key=QUALITY_ORDER.index,
    )
    if not allowed:
        return requested
    eligible = [code for code in allowed if QUALITY_ORDER.index(code) >= QUALITY_ORDER.index(requested)]
    if eligible:
        return min(eligible, key=QUALITY_ORDER.index)
    return allowed[0] if QUALITY_ORDER.index(requested) < QUALITY_ORDER.index(allowed[0]) else allowed[-1]


def playurl(
    *,
    cid: int,
    bvid: str = "",
    aid: int = 0,
    ep_id: int = 0,
    qn: int = 80,
    cookie: str = "",
) -> tuple[dict[str, Any], list[int]]:
    """Request playurl and return (data, accept_quality).

    `/x/player/playurl` without wbi is the most reliable in practice: code=0 signed in or out;
    the signed `/x/player/wbi/playurl` is refused with -400, so nothing is signed here.
    qn is omitted so Bilibili sends the best quality the account allows (with qn, signed-out drops to 360P).
    wbi_query stays for APIs that really require signing (search, user space and so on).
    """
    # cid is required; without it the request fails with -400.
    params: dict[str, Any] = {"cid": cid, "fnval": FNVAL_DEFAULT, "fourk": 1}
    if bvid:
        params["bvid"] = bvid
    elif aid:
        params["avid"] = aid
    if ep_id:
        params["ep_id"] = ep_id
    query = urllib.parse.urlencode(sorted((key, str(value)) for key, value in params.items()))
    data = require_ok(
        api_json(f"{API}/x/player/playurl?{query}", cookie=cookie), N_("Getting the play URL")
    )
    # Note: accept_quality lists the qualities the video has, not the ones you can get.
    # What is delivered is data.quality, always 32 (480P) or 16 (360P) when signed out.
    accept = data.get("accept_quality")
    accepted = [int(code) for code in accept if isinstance(code, (int, str)) and str(code).isdigit()] if isinstance(accept, list) else []
    return data, accepted


def playurl_durl(
    *, cid: int, bvid: str = "", aid: int = 0, ep_id: int = 0, qn: int = 80, cookie: str = ""
) -> tuple[dict[str, Any], list[int]]:
    """Request the single-file durl stream.

    Signed out, DASH is limited to 480P while durl reaches 720P (observed 720x1280).
    The cost: no DASH quality steps, audio and video in one MP4, and a much larger file
    (the same 4-minute portrait video: durl 46 MB vs DASH 480P 8.5 MB).
    """
    params: dict[str, Any] = {"cid": cid, "qn": qn, "fourk": 1, "platform": "html5"}
    if bvid:
        params["bvid"] = bvid
    elif aid:
        params["avid"] = aid
    if ep_id:
        params["ep_id"] = ep_id
    query = urllib.parse.urlencode(sorted((key, str(value)) for key, value in params.items()))
    data = require_ok(
        api_json(f"{API}/x/player/playurl?{query}", cookie=cookie), N_("Getting the play URL (durl)")
    )
    accept = data.get("accept_quality")
    accepted = (
        [int(code) for code in accept if isinstance(code, (int, str)) and str(code).isdigit()]
        if isinstance(accept, list)
        else []
    )
    return data, accepted


def streams_from_dash(data: dict[str, Any], want_quality: int) -> tuple[list[Stream], list[Stream]]:
    """Pick one video and one audio stream from DASH."""
    dash = data.get("dash")
    if not isinstance(dash, dict):
        return [], []
    videos: list[Stream] = []
    for item in dash.get("video") or []:
        if not isinstance(item, dict):
            continue
        urls = stream_urls(item)
        if not urls:
            continue
        url = urls[0]
        try:
            videos.append(
                Stream(
                    url=url,
                    quality=int(item.get("id") or 0),
                    codec=str(item.get("codecs") or ""),
                    bandwidth=int(item.get("bandwidth") or 0),
                    mime=str(item.get("mimeType") or "video/mp4"),
                    kind="video",
                    alt_urls=urls[1:],
                )
            )
        except (TypeError, ValueError):
            continue
    videos = [item for item in videos if item.quality in QUALITY_ORDER]
    if not videos:
        return [], []
    # Target quality: the highest available one not above the request
    allowed = sorted({item.quality for item in videos}, key=QUALITY_ORDER.index)
    chosen = pick_quality(allowed, want_quality)
    candidates = [item for item in videos if item.quality == chosen]
    # One quality can come in several codecs (avc/hevc/av1). Prefer AVC: it is the most compatible
    # and ffmpeg merges it without fuss; HEVC/AV1 show a black screen in some players and browsers.
    def codec_rank(stream: Stream) -> tuple[int, int]:
        codec = stream.codec.lower()
        if codec.startswith("avc"):
            tier = 0
        elif codec.startswith(("hev", "hvc")):
            tier = 1
        else:
            tier = 2
        return (tier, -stream.bandwidth)

    video = min(candidates, key=codec_rank)

    audios: list[Stream] = []
    for item in (dash.get("audio") or []):
        if not isinstance(item, dict):
            continue
        urls = stream_urls(item)
        if not urls:
            continue
        url = urls[0]
        try:
            audios.append(
                Stream(
                    url=url,
                    quality=int(item.get("id") or 0),
                    codec=str(item.get("codecs") or ""),
                    bandwidth=int(item.get("bandwidth") or 0),
                    mime=str(item.get("mimeType") or "audio/mp4"),
                    kind="audio",
                    alt_urls=urls[1:],
                )
            )
        except (TypeError, ValueError):
            continue
    if not audios:
        return [video], []
    # Audio ids: 30280=192K, 30232=132K, 30216=64K, 30250=Dolby Atmos, 30251=lossless
    def audio_rank(stream: Stream) -> tuple[int, int]:
        preferred = {30250: 0, 30251: 1, 30280: 2, 30232: 3, 30216: 4}
        return (preferred.get(stream.quality, 9), -stream.bandwidth)

    audio = min(audios, key=audio_rank)
    return [video], [audio]


def durl_stream(data: dict[str, Any], quality: int = 0) -> Stream | None:
    """durl mode: audio and video share one file, so no merge is needed.

    Older APIs and some series only offer durl. Without per-quality data, it is labeled with the requested quality.
    """
    durl = data.get("durl")
    if not isinstance(durl, list) or not durl:
        return None
    first = durl[0]
    if not isinstance(first, dict):
        return None
    urls = stream_urls(first)
    if not urls:
        return None
    url = urls[0]
    try:
        size = int(first.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return Stream(
        url=url,
        quality=quality or int(data.get("quality") or 0) or 32,
        codec="durl",
        # durl gives an exact byte count (not a bitrate), which serves as Content-Length while downloading.
        bandwidth=0,
        mime="video/mp4",
        kind="video",
        known_bytes=size,
        alt_urls=urls[1:],
    )


def stream_urls(item: dict[str, Any]) -> list[str]:
    """List every usable address for this stream, primary first.

    The primary address (akamaized and others) has been seen to truncate silently, so backupUrl
    must stay as a fallback rather than taking only the first.
    """
    urls: list[str] = []
    for key in ("baseUrl", "base_url", "url"):
        value = item.get(key)
        if isinstance(value, str) and value:
            urls.append(value)
            break
    backups = item.get("backupUrl") or item.get("backup_url")
    if isinstance(backups, list):
        urls.extend(
            candidate for candidate in backups if isinstance(candidate, str) and candidate
        )
    return urls


def _first_url(item: dict[str, Any]) -> str:
    """Return the first usable address: baseUrl for DASH, url for durl."""
    urls = stream_urls(item)
    return urls[0] if urls else ""


# --- Sign-in ---


@dataclass
class QrLogin:
    url: str
    qrcode_key: str

    def png(self, path: Path) -> Path:
        try:
            import segno
        except ImportError as exc:  # pragma: no cover - an actionable hint when the dependency is missing
            raise BiliError(
                tr("The QR code renderer segno is missing. Install it with:") + "\n"
                "  /home/openclaw/.local/bin/uv pip install --python "
                "/srv/projects/telegram-cli-gateway/.venv/bin/python segno"
            ) from exc
        segno.make(self.url, error="m").save(path, scale=9, border=2)
        return path


def qr_generate() -> QrLogin:
    data = require_ok(
        api_json(f"{PASSPORT}/x/passport-login/web/qrcode/generate"), N_("Creating the QR code")
    )
    url = str(data.get("url") or "")
    key = str(data.get("qrcode_key") or "")
    if not url or not key:
        raise BiliError(tr("The QR code API returned no url/qrcode_key"))
    return QrLogin(url=url, qrcode_key=key)


QR_STATUS = {
    0: N_("Success"),
    86038: N_("QR code expired"),
    86090: N_("Scanned; confirm on your phone"),
    86101: N_("Waiting for a scan"),
}


def qr_poll(qrcode_key: str) -> tuple[int, Cookies | None]:
    url = f"{PASSPORT}/x/passport-login/web/qrcode/poll?qrcode_key={urllib.parse.quote(qrcode_key)}"
    raw, headers = http_get_with_headers(url)
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise BiliError(tr("The QR code poll did not return JSON")) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    code = int((data or {}).get("code") or 0) if isinstance(data, dict) else -1
    if code != 0:
        return code, None
    cookies = parse_set_cookie(headers.get("set-cookie", ""))
    if not cookies.sessdata:
        raise BiliError(tr("Signed in, but no SESSDATA was returned"))
    return 0, cookies


def parse_set_cookie(header: str) -> Cookies:
    """Extract the needed fields from one Set-Cookie header (or several joined by commas)."""
    values: dict[str, str] = {}
    for chunk in re.split(r",(?=[^;=]+=)", header or ""):
        first = chunk.split(";", 1)[0].strip()
        if "=" not in first:
            continue
        key, value = first.split("=", 1)
        values[key.strip()] = value.strip()
    known = {"SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4", "DedeUserID__ckMd5"}
    extra = {
        key: value
        for key, value in values.items()
        if key not in known and key not in {"sid", "b_nut"} and value
    }
    return Cookies(
        sessdata=values.get("SESSDATA", ""),
        bili_jct=values.get("bili_jct", ""),
        dedeuserid=values.get("DedeUserID", ""),
        buvid3=values.get("buvid3", ""),
        extra=extra,
    )


def parse_cookie_header(raw: str) -> Cookies:
    """Parse the full cookie string a user pasted.

    Only split(";") and the first "=": cookie values contain = " ' | , and similar characters
    (SESSDATA even has %2A), and any shell, quote or JSON style parsing would break them.
    """
    values: dict[str, str] = {}
    # Telegram may wrap the text into several lines; join them back first.
    for line in raw.replace("\r", "").splitlines() or [raw]:
        for part in line.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            values[key.strip()] = value.strip()
    sessdata = values.get("SESSDATA", "")
    if not sessdata:
        raise BiliError(tr("The cookie has no SESSDATA, so sign-in is impossible"))
    return Cookies(
        sessdata=sessdata,
        bili_jct=values.get("bili_jct", ""),
        dedeuserid=values.get("DedeUserID", ""),
        buvid3=values.get("buvid3", ""),
        extra={
            key: value
            for key, value in values.items()
            if key
            not in {"SESSDATA", "bili_jct", "DedeUserID", "buvid3", "DedeUserID__ckMd5"}
        },
    )


@dataclass
class Account:
    logged_in: bool
    uname: str = ""
    mid: int = 0
    vip: bool = False
    vip_label: str = ""


def account_info(cookie: str = "") -> Account:
    """Read the account info. Signed out, nav returns -101, which means "signed out", not an error."""
    payload = api_json(f"{API}/x/web-interface/nav", cookie=cookie)
    if payload.get("code") not in (0, -101):
        require_ok(payload, N_("Reading account info"))
    data = payload.get("data")
    if not isinstance(data, dict):
        return Account(logged_in=False)
    vip = data.get("vip") or {}
    vip_status = int(vip.get("status") or 0) if isinstance(vip, dict) else 0
    vip_type = int(vip.get("type") or 0) if isinstance(vip, dict) else 0
    if vip_status and vip_type == 2:
        label = tr("Annual premium")
    elif vip_status:
        label = tr("Premium")
    else:
        label = ""
    return Account(
        logged_in=bool(data.get("isLogin")),
        uname=str(data.get("uname") or ""),
        mid=int(data.get("mid") or 0),
        vip=bool(vip_status),
        vip_label=label,
    )


def quality_cap(account: Account) -> str:
    if not account.logged_in:
        return tr("480P (signed-out cap)")
    if account.vip:
        return tr("4K / 1080P60 / HDR / Dolby (premium)")
    return tr("1080P (signed-in cap)")


def cookie_path(manifest_dir: Path) -> Path:
    return manifest_dir / "cookie.json"


def load_cookies(path: Path) -> Cookies:
    if not path.exists():
        return Cookies()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Cookies()
    if not isinstance(payload, dict):
        return Cookies()
    return Cookies.from_json(payload)


def save_cookies(path: Path, cookies: Cookies) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(cookies.to_json(), handle, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)


def clear_cookies(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# Chinese for every tr()/N_() string in this extension, used when TG_LANGUAGE=zh.
ZH: dict[str, str] = {
    """Usage:
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
""": """用法：
  /bili <URL|BV号|av号|ep号|ss号> [画质] [选项]
  /bili login cookie <整条 Cookie 串>   # 用 SESSDATA 等 Cookie 登录
  /bili login                            # 扫码登录
  /bili logout
  /bili whoami

画质：8k 4k 1080p60 1080p+ 1080p 720p 480p 360p（默认按账号权限取最高）
选项：-p <分P序号>  --audio-only  --full
产物目录：{workdir}

说明：未登录时 B站把 DASH 流限制到 480P，只有 durl 单文件能到 720P。
      --full 走 durl 换清晰度，代价是体积大 3~4 倍。登录后 DASH 就能给到 1080P。
""",
    "Dolby Vision": "杜比视界",
    "AI enhanced": "智能修复",
    "Short link redirect failed: HTTP {code}": "短链跳转失败：HTTP {code}",
    "Short link redirect failed: {reason}": "短链跳转失败：{reason}",
    "The API did not return JSON: {raw}": "接口返回不是 JSON：{raw}",
    "The API returned an unexpected structure": "接口返回结构异常",
    "unknown error": "未知错误",
    " (not found or deleted)": "（内容不存在或已删除）",
    " (no access; you may need to sign in or have premium)": "（无权限访问，可能需要登录或大会员）",
    " (request parameters were rejected)": "（请求参数被拒绝）",
    " (risk control triggered; try again later)": "（触发风控，请稍后再试）",
    " (too many requests)": "（请求过于频繁）",
    "{what} failed: code={code} {message}{hint}": "{what}失败：code={code} {message}{hint}",
    "The nav API returned no data": "nav 接口没有返回 data",
    "The nav API returned no wbi_img": "nav 接口没有返回 wbi_img",
    "Could not find a BV/av/ep/ss id in “{url}”": "无法从「{url}」识别出 BV/av/ep/ss 号",
    "Reading video info": "读取视频信息",
    "The video info has no usable cid": "视频信息里没有可用的 cid",
    "Untitled": "未命名",
    "Reading series info": "读取番剧信息",
    "The series info has no matching episode": "番剧信息里没有找到对应的剧集",
    "The episode info is malformed": "番剧剧集信息异常",
    "The episode has no cid": "番剧剧集缺少 cid",
    "Getting the play URL": "获取播放地址",
    "Getting the play URL (durl)": "获取播放地址(durl)",
    "The QR code renderer segno is missing. Install it with:": "缺少二维码渲染依赖 segno。安装：",
    "Creating the QR code": "生成二维码",
    "The QR code API returned no url/qrcode_key": "二维码接口没有返回 url/qrcode_key",
    "Success": "成功",
    "QR code expired": "二维码已过期",
    "Scanned; confirm on your phone": "已扫码，请在手机上确认",
    "Waiting for a scan": "等待扫码",
    "The QR code poll did not return JSON": "二维码轮询返回不是 JSON",
    "Signed in, but no SESSDATA was returned": "登录成功但没有拿到 SESSDATA",
    "The cookie has no SESSDATA, so sign-in is impossible": "Cookie 里没有 SESSDATA，无法登录",
    "Reading account info": "读取账号信息",
    "Annual premium": "年度大会员",
    "Premium": "大会员",
    "480P (signed-out cap)": "480P（未登录上限）",
    "4K / 1080P60 / HDR / Dolby (premium)": "4K / 1080P60 / HDR / 杜比（大会员）",
    "1080P (signed-in cap)": "1080P（登录上限）",
    "Network error: {reason}": "网络错误：{reason}",
    "-p needs a part number, for example -p 2": "-p 需要一个分P序号，例如 -p 2",
    "Unknown option: {option}": "未知选项：{option}",
    "Missing a video URL or BV id": "缺少视频地址或 BV 号",
    "unknown": "未知",
    "{label}: retrying on backup address {current}/{total}": "{label} 换备用地址重试（{current}/{total}）",
    "address {number}: {error}": "地址{number}: {error}",
    "address {number}: received only {done}/{total}": "地址{number}: 只收到 {done}/{total}",
    "unknown reason": "未知原因",
    "Downloading the {label} failed; every address was incomplete ({detail})": "下载{label}失败，所有地址都不完整（{detail}）",
    "ffmpeg is needed to merge audio and video; install ffmpeg or use --audio-only": "需要 ffmpeg 才能把音视频合流；请安装 ffmpeg 或使用 --audio-only",
    "Merging…": "合流中…",
    "ffmpeg merge failed: {detail}": "ffmpeg 合流失败：{detail}",
    "Usage: /bili login cookie <full cookie string>": "用法：/bili login cookie <整条 Cookie 串>",
    "The cookie is invalid or expired (the nav API reports no sign-in)": "Cookie 无效或已过期（nav 接口报告未登录）",
    "Signed in with the cookie: {user}": "Cookie 登录成功：{user}",
    "(unknown user)": "(未知用户)",
    "Membership: {label}": "会员：{label}",
    "Quality cap: {cap}": "画质上限：{cap}",
    "Saved to the local cookie file (mode 0600, not in .env).": "已保存到本机 cookie 文件（0600 权限，不在 .env 里）。",
    "Unrecognized sign-in method: {text}\nUsage: /bili login (QR code) or /bili login cookie <full cookie string>": "无法识别的登录方式：{text}\n用法：/bili login  （扫码）  或  /bili login cookie <整条 Cookie 串>",
    "Deleted the local cookie; later downloads run signed out (480P cap).": "已删除本机 cookie，后续下载按未登录处理（480P 上限）。",
    "No cookie was saved on this machine.": "本机本来就没有保存 cookie。",
    "Not signed in.": "未登录。",
    "Use /bili login for a QR code, or /bili login cookie <cookie string>.": "用 /bili login 扫码，或 /bili login cookie <Cookie 串>。",
    "The saved cookie expired; run /bili login again.": "保存的 cookie 已失效，请重新 /bili login。",
    "Signed in: {user} (mid {mid})": "已登录：{user}（mid {mid}）",
    "Resolving the short link…": "解析短链…",
    "Part {page} does not exist; this video has {count} parts": "分P {page} 不存在；这个视频有 {count} 个分P",
    "Resolved: {title}": "已解析：{title}",
    "No usable video stream; you may need to sign in, or the video is region-locked": "没有拿到可用的视频流；可能需要登录或该视频有地区限制",
    "Signed out, only {quality} is available; signed in, DASH reaches 1080P. Sign in with /bili login": "未登录只有 {quality}；登录后 DASH 可到 1080P。用 /bili login 登录",
    "Your account allows up to {cap}; downloading {quality}": "账号最高可用 {cap}，已按 {quality} 下载",
    "durl single file": "durl 单文件",
    "Quality {quality} · {mode} · codec {codec}": "画质 {quality} · {mode} · 编码 {codec}",
    " · over the {limit} send limit; only the local path will be returned": " · 超过发送上限 {limit}，只会回本机路径",
    "Estimated size ≈{size}{note}": "预计体积 ≈{size}{note}",
    "No audio stream": "没有拿到音频流",
    "video stream": "视频流",
    "audio stream": "音频流",
    "Done: {quality} · {size}": "完成：{quality} · {size}",
    "Duration: {minutes} min {seconds} s": "时长：{minutes} 分 {seconds} 秒",
    "Output: {path}": "产物：{path}",
    "Note: over Telegram's send limit ({limit}); only the local path will be returned.": "注意：超过 Telegram 发送上限（{limit}），只会回本机路径。",
    "Failed": "失败",
    "Error: {error}": "错误：{error}",
    "Cancelled": "已取消",
}
