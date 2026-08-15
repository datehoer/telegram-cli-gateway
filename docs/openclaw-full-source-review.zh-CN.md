# OpenClaw 完整源码能力与设计借鉴报告

> **范围说明：** 本文是设计研究和能力目录，不是项目路线图。当前项目边界与实现决策以根目录 `AGENTS.md` 为准；本项目依赖原生 CLI，不以复刻 OpenClaw 为目标。

> 研究对象：OpenClaw 官方仓库提交
> `c3e3a09fe6a564a7ab6968dfb7e681797264b23d`（2026-08-11）  
> 本地只读源码：`/srv/projects/openclaw-source`  
> 目标项目：`/srv/projects/telegram-cli-gateway`

本文所说的 OpenCloud 均按 OpenClaw 理解。研究不只统计功能，而是检查官方文档、实现入口、状态机、持久化、权限边界和测试文件，再判断哪些适合移植到“Telegram + 多个原生 CLI”网关。

## 一、先给结论

OpenClaw 实际上由五层组成：

```text
Telegram / Discord / Web / Mobile / CLI
                  │
        Gateway 控制面与身份层
                  │
    路由、会话、队列、任务与自动化层
                  │
  Agent runtime / Codex harness / CLI backend
                  │
 工具、浏览器、节点、Memory、Skill、插件
```

我们不应该复刻整个 OpenClaw。当前网关最适合演进成一个“小型 CLI 控制面”，继续把 Claude、Codex、Grok、Pi 的原生运行时当作执行引擎。

最应该借鉴的十项，按价值排序是：

1. 持久 Task Ledger 与重启对账。
2. 会话版本、过期按钮和幂等操作。
3. 原生 CLI 会话目录与接管，而不只展示网关创建的会话。
4. 分层 Memory、来源可信度和项目隔离。
5. 渐进式 Skill 目录、会话快照和受治理的学习提案。
6. 结构化运行轨迹与可导出的诊断包。
7. 自动化的“调度、条件判断、执行”三段式分离。
8. Secrets 运行时快照、末端注入和日志脱敏。
9. Model/CLI 故障切换只发生在尚未产生输出或副作用之前。
10. 远端 Node 抽象，为以后控制多台机器上的 CLI 留接口。

## 二、评价标准

| 级别 | 含义 |
| --- | --- |
| S | 对当前网关直接有价值，应该进入近期路线图 |
| A | 很有价值，但依赖持久化基础或明确使用场景 |
| B | 保留设计接口，出现需求后再实现 |
| C | OpenClaw 平台需要，当前项目不应照搬 |

成熟度使用四种标签：

- **核心**：稳定控制面的一部分。
- **可选成熟**：插件或功能默认关闭，但边界和恢复较完整。
- **实验**：源码和文档明确标为 experimental/Labs。
- **生态适配**：主要价值是兼容外部产品或供应商。

## 三、逐项功能分析

### 1. Gateway 控制面协议

**成熟度：核心；借鉴等级：A**

OpenClaw 用一个长连接 WebSocket 控制面统一 CLI、Web UI、桌面端、移动端和远端节点。协议由 TypeBox 闭合对象定义，首帧必须握手，返回服务器能力、方法、事件、权限和附件限制。请求、响应和事件分别拥有固定 envelope；错误包含机器可读 code、retryable 和 retryAfterMs。

关键设计：

- 客户端先声明协议版本区间、角色、能力、命令和权限。
- 服务端返回当前连接的策略快照，而不是让客户端写死限制。
- 有副作用的方法要求 idempotency key。
- 事件带 `seq` 或 `stateVersion`；客户端发现缺口后重新读取权威状态。
- 每个逻辑请求可携带独立 `traceparent`，不会把整个 WS 连接误当作一个 trace。
- 握手前、握手后分别限制帧大小和缓冲区，过大事件只记录元数据，不记录正文。

对当前网关的意义：Telegram 已经是前端，没必要立即增加 WebSocket。但是内部服务接口应当采用同样思想：命令 callback 携带资源 revision，所有写操作携带幂等键，Bot 重连后读取权威状态而不是相信旧按钮。

建议：

- 给 session、task、artifact 增加整数 revision。
- callback data 包含对象 id + revision；旧按钮返回“状态已变化，请刷新”。
- Telegram update id、用户消息 id、任务 id 建立幂等关系。

实现证据：`packages/gateway-protocol/src/schema/frames.ts`、`src/gateway/server/`。

### 2. 身份、设备配对和 Operator Scope

**成熟度：核心；借鉴等级：B**

OpenClaw 将身份认证和方法授权分开：token/password 证明能连接，operator scope 决定能读、写、管理、审批、配对、回答问题或使用 Talk。设备通过 challenge nonce 签名，批准后获得与角色绑定的设备 token；token 轮换不能突破已批准的角色和权限。

值得借鉴的不是复杂 PKI，而是两条原则：

- **白名单身份不等于所有动作权限。**
- **方法权限只是第一层，参数本身还要做风险判断。**例如普通 agent turn 需要 write，但 `/reset` 需要 admin。

当前只有一个高权限 Telegram 白名单用户，而且用户明确要求 CLI 免审批，因此不必增加逐命令审批。若以后允许第二个用户，至少应分出：只读观察者、可发 prompt 的操作者、可改配置/删除会话的管理员。

实现证据：`src/gateway/operator-scopes.ts`、`src/gateway/device-auth.ts`、`src/pairing/`。

### 3. 配置 schema、热更新和 Last-Known-Good

**成熟度：核心；借鉴等级：A**

OpenClaw 的配置不是随意读取 JSON：

