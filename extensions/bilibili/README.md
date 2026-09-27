# /bili — Bilibili Download Extension

It works without an account, but **DASH streams are capped at 480p**; signing in raises that to 1080p, and a Premium membership unlocks 4K/HDR/Dolby. Without signing in the only way to get a higher resolution is `--full`, which uses the single-file durl route (up to 720p at the cost of a 3–4× larger file).

```
/bili https://b23.tv/dfBSIGr        # b23.tv short links are followed through the redirect before parsing
/bili BV1xx411c7mD                  # defaults to the highest quality the account can use
/bili BV1xx411c7mD 1080p            # pick a quality
/bili BV1xx411c7mD -p 3 720p        # part 3 of a multi-part video
/bili BV1xx411c7mD --audio-only     # audio only (m4a)
/bili BV1xx411c7mD 720p --full      # unsigned-in: trade file size for resolution via durl (3–4× larger)
/bili ep123                         # a single bangumi episode
/bili https://www.bilibili.com/bangumi/play/ss456
```

## Login

Two ways, both writing to `cookie.json` (permissions 0600, **never into `.env`**).

```
/bili login cookie SESSDATA=xxx; bili_jct=yyy; DedeUserID=123
/bili login                          # QR code: sends the image automatically and saves on scan
/bili whoami                         # show the current account and its quality ceiling
/bili logout                         # clear the local cookie
```

The cookie route is the least hassle: sign in to Bilibili in a browser → F12 → Network → any request → copy the whole `Cookie` header. `SESSDATA` alone is enough to sign in; include the other fields if you have them.

If the cookie is long you can also paste it across lines (paste the whole thing as-is):

```
/bili login
cookie SESSDATA=xxx; bili_jct=yyy; DedeUserID=123
```

**A whole cookie can be pasted as-is** — no quotes and no escaping needed. Values such as `rpdid` legitimately contain `'`, `|`, `=`, and `"`; the gateway does no shell tokenization and passes the raw text through to the extension unchanged.

## Pitfalls Already Hit (read this before changing the extension)

| Symptom | Cause / Fix |
|---|---|
| `api.bilibili.com` returns **412** | It only risk-controls "browser-like UAs". Use a non-Mozilla UA; this extension pins `bilibili-cli-gateway/1.0` |
| Audio/video streams on the CDN return **403** | The CDN conversely rejects `curl`/`wget` UAs. One non-browser UA satisfies both sides |
| `fnval` rejected with **-400** or silently downgraded to 360p | The bit flags must **all be OR'd**: `4048 = 16\|64\|128\|256\|512\|1024\|2048`, missing even one breaks it (there is an assertion in the code) |
| `playurl` without `cid` returns **-400** | `cid` is a required parameter |
| The `nav` endpoint returns `code=-101` when signed out | But `data.wbi_img` is still usable; neither `wbi_keys()` nor `account_info()` may go through `require_ok` |
| Quotes in the cookie trigger `No closing quotation` | A former bug: the gateway split arguments with `shlex.split`, and the `'` in `rpdid=\|(u)\|Yl)JYlJ0J'u~Yu~mJ~Rk` was treated as a quote. Direct commands no longer do shell tokenization; the raw text travels via `TG_RAW_ARGS` |
| `b23.tv` short links are not recognized | The URL contains no BV id, so the 302 must be followed; `resolve_short_link()` sends `Range: bytes=0-0` to read only the `Location` and treats `?p=N` as the part number |
| `accept_quality` lies | It lists which tiers the video has (even signed out it may return `[126,120,116,80,64,32,16]`), but **the actual download is `data.quality`**, which is always 32 (480p)/16 (360p) when signed out |
| The primary CDN silently truncates | Primary hosts such as `akamaized` may deliver only 5% and then close the connection cleanly. Keep `backupUrl` and verify the byte count, re-downloading from another host on mismatch |
| Signed out, only 480p is available | Server-side entitlement. Adding `platform=html5` yields 1080p **durl**, but that is a complete MP4 that is 3–4× the size of DASH (measured: 34 minutes ≈ 94 MB), so it is off by default |
| Quality tiers and codecs | Within a tier prefer AVC (`avc1`); HEVC/AV1 shows black video in some players |

## Output

`{cwd}/{video title}/{video title}.mp4` (based on `TG_WORKDIR`, default `/srv/projects/downloads`). Audio and video are muxed with `ffmpeg -c copy` without re-encoding. Segmented downloads resume from a `.part` file.

Artifacts over `TELEGRAM_MAX_FILE_BYTES` (default 45 MiB) are not auto-sent; the card only shows a "path" button — so 4K and long videos almost always take that route.

## Dependencies

Only the standard library plus the system `ffmpeg` (for muxing). QR login additionally needs `segno`:

```bash
/home/openclaw/.local/bin/uv pip install --python \
  /srv/projects/telegram-cli-gateway/.venv/bin/python segno
```

(It is also declared as the optional extra `bilibili` in the repository's `pyproject.toml`; the gateway itself stays dependency-free.)
