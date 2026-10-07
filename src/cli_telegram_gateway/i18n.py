"""User-facing text: English in the code, with an optional Chinese catalog.

Code keeps the English text inline and passes it through ``tr``. ``GATEWAY_LANGUAGE``
selects the language once at startup. Text without a translation stays English.
Logs, comments and prompts sent to the CLIs are English only.
"""

from __future__ import annotations

from collections.abc import Iterable

LANGUAGES = ("en", "zh")

_language = "en"


def set_language(language: str) -> None:
    global _language
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language: {language}")
    _language = language


def current_language() -> str:
    return _language


def tr(text: str, /, **values: object) -> str:
    """Translate ``text`` and fill its ``{name}`` placeholders from ``values``."""
    template = ZH.get(text, text) if _language == "zh" else text
    return template.format(**values) if values else template


def join_items(items: Iterable[str]) -> str:
    """Join a short list the way the configured language writes one."""
    return ("、" if _language == "zh" else ", ").join(items)


def N_(text: str) -> str:
    """Mark text for translation where it is defined; ``tr`` translates it where shown."""
    return text


ZH: dict[str, str] = {
    # app.py
    """Remote CLI gateway

/new [cli] [dir]       New session; without arguments, pick a CLI
/use <session-id|cli>  Switch; creates a session if that CLI has none
/back                  Return to the previous session
/sessions              List sessions
/tasks                 Show running tasks and queues
/tgstats               Show Telegram write and rate-limit stats
/where                 Show the current session and directory
/status                Show CLI status, context usage and account quota
/context               Show context usage and space left
/interrupt             Interrupt the current task
/resume [session]      Continue the last interrupted task
/cancel [session]      Give up the last interrupted task
/compact               Compact the current session's context (Codex, Pi)
/clear                 Start a new conversation and clear the context
/reload [current|all]  Reload the current session's CLI or all CLIs
/stop [session-id|cli] Stop a session
/rename [session] <name>  Rename a session
/archive [session]     Archive a session
/delete [session]      Delete a session (asks to confirm)
/model [name]          Show or switch the current session's model
/effort [level]        Show or switch the reasoning effort
/send <text>           Send text to the current CLI explicitly
/commands              List direct command extensions
/help                  Show this help

CLIs enabled for this bot: {clis}.
Plain text, files and images go straight to the current session. All CLIs run without approval prompts.
{extensions}""": """远程 CLI 网关

/new [cli] [目录]  新建会话；无参数时点选 CLI
/use <会话ID|cli>  切换；该 CLI 无会话时自动新建
/back               回到上一个会话
/sessions           查看会话列表
/tasks              查看当前任务与等待队列
/tgstats            查看 Telegram 写入与限流统计
/where              查看当前会话和目录
/status             查看 CLI 状态、上下文用量与账户额度
/context            查看上下文用量与剩余空间
/interrupt          中断当前任务
/resume [会话]      继续上次被中断的任务
/cancel [会话]      放弃上次被中断的任务
/compact            压缩当前会话上下文（Codex、Pi）
/clear              当前会话开新对话，清空上下文
/reload [current|all] 重载当前会话或全部 CLI
/stop [会话ID|cli]  停止会话
/rename [会话] 名称  重命名会话
/archive [会话]     归档会话
/delete [会话]      删除会话（需确认）
/model [名称]       查看或切换当前会话模型
/effort [级别]      查看或切换推理力度
/send <文本>        显式发送给当前 CLI
/commands           列出直连命令扩展
/help               查看帮助

此 Bot 启用的 CLI：{clis}。
普通文本、文件和图片会直接交给当前会话。所有 CLI 均已启用免审批模式。
{extensions}""",
    "Create a CLI session": "新建 CLI 会话",
    "Switch CLI or session": "切换 CLI 或会话",
    "Return to the previous session": "返回上一个会话",
    "List sessions": "列出会话",
    "Show the task queue": "查看任务队列",
    "Show Telegram API stats": "查看 Telegram API 统计",
    "Show the current session": "显示当前会话",
    "Show CLI status and quota": "查看 CLI 状态与额度",
    "Show context usage": "查看上下文用量",
    "Interrupt the current task": "中断当前任务",
    "Continue the last interrupted task": "继续上次被中断的任务",
    "Give up the last interrupted task": "放弃上次被中断的任务",
    "Compact the session context": "压缩当前会话上下文",
    "Start a new conversation": "当前会话开新对话",
    "Reload CLI backends": "重载 CLI 后端",
    "Stop a session": "停止会话",
    "Rename a session": "重命名会话",
    "Archive a session": "归档会话",
    "Delete a session": "删除会话",
    "Switch the session model": "切换当前会话模型",
    "Switch the reasoning effort": "切换推理力度",
    "List direct command extensions": "列出直连命令扩展",
    "Show help": "显示帮助",
    "🟡 Running": "🟡 运行中",
    "🔴 Waiting": "🔴 等待",
    "⚫ Last run failed": "⚫ 上次失败",
    "🔴 Interrupted, awaiting recovery": "🔴 已中断待恢复",
    "🟢 Idle": "🟢 空闲",
    "No direct command extensions are installed. Put an extension directory in extensions/ or set COMMAND_DIR.": "当前未安装直连命令扩展。把扩展目录放进 extensions/ 或配置 COMMAND_DIR。",
    "Direct command extensions": "直连命令扩展",
    "These commands run on this machine without calling an AI or entering session context.": "这些命令直接在本机执行，不调用 AI，也不进入会话上下文。",
    "{value} (gateway default)": "{value}（网关默认）",
    "CLI default": "CLI 默认",
    "Telegram write stats (UTC {day})": "Telegram 写入统计（UTC {day}）",
    "Successful writes today: {new} new messages · {edits} edits · {other} other": "今日成功写入：新消息 {new} · 编辑 {edits} · 其他 {other}",
    "Requests today: {ok} succeeded · {failed} failed · {limited} rate-limited (429) · {deferred} deferred locally": "今日请求：成功 {ok} · 失败 {failed} · 429 {limited} · 本地延后 {deferred}",
    "Successful writes, last 7 days: {new} new messages · {edits} edits · {other} other · {limited} rate-limited (429)": "近 7 天成功写入：新消息 {new} · 编辑 {edits} · 其他 {other} · 429 {limited}",
    "Current flood wait: {seconds} s": "当前 flood wait：{seconds} 秒",
    "Methods today: {methods}": "今日方法：{methods}",
    "Counts only the gateway's own write calls; this is not Telegram's official daily quota.": "仅统计 gateway 的实际写调用，不代表 Telegram 官方每日额度。",
    "No active session. Use /new to pick a CLI and create one.": "当前没有活动会话。使用 /new 选择并创建一个。",
    "{cli} is not enabled for this bot; create a session with an enabled CLI.": "此 Bot 未启用 {cli}，请新建已启用的 CLI 会话。",
    "Session {session} has exited; create or switch to another session.": "会话 {session} 已经退出，请创建或切换到其他会话。",
    "{cli} is not enabled for this bot. Available: {clis}": "此 Bot 未启用 {cli}。可选：{clis}",
    "Could not create the session: {error}": "创建失败：{error}",
    """Created and switched to {session}
CLI: {cli}
Directory: {cwd}
Model: {model}
Reasoning effort: {effort}
Permissions: no approval prompts""": "已创建并切换到 {session}\nCLI：{cli}\n目录：{cwd}\n模型：{model}\n推理力度：{effort}\n权限：免审批",
    "Pick a CLI for the new session:\nDefault directory: {cwd}": "选择要创建的 CLI：\n默认目录：{cwd}",
    "Current session ({cli})": "当前会话（{cli}）",
    "All CLIs": "全部 CLI",
    "Current: {label} · {cli}": "当前：{label} · {cli}",
    "No active session.": "当前没有活动会话。",
    "Choose what to reload:": "选择重载范围：",
    "Running tasks are not interrupted. Codex is a shared backend, so reloading one Codex session resumes every Codex session. This does not reread .env or the gateway source.": "运行中的任务不会被强制中断。Codex 是共享后端，重载一个 Codex 会话会恢复全部 Codex 会话。此操作不会重读 .env 或 Gateway 源码。",
    "Invalid arguments: {error}": "参数格式错误：{error}",
    "Usage: /new <{clis}> [dir]": "用法：/new <{clis}> [目录]",
    "Usage: /use <session-id|{clis}>": "用法：/use <会话ID|{clis}>",
    "Switched to {session}\nDirectory: {cwd}": "已切换到 {session}\n目录：{cwd}",
    "Session not found: {target}": "找不到会话：{target}",
    "Back to {session}\nDirectory: {cwd}": "已返回 {session}\n目录：{cwd}",
    "No previous active session to return to.": "没有可返回的活动会话。",
    """Current: {session}
CLI: {cli}
Directory: {cwd}
Model: {model}
Reasoning effort: {effort}
Permissions: no approval prompts""": "当前：{session}\nCLI：{cli}\n目录：{cwd}\n模型：{model}\n推理力度：{effort}\n权限：免审批",
    "Could not interrupt: {error}": "中断失败：{error}",
    "Interrupted {session}.": "已中断 {session}。",
    "No task is running.": "当前没有正在运行的任务。",
    "No session to resume was found.": "找不到要恢复的会话。",
    "{session} is running; there is nothing to resume.": "{session} 正在运行，无需恢复。",
    "{session} has no task to resume.": "{session} 没有待恢复的任务。",
    "Resuming {session}'s task…": "正在恢复 {session} 的任务…",
    "Session not found.": "找不到会话。",
    "{session} is running; use /interrupt to stop it.": "{session} 正在运行，请用 /interrupt 中断。",
    "Gave up {session}'s interrupted task.": "已放弃 {session} 的待恢复任务。",
    "Compaction failed: {error}": "压缩失败：{error}",
    "Could not clear: {error}": "清空失败：{error}",
    "Usage: /reload [current|all]": "用法：/reload [current|all]",
    "Reload failed: {error}": "重载失败：{error}",
    "{session} model set to {model} (applies from the next task).": "{session} 模型已设为 {model}（下次任务生效）。",
    "{session} reasoning effort set to {effort} (applies from the next task).": "{session} 推理力度已设为 {effort}（下次任务生效）。",
    "No session to stop was found.": "找不到要停止的会话。",
    "Could not stop the session; its record was kept. Try again later.": "停止失败，会话记录已保留，请稍后重试。",
    "Stopped {session}.": "已停止 {session}。",
    "Usage: /rename [session-id] <new name>": "用法：/rename [会话ID] <新名称>",
    "Rename failed: {error}": "重命名失败：{error}",
    "Renamed {session} to “{name}”.": "已将 {session} 重命名为「{name}」。",
    "No session to archive was found.": "找不到要归档的会话。",
    "No session to delete was found.": "找不到要删除的会话。",
    "Usage: /send <text for the current CLI>": "用法：/send <要发送给当前 CLI 的文本>",
    "Unknown command. Use /help to see the available commands.": "未知命令。使用 /help 查看可用命令。",
    "No direct command extensions are installed.\nPut an extension directory in extensions/ or set COMMAND_DIR.": "当前未安装直连命令扩展。\n把扩展目录放到 extensions/，或配置 COMMAND_DIR。",
    "Direct command extensions (no AI)": "直连命令扩展（不调用 AI）",
    "  Usage: {usage}": "  用法：{usage}",
    "  About: {detail}": "  说明：{detail}",
    "  Directory: {cwd}": "  目录：{cwd}",
    "  Manifest: {path}": "  定义：{path}",
    "Tasks:": "任务状态：",
    "Interrupt {session}": "中断 {session}",
    "Interrupt and clear the queue": "中断并清空",
    "Clear {count} queued for {session}": "清空 {session} 的 {count} 条等待",
    "No tasks are running or queued.": "当前没有运行中或排队的任务。",
    "Could not archive the session; its record was kept. Try again later.": "归档失败，会话记录已保留，请稍后重试。",
    "Archived {session}. Use /sessions all to view or restore it.": "已归档 {session}。用 /sessions all 查看或恢复。",
    "Delete": "确认删除",
    "Cancel": "取消",
    "Permanently delete session {session} ({id})?": "确认永久删除会话 {session}（{id}）？",
    " · {count} queued": " · 排队 {count}",
    "{session} ({id}) · {cli}": "{session}（{id}） · {cli}",
    "Status: {status}": "状态：{status}",
    "Directory: {cwd}": "目录：{cwd}",
    "Model: {model}": "模型：{model}",
    "Reasoning effort: {effort}": "推理力度：{effort}",
    "Current task: running for {seconds} s": "当前任务：已运行 {seconds}秒",
    "Current task model: {model} · reasoning effort: {effort}": "当前任务模型：{model} · 推理力度：{effort}",
    "The native Claude context query is unavailable right now; showing saved data.": "Claude 原生窗口查询暂时不可用，当前显示已保存数据。",
    "Account quota: {cli} offers no quota query through this integration.": "账户额度：{cli} 当前接入方式未提供额度查询。",
    "• {session} ({id}): {status}": "• {session} ({id})：{status}",
    "[{session}] · How can I help?": "[{session}] · 有什么可以帮你？",
    "Sessions (tap a button to switch):": "会话列表（点击按钮切换）：",
    "Archived": "已归档",
    " · exited": " · 已退出",
    "Restore {session}": "恢复 {session}",
    "Delete…": "删除",
    "Interrupt": "中断",
    "Archive": "归档",
    "There are no sessions yet.": "目前没有会话。",
    "{session} · current model: {model}": "{session} · 当前模型：{model}",
    "Tap to switch (applies from the next task):": "点击切换（下次任务生效）：",
    "Restore gateway default": "恢复网关默认",
    "Restore default": "恢复默认",
    "This CLI cannot list its models; use /model <name> directly.": "该 CLI 无法列出模型，请直接 /model <名称>。",
    "{session} · current reasoning effort: {effort}": "{session} · 当前推理力度：{effort}",
    "This CLI does not support setting the reasoning effort.": "该 CLI 不支持手动设置推理力度。",
    "Not allowed": "没有权限",
    "Unknown action": "未知操作",
    "That CLI is no longer available; use /new again": "CLI 已失效，请重新 /new",
    "Creating {cli}": "正在创建 {cli}",
    "Picked {cli}; creating…": "已选择 {cli}，正在创建…",
    "Reloading": "正在重载",
    "This file is no longer available": "文件已失效",
    "Could not send the file: {error}": "发送文件失败：{error}",
    "Already sending; please wait": "正在发送，请稍候",
    "Sending": "正在发送",
    "This path is no longer available": "路径已失效",
    "Path sent": "已发送路径",
    """The artifact exceeds Telegram's send limit, so here is its local path:
`{path}`
Size {size:.1f} MiB / limit {limit:.0f} MiB""": "产物超过 Telegram 发送上限，这里只给本机路径：\n`{path}`\n大小 {size:.1f} MiB / 上限 {limit:.0f} MiB",
    "Session expired": "会话已失效",
    "Restored the gateway default model {model}": "已恢复网关默认模型 {model}",
    "Restored the default model": "已恢复默认模型",
    "Restored the gateway default reasoning effort {effort}": "已恢复网关默认推理力度 {effort}",
    "Restored the default reasoning effort": "已恢复默认推理力度",
    "Model set to {model}": "模型已设为 {model}",
    "That choice expired; use /model again": "选择已失效，请重新 /model",
    "Reasoning effort set to {effort}": "推理力度已设为 {effort}",
    "That choice expired; use /effort again": "选择已失效，请重新 /effort",
    "Interrupted the current task": "已中断当前任务",
    "No task is running": "当前没有运行任务",
    "Cleared {count} queued messages": "已清空 {count} 条等待消息",
    "The queue is already empty": "队列已经为空",
    "Interrupted and cleared {count} queued messages": "已中断并清空 {count} 条等待消息",
    "Switched to {session}": "已切换到 {session}",
    "Interrupted": "已中断",
    "Restored": "已恢复",
    "Please confirm": "请再次确认",
    "Deleted": "已删除",
    "Deleted {session} ({id}).": "已删除 {session}（{id}）。",
    "Cancelled": "已取消",
    "Deletion cancelled.": "已取消删除。",
    "The session expired; use /reload again": "会话已失效，请重新 /reload",
    "Codex context compaction": "Codex 上下文压缩",
    " etc.": " 等",
    "Tasks are still running: {tasks}; wait for them to finish and retry": "仍有任务运行：{tasks}；请等待完成后重试",
    "Codex backend restarted; resumed {count} sessions.": "Codex 后端已重启，已恢复 {count} 个会话。",
    "Could not resume: {sessions}": "恢复失败：{sessions}",
    "{clis}: a new process starts for each task, so the next task uses the installed version.": "{clis} 按任务启动新进程；下次任务会直接使用当前安装版本。",
    "There is no CLI to reload.": "没有可重载的 CLI。",
    "{session} is running; /interrupt it before compacting.": "{session} 正在运行，请先 /interrupt 再压缩。",
    "Started context compaction for {session}.": "已触发 {session} 的上下文压缩。",
    "Compacting {session}'s context…": "正在压缩 {session} 的上下文…",
    "{session} uses Claude's automatic compaction (--autocompact); manual /compact exists only in interactive mode, not headless.": "{session} 使用 Claude 的 --autocompact 自动压缩；手动 /compact 仅交互模式可用，headless 下没有。",
    "{session} (Grok) has no manual compaction; Grok compacts automatically near 85% context. Use /clear to start over.": "{session}（Grok）无手动压缩；上下文接近 85% 时 Grok 会自动压缩。需要重置可 /clear 开新对话。",
    "Compaction interrupted.": "压缩已中断。",
    "{session}: {result}": "{session}：{result}",
    "{session} is running; /interrupt it before /clear.": "{session} 正在运行，请先 /interrupt 再 /clear。",
    "Could not create a new Codex thread; nothing was cleared.": "新建 Codex 线程失败，未清空。",
    "{session} started a new conversation (the old thread was archived).": "{session} 已开新对话（旧线程已归档）。",
    "{session} started a new conversation; the context was cleared.": "{session} 已开新对话，上下文已清空。",
    "{session} does not support /clear.": "{session} 不支持 /clear。",
    "{cli} is not enabled for this bot, so this session cannot continue.": "此 Bot 未启用 {cli}，不能继续该会话。",
    "[{session}] The CLI accepted the added request.": "[{session}] 已追加要求，CLI 已接收。",
    "The added request was not confirmed, so it was not resent to avoid running it twice. Check the current task output.": "追加请求的结果尚未确认，为避免重复执行，未自动重发。请查看当前任务输出。",
    "{session}'s queue is full (at most {limit}).": "{session} 的等待队列已满（最多 {limit} 条）。",
    "{session} is running; queued as #{position}.": "{session} 正在运行；已加入队列（第 {position} 条）。",
    "{cli} is not enabled for this bot, so the task cannot start.": "此 Bot 未启用 {cli}，不能启动任务。",
    "Unsupported session backend: {backend}": "不支持的会话后端：{backend}",
    "Could not send: {error}": "发送失败：{error}",
    "The task start was not confirmed, so the queue is paused. Check the native session first so nothing runs twice.": "启动结果未确认，等待队列已暂停。请先核对原生会话，避免重复执行。",
    "Task interrupted by a gateway restart: {session} resumed automatically and is continuing the unfinished work. If it already had side effects, stop it with /interrupt.": "任务中断（网关重启）：{session} 已自动恢复，正在继续上次未完成的工作。如已产生副作用，请 /interrupt 停止。",
    "⚠️ The last task was interrupted: {session} ({id}). Reply /resume to continue or /cancel to give up.": "⚠️ 上次任务被中断：{session}（{id}）。回复 /resume 继续，或 /cancel 放弃。",
    "Codex is retrying": "Codex 正在重试",
    "Codex reported an error; waiting for the task to end": "Codex 报错，等待任务结束",
    "{label}: {detail}": "{label}：{detail}",
    "Running": "运行中",
    "Done": "完成",
    "Failed": "失败",
    "Running in background": "后台运行中",
    "Run log · part {page}": "运行记录 · 第 {page} 段",
    "[{session}] · {label} {seconds}s": "[{session}] · {label} {seconds}秒",
    " · {count} added requests": " · 已追加 {count} 次",
    " · part {page}": " · 第 {page} 段",
    "Current command:": "当前命令：",
    "Command output:": "命令输出：",
    "The task finished; its progress is in the run log. The CLI gave no separate final answer.": "任务已结束，过程输出见运行记录；CLI 没有提供单独的最终回答。",
    "⚠️ The model finished without a visible answer. The gateway did not retry, to avoid repeating actions.": "⚠️ 模型已结束，但没有返回可见回答。为避免重复执行操作，网关未自动重试。",
    "⚠️ The task was interrupted. Reply /resume to continue or /cancel to give up.": "⚠️ 任务被中断。回复 /resume 继续，或 /cancel 放弃。",
    "Notice:": "提示：",
    "Error:": "错误：",
    "✅ This task finished; the result is in the newest progress message.": "✅ 此任务已完成，结果已更新到最新一条进度消息。",
    "⚠️ This task failed; see the newest progress message for details.": "⚠️ 此任务失败，详情见最新一条进度消息。",
    "⏹ This task was interrupted.": "⏹ 此任务已中断。",
    "[{session}] The CLI accepted added request #{number}.\nFurther progress appears below this message.": "[{session}] 已追加第 {number} 次要求，CLI 已接收。\n后续进展会显示在这条消息下方。",
    "The file exceeds the configured send size limit": "文件超过配置的发送大小限制",
    "Not a regular file": "不是普通文件",
    "The file is not inside ALLOWED_WORKDIRS": "文件不在 ALLOWED_WORKDIRS 中",
    "Send path: {name}": "发送路径：{name}",
    "Send image: {name}": "发送图片：{name}",
    "Send video: {name}": "发送视频：{name}",
    "Send file: {name}": "发送文件：{name}",
    "{label} {seconds}s": "{label} {seconds}秒",
    "Arguments: `{args}`": "参数：`{args}`",
    "Progress: {progress}": "进度：{progress}",
    "Output:": "输出：",
    "Started; waiting for output…": "已启动，等待输出…",
    "(Only the last {limit} bytes of output are kept)": "（输出仅保留最后 {limit} 字节）",
    "The command printed nothing.": "命令没有输出。",
    "Could not receive the file: {error}": "接收文件失败：{error}",
    "Could not recognize this attachment.": "无法识别这个附件。",
    "Text and files of every kind (images, audio, video, voice and more) are supported; stickers and similar messages are not forwarded to the CLI.": "目前支持文本和各类文件（图片、音频、视频、语音等）；贴纸这类消息不会转给 CLI。",
    # commands.py
    "There is no extension command named /{command}": "没有名为 /{command} 的扩展命令",
    "/{command} is already running; wait for it or /interrupt it first": "/{command} 正在运行，请等它结束或先 /interrupt",
    "The gateway is shutting down": "网关正在退出",
    "Could not start the command: {error}": "无法启动命令: {error}",
    "Could not start /{command}: {error}": "无法启动 /{command}: {error}",
    "Command exit code {code}": "命令退出码 {code}",
    "Stopped after running longer than {seconds} seconds": "执行超过 {seconds} 秒，已终止",
    # headless_backend.py
    "Could not start the native Claude status query": "无法启动 Claude 原生状态查询",
    "The native Claude status query timed out": "Claude 原生状态查询超时",
    "The native Claude status query returned no valid data": "Claude 原生状态接口未返回有效数据",
    "The native Claude status query exited": "Claude 原生状态查询已退出",
    "The native Claude status response exceeded the size limit": "Claude 原生状态响应超过限制",
    "Account quota: temporarily unavailable; try again later.": "账户额度：暂时无法查询，请稍后重试。",
    "Could not connect to the native Claude status query": "Claude 原生状态查询连接失败",
    "Compaction did not run: {reason}": "压缩未执行：{reason}",
    "Compaction aborted.": "压缩已中止。",
    "Context compaction started.": "已触发上下文压缩。",
    "Compaction finished. Summary: {summary}": "压缩完成。摘要：{summary}",
    "Pi context compaction timed out; the compaction process was stopped": "Pi 上下文压缩超时，已停止压缩进程",
    "{cli} RPC returned no compact result (exit {code})": "{cli} RPC 未返回 compact 结果（exit {code}）",
    # telegram.py
    "File exceeds the size limit (max {limit} MB)": "文件超过大小限制（最大 {limit} MB）",
    "Telegram file download failed: {reason}": "Telegram 文件下载失败：{reason}",
    "Could not read the file to send: {error}": "无法读取发送文件：{error}",
    # usage.py
    "latest request": "最近请求",
    "latest response": "最近响应",
    "latest request input": "最近请求输入",
    "latest successful request input · history": "最近成功请求输入 · 历史记录",
    "latest report": "最近上报",
    "Context: no valid usage yet; waiting for the CLI's next report.": "上下文：暂无有效用量，等待 CLI 下一次上报。",
    "Context window: {window:,} tokens": "上下文窗口：{window:,} tokens",
    "Context ({basis})": "上下文（{basis}）",
    "{label}: {tokens:,} / {window:,} tokens ({percent:.1f}%)": "{label}：{tokens:,} / {window:,} tokens（{percent:.1f}%）",
    "Context left: {tokens:,} tokens": "剩余上下文：{tokens:,} tokens",
    "{label}: {tokens:,} tokens": "{label}：{tokens:,} tokens",
    "Context window: not reported by the CLI, so the share is unknown.": "上下文窗口：CLI 未上报，无法计算占比。",
    "Usage model: {model}": "用量模型：{model}",
    "Usage updated: {time}": "用量更新：{time}",
    "input": "输入",
    "cache read": "缓存读取",
    "cache write": "缓存写入",
    "output": "输出",
    "CLI-reported usage: {parts}": "CLI 上报用量：{parts}",
    "Session total: {total:,} tokens": "会话累计用量：{total:,} tokens",
    "Account quota: Claude returned no quota data.": "账户额度：Claude 未返回额度数据。",
    "Account quota: this Claude sign-in has no plan quota.": "账户额度：当前 Claude 登录方式未提供套餐额度。",
    "OAuth apps": "OAuth 应用",
    "5-hour quota": "5小时额度",
    "7-day quota": "7天额度",
    "Usage quota": "使用额度",
    "{name}: {left:g}% left ({used:g}% used)": "{name}：剩余 {left:g}%（已用 {used:g}%）",
    " · resets {time}": " · 重置 {time}",
    "Extra usage: off": "额外用量：未启用",
    "Extra usage: on, {percent:g}% used": "额外用量：已启用，已用 {percent:g}%",
    "Extra usage: on, usage not reported": "额外用量：已启用，用量未上报",
    "Account quota: Claude returned no usable quota data.": "账户额度：Claude 未返回可用额度数据。",
    "Account quota · Claude": "账户额度 · Claude",
    " ({plan})": "（{plan}）",
    "{days}-day quota": "{days}天额度",
    "{hours}-hour quota": "{hours}小时额度",
    "{minutes}-minute quota": "{minutes}分钟额度",
    "Account quota: the CLI returned no quota data.": "账户额度：CLI 未返回额度数据。",
    "Primary quota window": "主额度窗口",
    "Secondary quota window": "次额度窗口",
    "Credits: unlimited": "附加额度：不限量",
    "Credit balance: {balance} (reported by the CLI)": "附加额度余额：{balance}（CLI 上报）",
    "Credits: available, balance not reported": "附加额度：可用，余额未上报",
    "Credits: none available": "附加额度：无可用额度",
    "Quota status: usage limit reached": "额度状态：已达到使用限制",
    "Quota status: spend limit reached": "额度状态：已达到支出限制",
    "Account quota · {label}": "账户额度 · {label}",
    "Account: plan quota can't be used right now": "账户：当前不允许使用套餐额度",
    "Account quota: the CLI returned no usable quota data.": "账户额度：CLI 未返回可用的额度数据。",
}