- schema 拒绝未知键和错误类型；插件 schema 可以合并进主 schema。
- 配置写入使用原子替换和 compare-and-swap hash。
- 热更新构建完整候选快照，成功后一次发布，避免一半新一半旧。
- 失败的配置保存为 `.rejected.<timestamp>`，当前运行时继续使用已接受版本。
- 防止明显误覆盖，例如配置体积突然缩小一半或关键字段消失。
- Doctor 拥有迁移和恢复权，运行时不会悄悄把坏配置改回去。

当前 `.env` 很小，没必要复制其庞大 schema 系统；但应该增加 `gateway check` 的结构化检查，并让配置 reload 保留旧快照，尤其是以后加入 Memory、语音和自动化后。

### 4. Secrets 快照与末端注入

**成熟度：核心；借鉴等级：S**

这是 OpenClaw 很强但容易忽略的一部分。SecretRef 支持 env、受保护文件、直接 exec resolver 和共享 store。Secrets 在启动或 reload 时解析为一个内存快照，不在每次请求热路径重新读取。健康 owner 使用新值，失败但定义未变化的 owner 可以保留 last-known-good；身份入口等关键 owner 仍然 fail closed。

更高级的做法是 sentinel：模型 provider 链路中尽量传递进程内不透明占位符，真实凭据只在最终 HTTP header/URL 注入点展开。未知 sentinel 在出网前直接失败，已解析真实值同时注册日志精确脱敏。

当前项目应当先做精简版：

- 不再到处直接读取 dotenv 字典。
- 启动时生成只读 `SecretsSnapshot`，业务代码只拿需要的 token。
- 日志 formatter 注册 Bot token、API key 的精确值和常见变体进行脱敏。
- `/health` 只显示存在/不可用，不回显任何长度、前后缀或原始错误体。
- 后续支持 `file:` secret，但读取必须检查普通文件、owner 和 mode。

实现证据：`src/secrets/`、`docs/gateway/secrets.md`。

### 5. Channel Adapter 和统一 Inbound Envelope

**成熟度：核心；借鉴等级：A**

OpenClaw 将各消息平台的原始 update 转为统一事实：channel、account、conversation、sender、thread、reply、media、mention、callback 来源和授权方式。路由、命令授权、mention gate、ambient event 都只依赖这些事实，不在核心层解析 Telegram 特有 payload。

每个接入事件会形成一张可解释的访问图：route gate、sender gate、command gate、event-origin gate 和 mention gate。拒绝结果包含 decisive gate 和 reason code，日志中只暴露脱敏的匹配信息。

当前网关只有 Telegram，仍值得把 update 解析与业务命令拆开。这样未来加 Web UI、HTTP 或另一个 bot 时，不会复制 `/sessions`、附件和队列逻辑。

### 6. 路由、账号和 Multi-Agent Binding

**成熟度：核心；借鉴等级：A**

OpenClaw 的路由不是一串 if，而是固定优先级：精确 peer、父 thread、peer wildcard、guild+role、guild、team、account、channel、默认 agent。同层冲突由配置顺序决定；多个 match 字段使用 AND。解析结果不仅返回 agent，还返回 canonical session key 和 last-route policy。

当前网关中的“CLI 类型”更像 runtime，不等于 OpenClaw agent。我们应保留三层身份：

```text
Telegram chat/user -> gateway session -> runtime backend + native CLI session id
```

以后可以让同一个 Telegram bot 根据 topic、群、用户或项目默认选择不同 CLI，而不复制会话。

实现证据：`src/routing/resolve-route.ts`、`src/routing/session-key.ts`。

### 7. DM、群聊、Access Group、Mention 与 Ambient Room

**成熟度：核心；借鉴等级：B**

具体功能包括：

- DM pairing、allowlist、open、disabled。
- 可跨 channel 复用的 access group，但它只是名单别名，不是角色。
- 群聊 sender allowlist 与 room allowlist 分开。
- mention gating 支持平台原生 mention 和安全文本模式。
- ambient room 将未 @ 的群消息作为安静上下文；只有 agent 显式调用 message tool 才能公开说话。
- bot-to-bot 使用无方向 participant pair、滑动窗口预算和 cooldown 防止无限互聊。

当前默认私聊模式已经足够。若以后启用 Telegram group/topic，最值得借鉴的是：群内普通聊天不要自动变成可见回答；topic 必须成为 session route 的一部分；Bot 自己和其他 Bot 的消息需要 loop guard。

### 8. Streaming、Draft、可见回复和 Delivery

**成熟度：核心；借鉴等级：S（大部分已实现）**

OpenClaw 区分模型流、transport chunk、typing、临时 draft 和最终持久消息。它还区分“final text 可以自动发送”与“必须由 message tool 显式发送”。Delivery 是独立阶段，模型成功并不代表消息已经送达。

当前项目已经使用 Telegram Rich Draft 和 Rich Message，这是方向正确的。还缺：

- 最终 delivery 的持久 spool、两阶段确认和 dead-letter。
- Telegram 429 的 `retry_after` 与当前步骤级重试。
- message route 的 revision，防止旧回复按钮切到错误会话。
- 对“CLI 完成但 Telegram 最终发送失败”保持可重投记录。

### 9. 附件、媒体资产和短期访问能力

**成熟度：核心；借鉴等级：A**

OpenClaw 将附件和消息正文分离，限制解码后大小、Base64 后帧大小、MIME sniff、图片 hydration 上限。生成的媒体成为 managed artifact；HTTP 下载使用短期 ticket、Range、ETag 和 HEAD。音视频转换是异步、缓存和有时限的，失败回落到原文件。

