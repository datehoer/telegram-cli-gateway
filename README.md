# Telegram CLI Gateway

通过一个 Telegram Bot 远程使用本机的 Claude Code、Codex、Grok 和 Pi，并在多个持久会话之间切换。

项目的产品边界和实现原则见 [`AGENTS.md`](AGENTS.md)：这是原生 CLI 的轻量 Telegram 控制层，不是通用 Agent 平台。

## 当前能力

- Telegram 用户 ID 白名单，默认只接受私聊
- `/new`、`/use`、`/back`、`/sessions`、`/tasks`、`/where`、`/interrupt`、`/resume`、`/cancel`
- `/reload` 可点选重载当前会话或全部 CLI；Codex 会重启共享 app-server 并恢复全部原生 thread，Claude/Grok/Pi 的下一轮任务天然使用当前安装版本
- `/tasks` 提供中断当前、清空等待和中断并清空按钮；每个会话最多排队 20 条输入
- `/sessions` 返回 Telegram 按钮，可切换、中断、归档、恢复和确认删除会话；每个会话显示状态徽章（🟡 运行中 / 🟢 空闲 / ⚫ 上次失败 / 🔴 已中断待恢复 / ⏳ 排队 N）
- `/rename` 设置易读会话名称；`/sessions all` 查看已归档会话
- `/model` 查看/切换当前会话模型，`/effort` 查看/切换推理力度（下次任务生效；选择项来自各 CLI 自己的模型目录，改完直接透传给 CLI，网关不接管模型路由）
- 回复任意历史 CLI 结果会自动切回对应会话，再把消息发送给它
- Codex 使用 `app-server`；Claude、Grok、Pi 使用各自的 headless JSON 流
- Codex 运行中收到新消息时使用原生 `turn/steer` 追加到当前任务；其他 CLI 先按会话 FIFO 排队并在完成后自动继续
- 任务被网关重启/关机连累中断后：网关重启时提示“回复 /resume 继续，或 /cancel 放弃”，/resume 通过 CLI 原生 resume 续跑同一会话（改网关代码后重启自己这类场景可配合 `AUTO_RESUME=true` 自动续跑）；用户主动 /interrupt 的任务不会进入恢复候选
- 使用原生 Rich Message 原地编辑流式更新回答、当前命令和运行秒数（不用 Draft：Draft 一旦被中断无法由网关收尾，会留下永久“加载中”气泡）
- 多会话并发时只直播“当前” session：切到谁就在底部发一条每 10 秒原地刷新的进度卡片；切走的那条定格为“后台运行中”，任务跑完后照样把结果更新到它的卡片；稍后切回空闲 session 时会把最后一条成功结果复制到聊天底部，`/clear` 后则显示简短的新对话提示；来回切换时残留的旧卡片会在任务结束时收尾为一句短状态
- Telegram 限流或临时失败时按 `retry_after` 或封顶指数退避重试消息更新
- `/tgstats` 查看今日和最近 7 天的 Telegram 新消息、编辑、失败、429 与本地延后次数；按 UTC 聚合，只保留最近 14 天，不记录消息内容或 chat_id
- 使用原生 Rich Markdown 渲染表格、标题、列表、任务列表、链接、引用、公式和代码块
- Rich Messages 不可用时自动回退到安全 HTML/纯文本
- 接收 Telegram 文件和图片并交给当前 CLI
- 回答提到允许目录内真实存在的文件时，自动附加“一键发送文件/图片”按钮
- 回答完成后自动发送产物：`AUTO_SEND_ARTIFACTS=images`（默认，只自动发≤10MB 的图片）/ `all`（图片+文件）/ `off`（仅按钮）
- 会话 ID、本地状态和 CLI 上下文持久化
- 单实例文件锁

语音转文字和文字转语音尚未接入。

## 权限模式

四个 CLI 都按免审批方式运行：

- Codex：`approvalPolicy=never`、`sandbox=danger-full-access`
- Claude Code：`--dangerously-skip-permissions`
- Grok：`--always-approve --permission-mode bypassPermissions`
- Pi：`--approve`

这意味着白名单账号发出的提示可以让 CLI 读写本机文件并执行命令。请把 Telegram Bot token 和白名单账号视为高权限凭据，不要开启陌生用户或不受控群聊。

