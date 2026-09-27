#!/usr/bin/env python3
"""哔哩哔哩直连命令扩展。

契约（见 telegram-cli-gateway/extensions/README.md）：
- 参数从 argv 拿；网关不经过 shell。
- stdout 里 `@@PROGRESS <文本>` 是进度行，网关原地刷新卡片。
- stdout 里的本机绝对路径会在允许目录内时挂上「发送图片/视频/文件」按钮。
- 退出码 0 成功，非 0 失败；失败原因写 stderr。
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

PROGRESS = "@@PROGRESS"
MANIFEST_DIR = Path(os.environ.get("TG_MANIFEST_DIR") or Path(__file__).resolve().parent)
WORKDIR = Path(os.environ.get("TG_WORKDIR") or os.getcwd())
COOKIE_FILE = bili.cookie_path(MANIFEST_DIR)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
# 单路请求失败后在备用 CDN 上的重试次数。
RETRY_HOSTS = 3

# 注意：这里不能出现以 "@@PROGRESS" 开头的行 —— 网关会把它当进度行消费掉，
# 导致 HELP 的第一行静默消失。
HELP = f"""用法：
  /bili <URL|BV号|av号|ep号|ss号> [画质] [选项]
  /bili login cookie <整条 Cookie 串>   # 用 SESSDATA 等 Cookie 登录
  /bili login                            # 扫码登录
  /bili logout
  /bili whoami

画质：8k 4k 1080p60 1080p+ 1080p 720p 480p 360p（默认按账号权限取最高）
选项：-p <分P序号>  --audio-only  --full
产物目录：{WORKDIR}

说明：未登录时 B站把 DASH 流限制到 480P，只有 durl 单文件能到 720P。
      --full 走 durl 换清晰度，代价是体积大 3~4 倍。登录后 DASH 就能给到 1080P。
"""


@dataclass
class Args:
    ref: str = ""
    quality: int = 80
    quality_text: str = ""
    page: int = 1
    audio_only: bool = False
    # 未登录时 DASH 被卡在 480P，durl 单文件能到 720P，但体积大得多。
    full: bool = False


SUBCOMMANDS = frozenset({"login", "logout", "whoami", "status"})

HELP_WORDS = frozenset({"-h", "--help", "help"})


def dispatch(argv: list[str]) -> tuple[str, list[str]]:
    """把原始输入拆成 (子命令, 参数行)。

    网关刻意不做 shell 分词（Cookie 里的引号会炸 shlex），所以拆词在这里做：
      * 以 login/logout/whoami 开头 → 对应子命令，整段原文交给它自己解析；
      * 其他情况 → 下载，每行按空格拆成 token。
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
    """把参数行按空格拆成 token（下载参数里没有带空格的值）。"""
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
                raise bili.BiliError("-p 需要一个分P序号，例如 -p 2")
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
            raise bili.BiliError(f"未知选项：{item}")
        else:
            rest.append(item)
        index += 1
    args.ref = rest[0] if rest else ""
    if not args.ref:
        raise bili.BiliError("缺少视频地址或 BV 号")
    return args


def progress(text: str) -> None:
    print(f"{PROGRESS} {text}", flush=True)


def human_size(size: int) -> str:
    if size <= 0:
        return "未知"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024.0
    return f"{size:.1f} GiB"


def sanitize(name: str, limit: int = 80) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:limit] or "bilibili").strip()


# --- 下载 ---


CHUNK = 1 << 20