Telegram 已经替我们承担下载鉴权与播放，所以不必实现 ticketed HTTP。应借鉴：

- artifact 是一等实体，有 owner session、来源、MIME、size、hash、保留期和发送状态。
- 附件路径不应只存在于聊天文字里。
- 文件和图片自动按钮应绑定 artifact id，而不是永久暴露绝对路径。

### 10. Agent Runtime / Harness / CLI Backend 分层

**成熟度：核心；借鉴等级：S（当前架构已采用）**

OpenClaw 把 model provider、agent runtime、channel 和工具分开。内置 runtime 与 Codex harness 实现同一个生命周期能力契约；Claude 等 CLI backend 是另一层。运行时选择依据的是注册能力和具体 provider route，而不是看到 `openai/` 就猜 Codex。

我们目前的 Codex app-server + 其他 CLI headless backend 正符合这一思路。下一步应正式定义统一 backend contract：

- create/adopt/resume session
- start/steer/interrupt turn
- structured event stream
- capabilities（steer、attachments、tool events、usage、native session catalog）
- terminal outcome 与 recoverability

不要让 `app.py` 继续通过 backend 类型判断累积特殊分支。

### 11. Model/Auth Failover

**成熟度：核心；借鉴等级：A**

OpenClaw 先在同 provider 内轮换 auth profile，再走 model fallback。关键安全边界：仅当尚未执行工具、尚未产生 assistant 输出时，纯 overload 才能自动重试整个候选链。一旦可能存在副作用，不能把完整 turn 在另一个模型上盲重放。Fallback 只对当前 turn 生效，不偷偷改写用户选中的 session model。

映射到多 CLI：

- Codex 启动前失败，可以建议改投 Claude/Pi。
- 已经看到命令执行或文件修改后，不自动换 CLI 重跑 prompt。
- fallback 应产生可见记录：“选择的是 codex，本轮实际由 claude 完成”。
- 用户明确 `/use codex` 属于严格选择，不应自动换引擎；自动新会话才可配置 fallback。

### 12. Session Store、目录、搜索、链接与多端接管

**成熟度：核心；借鉴等级：S**

OpenClaw 的 session 不只是 transcript：包含 canonical key、别名、运行状态、route、model、goal、usage、worktree、placement 和 recovery fences。它还提供：

- 通过完整 key、session id、label 或短 id 解析。
- session URL 可在 Web、TUI、移动端和 attach CLI 中打开同一个 Gateway session。
- FTS 搜索过去 session，结果先按可见性过滤，再应用数量上限。
- 原生 Codex、Claude、OpenCode、Pi session catalog，可从本机或 paired node 只读浏览。
- Codex/Claude 目录中的历史会话可在拥有它的机器上 `resume`；远程目录行不假装支持安全 continue/archive。
- Beam 可以只上传脱敏后的 coding session 快照，明确不提供远控能力。

这是当前网关除 Memory 外最大的机会：`/sessions` 不应只列我们创建的会话，还可以增加 `/discover` 或 `/native`，列出已有 Codex/Claude/Pi 会话，选择后创建一个 gateway binding。接管前必须区分：只读查看、在原机器终端打开、网关正式 adoption。

### 13. Session State Signal Log

**成熟度：核心；借鉴等级：S**

OpenClaw 用 append-only signal log 表示 session 状态变化，每个信号有单调版本。Watcher 保存 cursor；多个变化合并成一个 notice；处理 notice 时先冻结 watermark，再读取 `changesSince`，避免在通知处理中不断追赶新变化。重启从 cursor 恢复，旧信号有界清理。

当前 Telegram 动态状态可借用精简版：Task/Session 每次状态变化写 event row，Telegram updater 只消费到自己的 cursor。这样发送进度、最终消息、`/tasks` 和重启恢复使用同一事实源，不再由多个线程直接改消息。

### 14. Queue Steering

**成熟度：核心；借鉴等级：S（部分已实现）**

OpenClaw 把 `steer`、`followup`、`collect` 和 `interrupt` 明确定义：

- steer 只影响尚未启动的后续模型/工具边界，不撤回正在运行的命令。
- 顺序工具尾部被跳过时，仍写入成对的 synthetic tool result，保持 transcript 结构合法。
- parallel batch 只有一个原子 launch checkpoint。
- Codex 使用原生 `turn/steer`，由 Codex 自己决定模型边界。
- collect 经过 debounce 将兼容消息合并为一轮；interrupt 才真正终止当前 run。

当前 Codex steer 和其他 CLI FIFO 已实现。下一步应在 Telegram 暴露 `/queue steer|followup|collect|interrupt`，并持久化每条输入的模式和顺序。

### 15. Context Engine、Pruning、Compaction 与 Transcript Hygiene

**成熟度：核心；借鉴等级：B**

OpenClaw 将 context assembly 和 transcript storage 分开：插件化 Context Engine 可组装窗口、维护 transcript、压缩、处理 subagent 生命周期，还能告诉持久 backend “这个 context epoch 只 bootstrap 一次”。

另外有三套不同机制：

- pruning：只在请求内裁掉旧 tool output，不改磁盘 transcript。
- compaction：把旧对话总结成持久 summary，同时保留近期尾部和 tool-call/result 配对。
- transcript hygiene：按 provider 修复 call id、tool result 配对、thinking signature、图片尺寸和 turn alternation，但不篡改用户可见记录。

当前原生 CLI 已自己管理上下文，网关不应重复压缩 CLI transcript。我们应只管理 Telegram 侧的索引和摘要，用于搜索、Memory 候选和恢复 UI；真正发给 CLI 的历史仍由 native session 控制。

### 16. Goal

**成熟度：核心；借鉴等级：A**

