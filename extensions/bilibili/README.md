# /bili — 哔哩哔哩下载扩展

不用账号就能用，但 **DASH 流被卡在 480P**；登录后 1080P；大会员 4K/HDR/杜比。
未登录想要更高清晰度，只有 `--full` 走 durl 单文件（能到 720P，代价是体积大 3~4 倍）。

```
/bili https://b23.tv/dfBSIGr        # b23.tv 短链会先跟随跳转再解析
/bili BV1xx411c7mD                  # 默认取账号可用的最高画质
/bili BV1xx411c7mD 1080p            # 指定画质
/bili BV1xx411c7mD -p 3 720p        # 第 3 个分P
/bili BV1xx411c7mD --audio-only     # 只下音频（m4a）
/bili BV1xx411c7mD 720p --full      # 未登录时用 durl 单文件换清晰度（体积大 3~4 倍）
/bili ep123                         # 番剧单集
/bili https://www.bilibili.com/bangumi/play/ss456
```

## 登录

两种方式，都写进 `cookie.json`（0600 权限，**不进 `.env`**）。

```
/bili login cookie SESSDATA=xxx; bili_jct=yyy; DedeUserID=123
/bili login                          # 扫码：自动发二维码图片，扫码成功自动保存
/bili whoami                         # 看当前账号与画质上限
/bili logout                         # 清除本机 cookie
```

Cookie 最省事：浏览器登录 B站 → F12 → Network → 任意请求 → 复制整条 `Cookie`。
只需要 `SESSDATA` 就够登录，其余字段有则一起带上。

Cookie 太长时也可以换行（整段原样粘贴即可）：

```
/bili login
cookie SESSDATA=xxx; bili_jct=yyy; DedeUserID=123
```

**整条 Cookie 可以原样粘贴**，不需要加引号、不需要转义。`rpdid` 这类值里带 `'`、`|`、`=`、`"`
都是正常的，网关不做 shell 分词，原文会一字不改地传给扩展。

## 已实测的坑（改动前请先读）

| 现象 | 原因 / 做法 |
|---|---|
| `api.bilibili.com` 返回 **412** | 只对「浏览器式 UA」风控。必须用非 Mozilla UA，本扩展固定 `bilibili-cli-gateway/1.0` |
| CDN 上的音视频流 **403** | 反过来要求 UA 不是 `curl`/`wget`。同一个非浏览器 UA 两边都能过 |
| `fnval` 被拒 **-400** 或静默降到 360P | 位标志必须**全部 OR**：`4048 = 16\|64\|128\|256\|512\|1024\|2048`，少一位都不行（代码里有断言） |
| `playurl` 少了 `cid` 直接 **-400** | `cid` 是必填参数 |
| `nav` 接口未登录返回 `code=-101` | 但 `data.wbi_img` 仍然可用；`wbi_keys()` 和 `account_info()` 都不能走 `require_ok` |
| Cookie 里的引号触发 `No closing quotation` | 曾经的 bug：网关用 `shlex.split` 拆参数，而 `rpdid=|(u)|Yl)JYlJ0J'u~Yu~mJ~Rk` 里的 `'` 会被当引号。现在直接命令不做 shell 分词，整段原文走 `TG_RAW_ARGS` |
| `b23.tv` 短链识别不了 | URL 里没有 BV 号，必须跟随 302；`resolve_short_link()` 带 `Range: bytes=0-0` 只取 Location，并把 `?p=N` 当分P |
| `accept_quality` 会骗人 | 它列的是「这视频有哪些档」（未登录也可能返回 `[126,120,116,80,64,32,16]`），**实际下发看 `data.quality`**，未登录恒为 32(480P)/16(360P) |
| 主 CDN 静默截断 | `akamaized` 等主地址可能只传回 5% 就正常关闭连接。必须保留 `backupUrl` 并校验字节数，对不上就换地址重下 |
| 未登录只能拿 480P | 服务端权限。加 `platform=html5` 能拿到 1080P **durl**，但那是完整 MP4，体积是 DASH 的 3~4 倍（实测 34 分钟 ≈ 94 MB），默认关闭 |
| 画质档位与编码 | 同档位优先 AVC（`avc1`），HEVC/AV1 在部分播放器会黑屏 |

## 输出

`{cwd}/{视频标题}/{视频标题}.mp4`（按 `TG_WORKDIR`，默认 `/srv/projects/downloads`）。
音视频用 `ffmpeg -c copy` 合流，不重新编码。分片下载带 `.part` 断点续传。

超过 `TELEGRAM_MAX_FILE_BYTES`（默认 45 MiB）的产物不会被自动发送，卡片上只给一个
「路径」按钮 —— 所以 4K/长视频基本都会走这条路。

## 依赖

只依赖标准库 + 系统 `ffmpeg`（合流用）。扫码登录额外需要 `segno`：

```bash
/home/openclaw/.local/bin/uv pip install --python \
  /srv/projects/telegram-cli-gateway/.venv/bin/python segno
```

（也声明在仓库 `pyproject.toml` 的可选 extra `bilibili` 里，网关本体仍然零依赖。）
