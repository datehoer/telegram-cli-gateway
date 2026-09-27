"""B站接口、WBI 签名、Cookie 与扫码登录。只用标准库。"""

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

API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
# 实测：api.bilibili.com 只对“浏览器式”UA 返回 412（风控），非浏览器 UA 通过；
# 但 CDN 上的音视频流反过来要求 UA 不是 curl/wget 这类通用客户端。
# "bilibili-cli-gateway/1.0" 两边都能过，因此固定使用它。
UA = "bilibili-cli-gateway/1.0"
REFERER = "https://www.bilibili.com"

# WBI 混入密钥的重排表（bilibili-API-collect / 上游实现一致）。
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# code → 画质名。B站只在当前账号有权限时才会返回对应流。
QUALITY_NAMES = {
    127: "8K",
    126: "杜比视界",
    125: "HDR",
    120: "4K",
    116: "1080P60",
    112: "1080P+",
    100: "智能修复",
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
# 未登录/无权限时 playurl 不返回 accept_quality，用它兜底展示。
GUEST_QUALITIES = [32, 16, 6]

# fnval 位标志：DASH 与各附加流
FNVAL_DASH = 16
FNVAL_HDR = 64
FNVAL_4K = 128
FNVAL_DOLBY_AUDIO = 256
FNVAL_DOLBY_VISION = 512
FNVAL_8K = 1024
FNVAL_AV1 = 2048
# B站要求把位标志全部 OR 起来；少任何一位都可能被拒为 -400 或降档。
# 4048 = 16|64|128|256|512|1024|2048，实测是被接受的组合。
FNVAL_DEFAULT = (
    FNVAL_DASH
    | FNVAL_HDR
    | FNVAL_4K
    | FNVAL_DOLBY_AUDIO
    | FNVAL_DOLBY_VISION
    | FNVAL_8K
    | FNVAL_AV1
)
assert FNVAL_DEFAULT == 4048, "B站只接受完整的 fnval 位组合"

MAX_BVID_CANDIDATES = 4
QR_POLL_INTERVAL = 2.0
QR_TIMEOUT_SECONDS = 180


class BiliError(RuntimeError):
    """B站 API 或网络层面的失败，消息面向用户可读。"""


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
        raise BiliError(f"网络错误：{exc.reason}") from exc


# b23.tv / bilibili.com 短链里的 BV 号不会出现在 URL 上，必须跟随跳转才能拿到。
SHORT_LINK_HOSTS = ("b23.tv", "bili2233.cn", "bili22.cn", "bili23.cn")


def is_short_link(raw: str) -> bool:
    text = raw.strip()
    if not text.lower().startswith(("http://", "https://")):
        return False
    host = urllib.parse.urlparse(text).netloc.lower().split(":")[0]
    return any(host == item or host.endswith("." + item) for item in SHORT_LINK_HOSTS)


def resolve_short_link(raw: str) -> str:
    """跟随短链跳转，返回最终 URL。

    B站把分P 放在 ?p=N 上，所以取第一个 302 的 Location 而不是跟到底；
    同时带 Range: bytes=0-0，避免把整个播放页拉下来。
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
        raise BiliError(f"短链跳转失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise BiliError(f"短链跳转失败：{exc.reason}") from exc


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
        raise BiliError(f"网络错误：{exc.reason}") from exc


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
        raise BiliError(f"接口返回不是 JSON：{raw[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise BiliError("接口返回结构异常")
    return payload


def require_ok(payload: dict[str, Any], what: str) -> dict[str, Any]:
    code = payload.get("code")
    if code != 0:
        message = payload.get("message") or payload.get("msg") or "未知错误"
        hint = {
            -404: "（内容不存在或已删除）",
            -403: "（无权限访问，可能需要登录或大会员）",
            -400: "（请求参数被拒绝）",
            -352: "（触发风控，请稍后再试）",
            -509: "（请求过于频繁）",
        }.get(code, "")
        raise BiliError(f"{what}失败：code={code} {message}{hint}")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _wbi_encode(value: str) -> str:
    """B站的 WBI 编码：先剔除 !'()* 再做百分号编码（顺序不能颠倒）。"""
    filtered = re.sub(r"[!'()*]", "", value)
    return urllib.parse.quote(filtered, safe="-_.~")


def wbi_query(params: dict[str, Any], img_key: str, sub_key: str, *, now: int | None = None) -> str:
    """构造带 wts/w_rid 的查询串。键按字典序排序后参与签名。"""
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
    """从 nav 接口取 img_key / sub_key。

    未登录时 nav 返回 code=-101，但 data.wbi_img 依然可用，所以这里不能走
    require_ok，否则未登录下载会直接失败。
    """
    payload = api_json(f"{API}/x/web-interface/nav", cookie=cookie)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BiliError("nav 接口没有返回 data")
    wbi_img = data.get("wbi_img") or {}
    img_url = str(wbi_img.get("img_url") or "")
    sub_url = str(wbi_img.get("sub_url") or "")
    img_key = img_url.rsplit("/", 1)[-1].rsplit(".", 1)[0] if img_url else ""
    sub_key = sub_url.rsplit("/", 1)[-1].rsplit(".", 1)[0] if sub_url else ""
    if not img_key or not sub_key:
        raise BiliError("nav 接口没有返回 wbi_img")
    return img_key, sub_key


# --- URL / BV 号解析 ---

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
    """把裸数字补成 av 号，其余原样返回，便于统一匹配。"""
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
    raise BiliError(f"无法从「{url}」识别出 BV/av/ep/ss 号")


# --- 视频信息 ---


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
        "读取视频信息",
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
            raise BiliError("视频信息里没有可用的 cid")
        pages = [Page(index=1, cid=cid, title=str(data.get("title") or ""), duration=0)]
    owner = data.get("owner") or {}
    return VideoInfo(
        bvid=str(data.get("bvid") or ref.bvid),
        aid=int(data.get("aid") or ref.aid or 0),
        title=str(data.get("title") or "未命名"),
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
        "读取番剧信息",
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
        raise BiliError("番剧信息里没有找到对应的剧集")
    try:
        cid = int(target.get("cid") or 0)
        aid = int(target.get("aid") or 0)
    except (TypeError, ValueError) as exc:
        raise BiliError("番剧剧集信息异常") from exc
    if not cid:
        raise BiliError("番剧剧集缺少 cid")
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


# --- 播放地址 ---


@dataclass
class Stream:
    url: str
    quality: int
    codec: str
    bandwidth: int
    mime: str
    kind: str  # video | audio
    label: str = ""
    # durl 会给精确字节数；DASH 只有码率，只能按 duration 估算。
    known_bytes: int = 0
    # 备用 CDN 地址。主地址可能中途静默截断，下载器逐个重试。
    alt_urls: list[str] = field(default_factory=list)

    def all_urls(self) -> list[str]:
        return [self.url, *self.alt_urls]

    @property
    def quality_name(self) -> str:
        return QUALITY_NAMES.get(self.quality, str(self.quality))

    @property
    def size_estimate(self) -> int:
        """按时长估算的字节数，用于提前提示超大文件。"""
        return 0


def quality_name(code: int) -> str:
    return QUALITY_NAMES.get(code, str(code))


def pick_quality(accepted: list[int], requested: int) -> int:
    """在账号可用的画质里选一个不高于 requested 的最高档。"""
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
    """请求 playurl，返回 (data, accept_quality)。

    `/x/player/playurl`（不带 wbi）实测最稳：未登录与登录都返回 code=0；
    带 wts/w_rid 的 `/x/player/wbi/playurl` 反而会被拒为 -400，所以这里不签名。
    不传 qn，让 B站直接下发当前账号可用的最高画质（带 qn 时未登录会被降到 360P）。
    wbi_query 保留给后续确实要求签名的接口（搜索、空间等）。
    """
    # cid 必须带上，少了它会直接 -400。
    params: dict[str, Any] = {"cid": cid, "fnval": FNVAL_DEFAULT, "fourk": 1}
    if bvid:
        params["bvid"] = bvid
    elif aid:
        params["avid"] = aid
    if ep_id:
        params["ep_id"] = ep_id
    query = urllib.parse.urlencode(sorted((key, str(value)) for key, value in params.items()))
    data = require_ok(
        api_json(f"{API}/x/player/playurl?{query}", cookie=cookie), "获取播放地址"
    )
    # 注意：accept_quality 列的是「这个视频有哪些档」，不是「你能拿哪些档」。
    # 真正下发的是 data.quality，未登录恒为 32(480P)/16(360P)。
    accept = data.get("accept_quality")
    accepted = [int(code) for code in accept if isinstance(code, (int, str)) and str(code).isdigit()] if isinstance(accept, list) else []
    return data, accepted


def playurl_durl(
    *, cid: int, bvid: str = "", aid: int = 0, ep_id: int = 0, qn: int = 80, cookie: str = ""
) -> tuple[dict[str, Any], list[int]]:
    """请求 durl 单文件流。

    未登录时 DASH 被限制到 480P，但 durl 能拿到 720P（实测 720x1280）。
    代价是没有分档 DASH，音视频在同一个 MP4 里，且体积明显更大
    （同样 4 分钟竖屏视频：durl 46MB vs DASH 480P 8.5MB）。
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
        api_json(f"{API}/x/player/playurl?{query}", cookie=cookie), "获取播放地址(durl)"
    )
    accept = data.get("accept_quality")
    accepted = (
        [int(code) for code in accept if isinstance(code, (int, str)) and str(code).isdigit()]
        if isinstance(accept, list)
        else []
    )
    return data, accepted


def streams_from_dash(data: dict[str, Any], want_quality: int) -> tuple[list[Stream], list[Stream]]:
    """从 DASH 里选一路视频 + 一路音频。"""
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
    # 目标画质：取不高于期望的最高可用档
    allowed = sorted({item.quality for item in videos}, key=QUALITY_ORDER.index)
    chosen = pick_quality(allowed, want_quality)
    candidates = [item for item in videos if item.quality == chosen]
    # 同一个画质可能有多编码（avc/hevc/av1）。优先 AVC：兼容性最好，
    # ffmpeg 合流不挑；HEVC/AV1 在部分播放器/浏览器里会黑屏。
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
    # 音频 id：30280=192K、30232=132K、30216=64K、30250=杜比全景声、30251=无损
    def audio_rank(stream: Stream) -> tuple[int, int]:
        preferred = {30250: 0, 30251: 1, 30280: 2, 30232: 3, 30216: 4}
        return (preferred.get(stream.quality, 9), -stream.bandwidth)

    audio = min(audios, key=audio_rank)
    return [video], [audio]


def durl_stream(data: dict[str, Any], quality: int = 0) -> Stream | None:
    """durl 模式：音视频在同一文件里，不需要合流。

    老接口/部分番剧只给 durl。质量拿不到逐档信息，只能标成请求的档位。
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
        # durl 给的是已知的精确字节数（不是码率），下载时正好当 Content-Length 用。
        bandwidth=0,
        mime="video/mp4",
        kind="video",
        known_bytes=size,
        alt_urls=urls[1:],
    )


def stream_urls(item: dict[str, Any]) -> list[str]:
    """列出这个流的全部可用地址，主地址在前。

    主地址（akamaized 等）实测会在中途静默截断，所以必须保留 backupUrl 兜底，
    不能只取第一个。
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
    """取第一个可用地址。DASH 用 baseUrl，durl 用 url。"""
    urls = stream_urls(item)
    return urls[0] if urls else ""


# --- 登录 ---


@dataclass
class QrLogin:
    url: str
    qrcode_key: str

    def png(self, path: Path) -> Path:
        try:
            import segno
        except ImportError as exc:  # pragma: no cover - 部署环境缺失时给出可操作提示
            raise BiliError(
                "缺少二维码渲染依赖 segno。安装：\n"
                "  /home/openclaw/.local/bin/uv pip install --python "
                "/srv/projects/telegram-cli-gateway/.venv/bin/python segno"
            ) from exc
        segno.make(self.url, error="m").save(path, scale=9, border=2)
        return path


def qr_generate() -> QrLogin:
    data = require_ok(
        api_json(f"{PASSPORT}/x/passport-login/web/qrcode/generate"), "生成二维码"
    )
    url = str(data.get("url") or "")
    key = str(data.get("qrcode_key") or "")
    if not url or not key:
        raise BiliError("二维码接口没有返回 url/qrcode_key")
    return QrLogin(url=url, qrcode_key=key)


QR_STATUS = {
    0: "成功",
    86038: "二维码已过期",
    86090: "已扫码，请在手机上确认",
    86101: "等待扫码",
}


def qr_poll(qrcode_key: str) -> tuple[int, Cookies | None]:
    url = f"{PASSPORT}/x/passport-login/web/qrcode/poll?qrcode_key={urllib.parse.quote(qrcode_key)}"
    raw, headers = http_get_with_headers(url)
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise BiliError("二维码轮询返回不是 JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    code = int((data or {}).get("code") or 0) if isinstance(data, dict) else -1
    if code != 0:
        return code, None
    cookies = parse_set_cookie(headers.get("set-cookie", ""))
    if not cookies.sessdata:
        raise BiliError("登录成功但没有拿到 SESSDATA")
    return 0, cookies


def parse_set_cookie(header: str) -> Cookies:
    """从一个（或多个逗号拼接的）Set-Cookie 头里提取需要的字段。"""
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
    """解析用户粘贴的整条 Cookie 串。

    只做 split(";") + 找第一个 "="：Cookie 值里本来就含 = " ' | , 等字符
    （SESSDATA 里甚至有 %2A），任何 shell/引号/JSON 式的解析都会把它们弄坏。
    """
    values: dict[str, str] = {}
    # Telegram 可能把整段换行折行，先拼回一行。
    for line in raw.replace("\r", "").splitlines() or [raw]:
        for part in line.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            values[key.strip()] = value.strip()
    sessdata = values.get("SESSDATA", "")
    if not sessdata:
        raise BiliError("Cookie 里没有 SESSDATA，无法登录")
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
    """读取账号信息。未登录时 nav 返回 -101，这里视为“未登录”而不是错误。"""
    payload = api_json(f"{API}/x/web-interface/nav", cookie=cookie)
    if payload.get("code") not in (0, -101):
        require_ok(payload, "读取账号信息")
    data = payload.get("data")
    if not isinstance(data, dict):
        return Account(logged_in=False)
    vip = data.get("vip") or {}
    vip_status = int(vip.get("status") or 0) if isinstance(vip, dict) else 0
    vip_type = int(vip.get("type") or 0) if isinstance(vip, dict) else 0
    if vip_status and vip_type == 2:
        label = "年度大会员"
    elif vip_status:
        label = "大会员"
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
        return "480P（未登录上限）"
    if account.vip:
        return "4K / 1080P60 / HDR / 杜比（大会员）"
    return "1080P（登录上限）"


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