Goal 是附着在 session 上的单一长期目标，不等于 task、cron 或 standing order。它跨重启存在，每轮以很短的上下文提醒模型；用户能 pause/resume/clear，模型只能在真实完成或反复遇到同一阻塞时标记 complete/blocked。可选 token budget 是行为护栏，不是账单上限。

对长时间远程 coding 很有用。建议以后加：

```text
/goal 修复部署脚本并完成测试
/goal status
/goal clear
```

目标应存于 gateway session，且只在启动新 turn 时追加一小段提示；不要每条状态更新都重复注入。

### 17. Task Ledger

**成熟度：核心；借鉴等级：S**

统一任务状态为 queued、running、succeeded、failed、timed_out、cancelled、lost；runtime 类型区分 subagent、ACP、CLI、cron。Task 还有独立 delivery 状态和通知策略。重启对账先问仍然拥有该工作的 runtime，再用持久历史判断，不能因为新进程内存表为空就认定任务消失。

这几乎可以直接映射当前项目：每个 Telegram prompt 都创建 task row；Codex turn id、Claude/Grok/Pi 进程、发送消息和 artifact 都通过 task id 关联。它应成为下一阶段的数据库基础。

### 18. Workboard

**成熟度：可选成熟；借鉴等级：B**

Workboard 是本地 Kanban，不只是卡片列表。它有依赖、claim token、heartbeat、attempt、proof、artifact、diagnostic、notification cursor、worker protocol violation 和 stale recovery。依赖完成后子卡才 ready；dispatcher 每轮有限并发且同一 owner 只选一张；worker 必须显式 complete 或 block，否则记录 protocol violation。

当前无需做完整看板，但 `/tasks` 应借两点：

- 任务结果要有“证据/产物”，不能只有 `done` 文本。
- 长任务要有 claim heartbeat，超时后才能回收，不能两个 worker 同时继续同一任务。

### 19. Subagents、ACP、Swarm 和 Specialist Lane

**成熟度：核心 + 实验组合；借鉴等级：B**

OpenClaw 的 subagent 使用独立 session、可选 fork context、push completion 和受限工具面。父 agent spawn 后不轮询，结束事件推回父 session；需要等待时主动 yield。嵌套深度、并发、模型和可见 session tree 都有边界。

ACP 是外部 coding harness 的会话协议；Swarm 则在 Code Mode 中提供结构化 fan-out。Parallel specialist lanes 强调按工作所有权拆 lane，而不是无脑增加 agent。

我们当前 CLI 本身已经是“专家 lane”。先不要让一个 CLI 自动大量 spawn 另一个 CLI。更现实的是未来提供显式 `/delegate claude ...`，创建独立子会话，并把结果作为带 provenance 的 inter-session message 返回当前会话。

### 20. Memory、User Model、Dreaming 和 Standing Intent

**成熟度：核心/可选；借鉴等级：S/A**

Memory 的完整结论已见另一份设计笔记。补充三项：

- `USER.md` 单独维护用户偏好，强调 supersede-in-place，避免不断追加互相矛盾的偏好。
- Dreaming 只对确定性筛选后的可信候选做合并，并保留 review/preimage；不是“睡一觉自由总结所有聊天”。
- Standing Intent 是事件型未来意图，使用本地 FTS 关键词、channel/sender scope、过期时间、cooldown 和 fire budget；每轮不调用模型。

Standing Intent 很适合 Telegram：例如“下次这个项目出现部署失败时提醒我检查回滚”。它应该晚于基础 Memory，但早于全功能自动化。

### 21. Skill、Workshop 与 Self-learning

**成熟度：核心/可选成熟；借鉴等级：A**

除渐进式目录和提案流程外，Self-learning 还有值得借鉴的准入条件：

- 只在足够深的成功或被用户纠正的 foreground turn 后评审。
- provider/prompt error、cron、heartbeat、memory、subagent 和评审自身不触发学习。
- 前台完成后安静 30 秒再后台评审，不拖慢回答。
- reviewer 只能产生一次有界 mutation，不能使用 message 或一般工具。
- trajectory 是证据而非指令；更新前必须完整读取目标 Skill 并绑定 hash。
- 一次性工作、个人事实、瞬态故障、泛泛建议和秘密都应 abstain。

当前项目应默认 `propose`，不应照搬 OpenClaw 当前文档中的默认 auto。多 CLI 的 prompt/工具输出来源复杂，先把学习提案发到 Telegram 供用户查看和应用。

### 22. Tool Search 与 Code Mode

**成熟度：实验；借鉴等级：C/B**

Tool Search 让模型先搜索/描述再调用大工具目录；Code Mode 只暴露 `exec`、`wait` 和少数不能跨 JSON bridge 的工具，模型在 QuickJS-WASI 中用小段 JS 搜索并组合隐藏工具。它减少 schema token、支持并行和数据变换，同时保持原工具的权限、hook 和 telemetry。

这对拥有上百工具的 OpenClaw 很重要，但我们只是调度四个原生 CLI，不应在网关再建一套模型工具 VM。可以借鉴的只有“能力目录 + 按需描述”，用于 `/capabilities` 和 backend feature detection。

### 23. Permission Mode、Sandbox、Tool Policy、Elevated、Approval

**成熟度：核心；借鉴等级：A（作为边界说明）**

OpenClaw 将五个常被混淆的概念分开：

1. tool policy 决定模型是否看得到工具。
2. sandbox 决定工具在哪里运行、能看哪些文件。
3. exec host 决定命令在 sandbox、Gateway 还是 node。
4. exec approval 决定 host 命令是否需要确认。
5. elevated 只影响 exec 是否越出 sandbox，并不增加工具。