def fetch_stream(stream: bili.Stream, target: Path, label: str) -> None:
    """下载单路流，逐个 CDN 地址重试。

    主地址（akamaized 等）实测会在中途静默截断：连接正常关闭，但收到的字节数
    远少于 Content-Length。所以每下载完一遍都要校验字节数，对不上就换下一个地址。
    """
    import urllib.request

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    urls = stream.all_urls()
    problems: list[str] = []

    for index, url in enumerate(urls):
        if index:
            progress(f"{label} 换备用地址重试（{index + 1}/{len(urls)}）")
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
        except Exception as exc:  # noqa: BLE001 - 任何网络/磁盘错误都换下一个地址
            problems.append(f"地址{index + 1}: {exc}")
            continue

        if total and done != total:
            problems.append(f"地址{index + 1}: 只收到 {human_size(done)}/{human_size(total)}")
            continue
        part.replace(target)
        return

    part.unlink(missing_ok=True)
    detail = "；".join(problems) or "未知原因"
    raise bili.BiliError(f"下载{label}失败，所有地址都不完整（{detail}）")


def has_ffmpeg() -> bool:
    return bool(shutil.which(FFMPEG))


def merge(video: Path, audio: Path | None, target: Path) -> None:
    if audio is None:
        video.replace(target)
        return
    if not has_ffmpeg():
        raise bili.BiliError(
            "需要 ffmpeg 才能把音视频合流；请安装 ffmpeg 或使用 --audio-only"
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
    progress("合流中…")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-500:]
        raise bili.BiliError(f"ffmpeg 合流失败：{detail}")
    video.unlink(missing_ok=True)
    audio.unlink(missing_ok=True)


# --- 子命令 ---


def cmd_login(lines: list[str]) -> int:
    """登录。两种写法都支持：

        /bili login cookie SESSDATA=...; bili_jct=...     （子命令与 Cookie 同行）
        /bili login                                        （扫码）
        /bili login
        cookie SESSDATA=...                                （Cookie 换行）
    """
    text = " ".join(line.strip() for line in lines).strip()
    if text.lower().startswith("login"):
        text = text[len("login") :].strip()

    if text.lower().startswith("cookie"):
        raw = text[len("cookie") :].strip()
        if not raw:
            raise bili.BiliError("用法：/bili login cookie <整条 Cookie 串>")
        cookies = bili.parse_cookie_header(raw)
        probe = bili.account_info(cookies.as_header())
        if not probe.logged_in:
            raise bili.BiliError("Cookie 无效或已过期（nav 接口报告未登录）")
        bili.save_cookies(COOKIE_FILE, cookies)
        print(f"Cookie 登录成功：{probe.uname or '(未知用户)'}")
        if probe.vip_label:
            print(f"会员：{probe.vip_label}")
        print(f"画质上限：{bili.quality_cap(probe)}")
        print("已保存到本机 cookie 文件（0600 权限，不在 .env 里）。")
        return 0
    if text:
        raise bili.BiliError(
            f"无法识别的登录方式：{text[:60]}\n"
            "用法：/bili login  （扫码）  或  /bili login cookie <整条 Cookie 串>"
        )

def cmd_logout() -> int:
    if bili.clear_cookies(COOKIE_FILE):
        print("已删除本机 cookie，后续下载按未登录处理（480P 上限）。")
    else:
        print("本机本来就没有保存 cookie。")
    return 0


def cmd_whoami() -> int:
    cookies = bili.load_cookies(COOKIE_FILE)
    if not cookies.logged_in:
        print("未登录。")
        print(f"画质上限：{bili.quality_cap(bili.Account(logged_in=False))}")
        print("用 /bili login 扫码，或 /bili login cookie <Cookie 串>。")
        return 0
    account = bili.account_info(cookies.as_header())
    if not account.logged_in:
        print("保存的 cookie 已失效，请重新 /bili login。")
        return 1
    print(f"已登录：{account.uname}（mid {account.mid}）")
    if account.vip_label:
        print(f"会员：{account.vip_label}")
    print(f"画质上限：{bili.quality_cap(account)}")
    return 0