`ALLOWED_WORKDIRS` 限制会话起始目录以及网关可发送的本地产物路径，但它不是 CLI 的文件系统沙箱；免审批 CLI 本身仍能访问其操作系统账号有权访问的内容。

## 配置

项目只需要 Telegram token、白名单和 CLI 路径。创建 `.env` 并确保只有当前用户可读：

```bash
cp .env.example .env
chmod 600 .env
./scripts/run.sh --check
```

需要多个 Telegram Bot 入口时，直接增加任意非空的
`TELEGRAM_BOT_TOKEN_<名称>`，无需额外开关：

```dotenv
TELEGRAM_BOT_TOKEN=主入口_token
TELEGRAM_BOT_TOKEN_2=第二入口_token
TELEGRAM_BOT_TOKEN_RESEARCH=研究入口_token
```

所有 Bot 共享同一套 CLI session。每个 Bot 对话的当前 session 与返回历史独立；
一个 Bot 切换 session 不会影响另一个。任务结果始终返回发起任务的 Bot，其他 Bot
可用 `/use <会话ID>` 主动读取该 session 的最新运行快照或本次网关运行期间缓存的
最近结果。运行中从另一 Bot 追加输入不会改变原任务的回复入口。`<名称>` 会作为
持久状态键使用，配置后不要随意改名。

常用配置：

```dotenv
TELEGRAM_ALLOW_GROUPS=false
DEFAULT_WORKDIR=/srv/projects
ALLOWED_WORKDIRS=/srv/projects
STREAM_UPDATE_INTERVAL=10
TELEGRAM_MAX_FILE_BYTES=20971520
AUTO_RESUME=false
ENABLED_CLIS=claude,codex,grok,pi
```

`AUTO_RESUME=true` 时，网关重启后会自动续跑上次被意外中断的任务（用户主动 /interrupt 的除外）；默认 `false`，收到提示后用 `/resume` 手动恢复、用 `/cancel` 放弃。

`ENABLED_CLIS` 可把一个网关实例限制为指定 CLI；例如 `ENABLED_CLIS=codex`。未启用的 CLI 不能创建或切换会话，Codex 未启用时也不会启动其 app-server。

附件保存在 `.runtime/uploads/`，目录权限为 `0700`、文件权限为 `0600`。默认最大 20 MiB。

## 使用

```text
/new
/new codex /srv/projects/my-project
/new claude
/use grok
/sessions
/tasks
/tgstats
/rename codex-a8d1 主项目
/back
/interrupt
/resume
/cancel
/reload
/reload current
/reload all
/model
/model sonnet
/effort high
```

普通文字直接发给当前会话。发送文件或图片时，caption 会作为提示；没有 caption 时使用“请查看并处理这个附件”。

`/reload` 只重启已配置 CLI 的运行后端，不会重读 `.env` 或 Gateway 源码；修改配置或 Gateway 本身后仍需重启 systemd 服务。任何目标会话仍在运行时，重载会拒绝执行，不会自动中断或重放任务。

项目使用 `uv` 管理本地 `.venv`，`scripts/run.sh` 会优先通过 `/home/openclaw/.local/bin/uv` 启动并在找不到 uv 时回退到系统 Python。

私聊中的运行状态先发送一条 Rich Message，任务运行期间每 10 秒原地编辑更新（回答、当前命令、运行秒数），任务完成后把同一条消息编辑为最终答案并附产物按钮；编辑失败时回退到安全 HTML 消息。不使用临时 Draft，因为被中断的 Draft 无法由网关收尾，会留下无法消除的“加载中”气泡。

## systemd 用户服务

```bash
mkdir -p ~/.config/systemd/user
cp systemd/telegram-cli-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telegram-cli-gateway
journalctl --user -u telegram-cli-gateway -f
```

## 测试

```bash
uv run python -W error::ResourceWarning -m unittest discover -s tests -v
```

## 三 Bot 隔离实验

Codex、Claude Code 和 Grok 的三容器隔离部署见 [`docker/experiments/README.md`](docker/experiments/README.md)。这是独立 gateway 实例的部署方式，不在 gateway 内增加多 Agent 编排。