当前用户要求四个 CLI 全部 bypass，因此网关无法把 `ALLOWED_WORKDIRS` 描述成安全沙箱；README 已正确提示这一点。我们仍应保留两个最低边界：只允许白名单 Telegram 身份，文件发送路径继续 realpath containment。若以后开放群聊，必须使用独立 OS 用户或真正容器，而不是依靠 prompt。

### 24. Exec 计划绑定和副作用防护

**成熟度：核心；借鉴等级：A**

OpenClaw 的审批不仅保存命令字符串，还尽力绑定 cwd、精确 argv、环境、已解析可执行文件和一个具体脚本文件。批准后执行的是保存的 canonical plan；脚本在批准与执行之间变化会被拒绝。无法明确绑定单一脚本的解释器调用不会伪装成安全。

虽然我们不审批 CLI 内部命令，但这个思想适用于 Telegram callback：确认删除 session 时，要绑定 session id、当前 revision 和归档状态，不能在确认期间让同名对象替换目标。

### 25. Browser 与 Computer Use

**成熟度：可选成熟/部分实验；借鉴等级：B**

Browser 有 managed isolated profile、用户真实 Chrome MCP attach 和浏览器扩展三种模式。它记录“由 agent 打开的 tab”所有权，不会清理用户原有 tab；可持久化识别的 target 在重启后继续有界清理。每次 action 基于 snapshot ref，页面变化后重新 snapshot；遇到登录、2FA、CAPTCHA、摄像头/麦克风权限时报告 manual action，不猜测绕过。

Computer Use 在 macOS/移动节点较成熟，Windows/Linux CUA 明确为实验。

当前网关不需要自己实现浏览器。原生 CLI 若已有 browser/MCP 就让它们负责。未来若要从 Telegram 控制真实登录态浏览器，优先做一个受限 node/browser adapter，不把 CDP 直接暴露给 Bot。

### 26. Plugins、Capability Contract 与 Bundle Compatibility

**成熟度：核心；借鉴等级：B**

OpenClaw 当前生成清单中有 58 个 core 插件、90 个官方外置插件。关键不是数量，而是：

- plugin 是功能/厂商所有权边界；capability 是多个 plugin 共同实现的核心契约。
- manifest metadata 和 config validation 不执行插件代码。
- activation plan 先根据命令、provider、channel、route、harness 缩小需要加载的插件。
- runtime snapshot 整体替换，不维护隐式 TTL metadata cache。
- native plugin、Codex/Claude/Cursor/Agent Plugins bundle 有不同信任边界。
- Bundle 可以映射 Skill、MCP、命令和部分 hook，但检测到不等于实际执行。

当前项目不需要通用 plugin loader。Backend registry、Memory provider、Speech provider 三个小型显式接口就够了；动态安装第三方 Python 代码会显著扩大风险和维护成本。

### 27. MCP 与远端能力发布

**成熟度：核心；借鉴等级：B**

OpenClaw 支持 Gateway 本机 MCP、bundle MCP 和 node-hosted MCP。Node 只公布工具描述，调用通过已批准的命令 family 返回原机执行；增加同 family 下的新 server/tool 不需要重新配对，但 Gateway 可全局禁用 node plugin tools。

当前多 CLI 各自管理 MCP。网关不要把所有 MCP 汇总后重复注入；可先只展示“本会话 CLI 启用了哪些 MCP/Skill”，用于诊断。

### 28. LLM Task、Lobster 与 Task Flow

**成熟度：可选成熟；借鉴等级：B/A**

三者解决不同问题：

- LLM Task：无 transcript、无工具、无 fallback 的一次 JSON-only 推理，可用 JSON Schema 验证。
- Lobster：小型确定性 pipeline DSL，执行到副作用前停下，返回 resume token；恢复不会重跑前面步骤。
- Task Flow：位于 task ledger 之上的多个 detached task 编排层，持久状态和 revision 独立于单任务。

对当前项目最有价值的是思想，而不是 DSL：复杂远程任务可保存 step checkpoint。重启后从最后一个确定完成的 checkpoint 继续，而不是把整个用户 prompt 再发一遍。等 task ledger 稳定后再考虑 `/workflow`。

### 29. Automation、Heartbeat、Event Trigger、Webhook、Standing Order

**成熟度：核心；借鉴等级：A**

OpenClaw 把几类自动化清楚区分：

- cron/at/every：精确时间调度。
- heartbeat：低成本合并检查，带 active hours 和静默返回约定。
- condition trigger：有界、无模型的观察脚本，只返回 fire/state；真正动作放在 payload。
- stream trigger：监督长进程，按行匹配和 quiet window 合并事件；每 job 只保留一个运行 payload 和一个待处理 batch。
- webhook：认证后映射外部事件到 Task Flow。
- standing order：写在 workspace 中的长期授权边界，定义 scope、approval 和 escalation；scheduler 只负责何时触发。

最值得照搬的是“观察与动作分离”：检查 CI 是否改变的脚本应只读并保存小状态，fire 后才启动 CLI。失败的动作不能提交新的观察 state，否则下次应能再次触发。

### 30. Node：远端执行、远端 Skill/MCP、文件和原生会话

**成熟度：核心；借鉴等级：A（中期）**

Node 是外设，不是第二个 Gateway。它声明 capabilities、commands 和 permissions，Gateway 仍然拥有路由、会话和模型。Node 可提供：

- `system.run`、which、PTY。
- camera、screen、location、notification、SMS、device data。
- node-hosted MCP 和 Skill。
- 本地 Ollama inference。
- Codex、Claude、OpenCode、Pi 的 session catalog 和有限 resume。
- 专用 file transfer，二进制不经过 shell stdout，最大 16 MB。