def cmd_download(lines: list[str]) -> int:
    parsed = parse_args(flatten(lines))
    if parsed is None:
        print(HELP)
        return 0
    cookies = bili.load_cookies(COOKIE_FILE)
    cookie_header = cookies.as_header()
    # 短链（b23.tv 等）要跟随跳转才能拿到 BV 号 / 分P。
    target = parsed.ref
    if bili.is_short_link(target):
        progress("解析短链…")
        target = bili.resolve_short_link(target)
        if parsed.page == 1:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)
            if "p" in query and query["p"][0].isdigit():
                parsed.page = int(query["p"][0])
    ref = bili.parse_ref(target, page=parsed.page)

    if ref.kind == "video":
        info = bili.video_info(ref)
        if parsed.page < 1 or parsed.page > len(info.pages):
            raise bili.BiliError(
                f"分P {parsed.page} 不存在；这个视频有 {len(info.pages)} 个分P"
            )
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

    progress(f"已解析：{title}")
    data, accepted = bili.playurl(
        cid=cid, bvid=bvid, aid=aid, ep_id=ep_id, qn=parsed.quality, cookie=cookie_header
    )
    videos, audios = bili.streams_from_dash(data, parsed.quality)
    single_file = False
    durl_data: dict[str, Any] = {}

    # 未登录时 DASH 实际只下发 480P/360P（accept_quality 只是"这视频有哪些档"）。
    # 用户要更高画质，或显式 --full 时，改走 durl 单文件流。
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
        raise bili.BiliError("没有拿到可用的视频流；可能需要登录或该视频有地区限制")

    chosen = videos[0]
    delivered = chosen.quality
    if bili.QUALITY_ORDER.index(delivered) < bili.QUALITY_ORDER.index(parsed.quality):
        if not cookie_header:
            progress(
                f"未登录只有 {bili.quality_name(delivered)}；登录后 DASH 可到 1080P。"
                f"用 /bili login 登录"
            )
        elif accepted:
            cap = bili.quality_name(max(accepted, key=bili.QUALITY_ORDER.index))
            progress(f"账号最高可用 {cap}，已按 {bili.quality_name(delivered)} 下载")
    mode = "durl 单文件" if single_file else "DASH"
    progress(f"画质 {bili.quality_name(delivered)} · {mode} · 编码 {chosen.codec or '未知'}")

    folder = (WORKDIR / sanitize(title)).resolve()
    stem = sanitize(title)
    # 提前告诉用户体积，避免下完才发现超过 Telegram 发送上限。
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
            note = f" · 超过发送上限 {human_size(limit)}，只会回本机路径"
        progress(f"预计体积 ≈{human_size(estimate)}{note}")
    if parsed.audio_only:
        if not audios:
            raise bili.BiliError("没有拿到音频流")
        target = folder / f"{stem}.m4a"
    else:
        target = folder / f"{stem}.mp4"

    video_temp = folder / f".{stem}.video.m4a.part"
    audio_temp = folder / f".{stem}.audio.m4a.part"
    if not parsed.audio_only:
        video_temp = folder / f".{stem}.video.tmp"
        fetch_stream(chosen, video_temp, "视频流")
    audio_stream = audios[0] if audios else None
    if audio_stream is not None:
        fetch_stream(audio_stream, audio_temp, "音频流")

    if parsed.audio_only:
        audio_temp.replace(target)
    elif single_file:
        video_temp.replace(target)
    else:
        merge(video_temp, audio_temp if audio_stream else None, target)

    size = target.stat().st_size
    print(f"完成：{bili.quality_name(chosen.quality)} · {human_size(size)}")
    print(f"时长：{duration // 60} 分 {duration % 60} 秒")
    print(f"产物：{target}")
    limit = int(os.environ.get("TELEGRAM_MAX_FILE_BYTES") or 0)
    if limit and size > limit:
        print(f"注意：超过 Telegram 发送上限（{human_size(limit)}），只会回本机路径。")
    return 0


def main(argv: list[str]) -> int:
    command, lines = dispatch(argv)
    if command in {"", "help"}:
        print(HELP)
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
        progress("失败")
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        progress("已取消")
        raise SystemExit(130)
