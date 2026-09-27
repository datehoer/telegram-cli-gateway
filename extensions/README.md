# 直连命令扩展（Direct Command Extensions）

一个扩展 = 一个目录 + `manifest.json` + 一个可执行入口。命中命令时网关**直接 spawn 本地进程**，
不调用任何 AI CLI，不占用会话上下文，也不消耗 token。

```
extensions/
├─ README.md
└─ example/                 # 命令名来自 manifest.command，目录名随意但建议同名
   ├─ manifest.json
   └─ main.py
```

## 发现规则

| 来源 | 说明 |
|---|---|
| `<repo>/extensions/*/manifest.json` | 随仓库版本化，始终扫描 |
| `COMMAND_DIR` 指向的目录 | 可选，必须位于 `ALLOWED_WORKDIRS` 内，用于放在别处的扩展 |

- 修改 manifest 或新增扩展需要重启网关；**没有热重载**。
- 单个扩展出错只跳过它自己并写 warning 日志，不影响网关启动，也不影响其他扩展。
- `command` 与内置命令同名，或与已加载的扩展同名时，后者被跳过并告警。

## manifest.json

```json
{
  "schema_version": 1,
  "id": "bilibili",
  "name": "哔哩哔哩下载",
  "command": "bili",
  "usage": "/bili <URL> [1080p|720p]",
  "description": "下载 B站视频并回传 Telegram",
  "exec": ["python3", "main.py"],
  "cwd": "/srv/projects/downloads",
  "timeout_seconds": 1800,
  "max_output_bytes": 16384,
  "enabled": true
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | ✅ | 必须为 `1` |
| `id` | ✅ | `^[a-z][a-z0-9_]{0,31}$`，仅标识用 |
| `command` | ✅ | Telegram 上触发的命令名，同样的字符集 |
| `exec` | ✅ | argv 数组。**不经过 shell**，不要再自己拼引号 |
| `name` | ❌ | 展示名，缺省用 `id` |
| `usage` | ❌ | `/help` 和 `/commands` 里显示的用法 |
| `description` | ❌ | 一行说明，也用于 Telegram 的 `/` 菜单 |
| `cwd` | ❌ | 工作目录，缺省 `DEFAULT_WORKDIR`；必须位于 `ALLOWED_WORKDIRS` 内；相对路径按扩展目录解析 |
| `timeout_seconds` | ❌ | 1 ~ 21600，默认 1800；超时会 `SIGTERM` 进程组，5 秒后 `SIGKILL` |
| `max_output_bytes` | ❌ | 展示时保留的输出尾部字节数，默认 16384。下载进度这类长任务建议调大 |
| `enabled` | ❌ | `false` 时跳过（不告警） |

## 插件契约

网关传给插件的东西：

| 参数 | 内容 |
|---|---|
| `argv[1:]` | 网关**不做 shell 分词**。`argv[0]`（子命令）通过 `TG_ARGV0` 传，其余每个换行分隔的非空行是一个 argv 元素 |
| `TG_RAW_ARGS` | 用户输入的**原始文本**，一个字都没动过 |
| `TG_COMMAND_ID` / `TG_COMMAND` / `TG_COMMAND_NAME` | 扩展标识、命令名、展示名 |
| `TG_ARGS_JSON` | 按行拆好的参数 JSON 数组 |
| `TG_CHAT_ID` / `TG_USER_ID` | 发起者上下文 |
| `TG_WORKDIR` | 解析后的 `cwd` |
| `TG_MANIFEST_DIR` | 扩展目录的绝对路径 |
| `cwd` | 同 `TG_WORKDIR` |

插件返回给网关：

| 通道 | 语义 |
|---|---|
| stdout 中 `@@PROGRESS <文本>` 开头的行 | 进度。原地刷新进度卡片，**不进入最终输出** |
| 其余 stdout | 最终输出，按 `max_output_bytes` 截尾展示 |
| stderr | 失败时附加展示；成功时记录到日志 |
| 退出码 | `0` 成功，非 `0` 失败 |
| stdout 里的绝对路径 | 若位于 `ALLOWED_WORKDIRS` 内且文件确实存在，会挂上「发送文件/图片/视频」按钮（MP4 按视频发送） |

产物发送继承网关既有规则：

- `TELEGRAM_MAX_FILE_BYTES`（默认 45 MiB）是发送上限，来自 Bot API 的 50 MB 上传限制
- 超过上限的产物不会自动发送，卡片上只给「路径」按钮，点开回一条本机绝对路径
- `AUTO_SEND_ARTIFACTS`（默认 `off`）决定是否自动发送
- 想发超过 50 MB 的文件需要自建 Local Bot API Server，网关不代管

所以长视频扩展应该自己控制输出体积，或者在超过上限时明确只回路径。

## 安全模型

进程以网关账号权限运行，**与免审批的 CLI 完全同级**。网关只做三件事：

1. `exec` 是 argv 数组、`shell=False`，用户参数不会被 shell 解释；
2. `cwd` 与产物路径必须落在 `ALLOWED_WORKDIRS` 内；
3. 超时后杀整个进程组。

**除此之外没有沙箱。** 装扩展等于把代码放进网关进程的同权限环境，
所以只安装自己审过源码的扩展；不要使用来路不明的二进制扩展。

## 最小验证

```bash
# 直接跑插件本体（模拟网关传入的参数）
cd extensions/example
TG_WORKDIR=/srv/projects python3 main.py hello world
```