这很适合我们的长期方向：如果以后不同 CLI 分布在多台机器上，可实现一个小型 `gateway-node`，只提供 backend/session/file 能力，不把完整 OpenClaw Node 搬过来。Node 应用设备密钥配对，且每种命令 family 都显式批准。

### 31. Managed Worktree 与 Project

**成熟度：核心；借鉴等级：A**

OpenClaw 为 coding task 创建独立 git worktree 和 branch。删除前先验证是否无修改、无未 push commit；非无损情况保留。自动清理前创建 synthetic snapshot ref，tracked 和非 ignored untracked 进入 Git，显式复制的 ignored 文件单独保存在有界状态中，之后可恢复为 unstaged changes。

对网关很有价值，但要作为显式选项：

```text
/new codex --worktree my-feature
```

不要默认给每个会话创建 worktree。第一版可以只创建和绑定，不做自动 GC；等 task ledger 和 session deletion 安全确认成熟后再加 snapshot/restore。

### 32. Cloud Worker 与 Closed Worker Protocol

**成熟度：核心但部署复杂；借鉴等级：C/B**

OpenClaw 可把 agent loop 和工具放在临时云机器，模型凭据和 transcript 留在 Gateway。Worker 通过 Gateway 拥有的 SSH 反向隧道连接，使用严格关闭的 RPC allowlist、短期凭据、bundle hash、owner epoch、session binding 和每 RPC 重验。远端 workspace 结果先存为 Git refs，再三方合并回本地；冲突 keep-local 并保留远端 ref。

我们现在在一台主机运行 CLI，不值得实现。值得保留的模式是：远程 worker 永远不拿 Telegram token/模型凭据；结果先落入可恢复 staging，再合并到权威 workspace。

### 33. Control UI、TUI、Canvas、A2UI 与 Workboard UI

**成熟度：核心 + 实验；借鉴等级：B**

Control UI 是 Gateway 状态投影，不单独保存另一份 session。Plugin tab 和 widget 由握手 metadata 宣告；变更事件只带 epoch/revision，UI 收到后重读 canonical state，并在拖拽/编辑期间延迟 refresh，避免覆盖本地交互。

Canvas/A2UI 允许 agent 生成交互界面，属于实验方向。当前 Telegram inline keyboard 已够用。若以后页面需求增多，比在 Telegram 消息里塞复杂 UI 更合理的是做一个只读 Web dashboard，沿用同一 task/session store。

### 34. Voice Note、STT、TTS、Talk、Voice Wake、Voice Call

**成熟度：混合；借鉴等级：A/B**

OpenClaw 将语音拆成独立 capability：

- 普通 voice note transcription。
- TTS provider，支持流式、语音消息和电话 PCM。
- Talk：连续语音，可走本地 STT/TTS，也可走 realtime voice。
- Voice wake：关键词在 Gateway 持久化，节点本地识别并同步；只发送触发，不发送持续音频。
- Voice Call：Twilio/Plivo/Telnyx 等电话传输，独立于 chat channel。

当前计划中的 Telegram STT/TTS 应从最小能力开始：下载 voice note → 有界转写 → 将 transcript 作为明确标注的用户输入；回答完成后按命令 `/speak` 或用户设置生成 voice。不要先做持续 Talk、唤醒词或电话。

### 35. Meeting、实时字幕与持久会议记录

**成熟度：生态适配；借鉴等级：C**

Google Meet、Teams、Zoom 插件可通过本机或 paired node Chrome 参会，支持 agent、双向 realtime voice、只转写三种模式。完成的 caption 和摘要进入 transcript store，但不录制原始音视频。遇到登录、lobby、CAPTCHA 和平台政策时返回 manualAction。

与当前 CLI 网关关系弱。可借鉴“实时缓冲区和持久摘要分开”：以后 Telegram voice stream 也不应把每个临时 partial transcript 写入长期历史。

### 36. Image/Video/Music Generation、Document Extract 与 Media Understanding

**成熟度：能力插件；借鉴等级：B**

OpenClaw 用统一 capability contract 让 provider 提供图像、音乐、视频、语音和媒体理解。文档附件优先提取文本，必要时回退页面图片；媒体理解按 image/audio/video 能力选择模型或 CLI fallback，并限制文件数、大小、时长和并发。

当前网关只需正确把附件传给已有 CLI。只有在某些 CLI 无法理解 PDF/音频时，才在 gateway 前置 document/STT adapter；结果要标注为 derived content，不能冒充用户原文。

### 37. Presence、Active Computer 与通知路由

**成熟度：可选成熟；借鉴等级：C/B**

OpenClaw 的 active computer 不信任节点上报的 wall clock，而由 Gateway 用观察时间减去 idle seconds；只上报空闲时长，不上传键值、坐标、窗口标题。模型 prompt 只得到稳定 node id，精确时间和可注入的 display name 留在工具查询中。

当前只有 Telegram，不需要 presence。若以后加入多主机，状态 prompt 只注入稳定 machine id，显示名称等不可信 metadata 不应进入 system context。

### 38. Logbook 与 Beam

**成熟度：可选成熟；借鉴等级：C/B**

Logbook 定时抓取 paired node 屏幕，让视觉模型生成可回顾工作日志，隐私风险很高，当前不建议实现。

Beam 则很值得理解：外部机器只上传严格大小限制、脱敏、文本化的 coding session 快照；Gateway 不能反向连接、不能执行命令、不能接管 session。它提供“观察”而非“控制”。未来若要查看另一台机器的 Claude/Codex 进度，但还不想部署 node，可以做 Beam 类只读推送。

### 39. Trajectory、Audit、Telemetry 与 Usage

**成熟度：核心/可选 exporter；借鉴等级：S/A**

四者职责不同：

- trajectory：单 session flight recorder，包含 prompt 编译、工具、fallback、usage、compaction 和结束原因，可导出有界脱敏包。
- audit：长期 metadata-only 审计，默认不保存正文，明确声明覆盖范围不等于完整证明。
- OpenTelemetry/Prometheus：队列、session、模型调用、工具、Talk、delivery 等指标和 trace。
- usage：模型实际使用、fallback、cache hit、cost 和 provider quota 的展示层。

当前最值得先做轻量 trajectory：每个 task 保存 backend 事件摘要、命令名、时间、状态、stderr 尾部、Telegram delivery id，但不保存秘密和完整命令输出。`/diagnostics <task>` 生成一个 JSON/Markdown 包，远程 debug 会容易很多。

### 40. Doctor、Health、Backup、Migration 和 Singleton Lock

**成熟度：核心；借鉴等级：S/A**

OpenClaw 将职责分开：

- health 是在线轻量探针。
- doctor 是离线/在线诊断、schema lint 和明确迁移工具；repair 必须显式调用。
- backup 对 SQLite 使用 online backup API，不复制活跃 WAL；verify 校验 hash、integrity、foreign key 和 schema；restore 只写新目标，不原地覆盖活库。
- Gateway lock 同时使用 state owner lock、config lock 和端口 bind；PID 之外还校验 process start identity，避免 PID 重用。

当前项目已有单实例锁和 `--check`。应升级：

- `/health` 显示 Bot、四 CLI、runtime dir、磁盘、任务压力和最后成功 delivery。
- `gateway doctor` 检查 orphan process、native session id、不可写路径、过期 artifact 和坏 state。
- JSON state 迁移到 SQLite 后再加入 snapshot/verify；恢复永远写到新文件。

### 41. Security Threat Model

**成熟度：核心文档；借鉴等级：S**

OpenClaw 明确列出 channel access、session isolation、tool execution、external content 和 supply chain 五个信任边界。它也承认外部内容 wrapper 不能彻底解决 prompt injection，Operator Scope 不是敌对多租户隔离，SecretRef 不是进程隔离。

对当前 bypass 模式必须保持同样诚实：Telegram 白名单账户相当于主机远程 root-like operator。真正的安全边界是 Bot token、Telegram 账户安全、运行网关的 OS 用户和主机备份，不是 `ALLOWED_WORKDIRS`。

## 四、完整能力映射表

| 能力 | OpenClaw 形态 | 当前网关建议 | 优先级 |
| --- | --- | --- | --- |
| Typed Gateway protocol | WS schema + capability handshake | 暂不建 WS，内部对象加 schema/revision/idempotency | A |
| Device pairing/scopes | 设备签名、角色、scope | 单用户先不做，多用户时再加 | B |
| Config/LKG | schema、原子发布、拒绝版本 | 增强 `--check` 和 reload snapshot | A |
| SecretRef | 内存快照、末端注入 | 先做 SecretsSnapshot + 日志脱敏 | S |
| Channel adapters | 标准 ingress envelope | 重构 Telegram 解析与业务层 | A |
| Routing/binding | peer/account/topic 到 agent/session | 为 topic、多用户、多机器预留 | A |
| Ambient room | 安静上下文 + 显式发言 | 仅启用群聊后需要 | B |
| Bot loop guard | pair window + cooldown | 群聊/接 bot 后加入 | B |
| Rich streaming | draft + final delivery | 已实现，补持久 delivery | S |
| Artifact model | 附件独立实体 | 改造路径按钮为 artifact id | A |
| Backend capability contract | runtime/harness/backend | 正式抽象四个 CLI | S |
| Model/CLI fallback | 无副作用前切换 | 只在启动前失败时建议/自动切换 | A |
| Native session catalog | 发现并查看原生历史 | `/native` 或扩展 `/sessions` | S |
| Session search | FTS + visibility | SQLite 后实现 | A |
| Session signal log | version + watcher cursor | 作为进度和重启恢复事实源 | S |
| Queue modes | steer/followup/collect/interrupt | 暴露 `/queue` 并持久化 | S |
| Context/compaction | context engine | 交给原生 CLI；只索引 Telegram 历史 | B |
| Goal | 单 session 长期目标 | `/goal` | A |
| Task ledger | 状态、delivery、reconcile | 下一阶段地基 | S |
| Workboard | Kanban + claims/proof | 只借 task proof/heartbeat | B |
| Subagent/delegate | isolated child + push result | 以后做显式 `/delegate` | B |
| Memory | tier/provenance/FTS | 近期实现 | S |
| Standing intent | deterministic future trigger | Memory 后实现 | A |
| Skill picker | bounded catalog/snapshot | `/skills` | A |
| Self-learning | governed proposal | 默认 propose，后期实现 | B |
| Code Mode/Tool Search | 大型工具目录压缩 | 不在网关重复实现 | C |
| Sandbox/approval | 多层权限 | bypass 下只保留真实边界说明 | A |
| Browser | profile + tab ownership | 交给 CLI，必要时 node adapter | B |
| MCP aggregation | local/bundle/node tools | 只做诊断展示 | B |
| Workflow checkpoint | Lobster/Task Flow | task ledger 后再做 | B |
| Cron/heartbeat | scheduler + monitor | 有监控需求时加入 | A |
| Event watcher | read-only gate + action payload | 很适合 CI/服务监控 | A |
| Node | remote host capability | 中期多机器方向 | A |
| Worktree | isolated git checkout | 显式 `/new --worktree` | A |
| Cloud worker | ephemeral remote execution | 不实现 | C |
| Web dashboard | canonical state projection | Telegram UI 不够时再做 | B |
| STT/TTS | provider capability | 按原计划后续做 | A |
| Meetings/voice call | 专用 transport | 不实现 | C |
| Beam | 只读远端 session 推送 | 多机器观察场景可用 | B |
| Logbook | 屏幕工作日志 | 隐私风险高，不实现 | C |
| Trajectory | session flight recorder | 近期轻量实现 | S |
| Audit/metrics | metadata + exporter | task ledger 后加入 | A |
| Doctor/backup | 检查、迁移、snapshot | SQLite 化后加入 | S/A |

## 五、推荐的目标架构

```text
Telegram Adapter
  ├─ auth / command / callback revision
  └─ rich draft + durable delivery worker
                         │
Gateway Core             │
  ├─ SQLite session store│
  ├─ task ledger + events┘
  ├─ artifact registry
  ├─ memory index
  ├─ skill catalog
  └─ automation scheduler
            │
Backend Registry
  ├─ Codex app-server
  ├─ Claude headless/native catalog
  ├─ Grok headless/native catalog
  └─ Pi headless/native catalog
            │
Optional Node Agent（以后）
  └─ remote backend / files / native sessions
```

### 核心数据实体

建议从 JSON state 逐步迁到 SQLite，并固定以下实体：

- `sessions`：gateway id、backend、native id、cwd、label、status、revision、goal。
- `tasks`：prompt、session、状态、run/turn/process identity、timestamps、recovery disposition。
- `task_events`：append-only 状态、tool/command 摘要、progress、terminal event。
- `deliveries`：Telegram target、payload kind、attempt、delivered marker、dead-letter。
- `artifacts`：路径、hash、MIME、owner task/session、retention。
- `memory_documents` / FTS：canonical file 与 derived chunks/provenance。
- `skills_snapshot`：session、name、path、hash、eligibility。
- `idempotency_keys`：Telegram update/callback 到写操作结果。

## 六、实际路线图

### Phase 1：可靠控制面

1. SQLite task ledger。
2. 持久 inbound queue 和 outbound delivery。
3. task/session revision 与旧按钮保护。
4. `/health`、`/diagnostics`、启动 reconcile。

### Phase 2：会话能力

1. Backend capability contract。
2. 原生 Codex/Claude/Pi/Grok session catalog。
3. `/queue` 四种模式。
4. `/goal` 和 session FTS search。

### Phase 3：知识层

1. `MEMORY.md`、`USER.md`、daily notes、FTS5。
2. `/remember`、`/memory`、`/forget`。
3. `/skills` 只读目录与 session snapshot。
4. project key 与来源可信度。

### Phase 4：自动化与隔离

1. one-shot/every/cron。
2. condition watcher 与 standing intent。
3. managed worktree。
4. proposal-only self-learning。

### Phase 5：远程主机与语音

1. Telegram voice-note STT 与按需 TTS。
2. 小型 paired node 协议。
3. 远端 CLI session catalog、文件传输和 backend 执行。

## 七、不建议照搬的部分

- 148 个官方插件的完整安装/市场体系。
- OpenClaw 自己的模型 provider 层；已有 CLI 已经承担 provider/auth。
- QuickJS Code Mode 和大型 Tool Search。
- 默认自动应用的 self-learning；先使用 proposal。
- Canvas/A2UI、会议机器人、电话和连续 Talk。
- Cloud worker 和 fleet，除非未来确实需要临时算力。
- Logbook 屏幕捕获。
- 把 Operator Scope 当作敌对多租户隔离。
- 在已经产生 CLI 副作用后自动换另一个 CLI 重放整轮。

## 八、源码索引

| 主题 | 主要源码/文档 |
| --- | --- |
| Gateway 协议 | `packages/gateway-protocol/src/`, `src/gateway/` |
| 路由和 session key | `src/routing/` |
| Channel ingress | `src/channels/inbound-event/`, `src/channels/message-access/` |
| Task/Flow | `src/tasks/` |
| Scheduler | `src/cron/` |
| Agent runtime | `src/agents/`, `packages/agent-core/` |
| Context | `src/context-engine/` |
| Recovery | `src/agents/main-session-recovery/`, `src/agents/subagents/registry/` |
| Memory | `packages/memory-host-sdk/`, `extensions/memory-core/`, `extensions/active-memory/` |
| Skills | `src/skills/` |
| Secrets | `src/secrets/` |
| Audit/trajectory | `src/audit/`, `src/trajectory/` |
| Nodes | `src/node-host/`, `src/gateway/node-*` |
| Browser | `extensions/browser/` |
| Workboard | `extensions/workboard/` |
| Voice | `src/talk/`, `src/tts/`, `src/realtime-transcription/`, `extensions/voice-call/` |
| Plugins | `src/plugins/`, `src/plugin-sdk/`, `packages/plugin-sdk/` |

### 在线主资料

- 官方仓库：<https://github.com/openclaw/openclaw>
- Gateway 协议：<https://github.com/openclaw/openclaw/blob/main/docs/gateway/protocol.md>
- Gateway 客户端：<https://docs.openclaw.ai/gateway/clients>
- Node：<https://docs.openclaw.ai/nodes>
- Self-learning：<https://docs.openclaw.ai/tools/self-learning>

这份报告描述的是在当前提交上可见的真实实现。OpenClaw 仍高速变化，未来借实现细节时应固定 commit 并重新核对协议、schema 和迁移行为。
