# Telegram CLI Gateway

Drive the Claude Code, Codex, Grok, and Pi CLIs installed on your machine from a single Telegram bot, and switch between multiple persistent sessions.

The product boundary and engineering principles are documented in [`AGENTS.md`](AGENTS.md): this is a thin Telegram control layer over native CLIs, not a general agent platform.

## Features

- Telegram user-ID allowlist; direct messages only by default
- `/new`, `/use`, `/back`, `/sessions`, `/tasks`, `/where`, `/interrupt`, `/resume`, `/cancel`
- `/reload` reloads the current session or every CLI through a picker; Codex restarts its shared app-server and restores all native threads, while Claude/Grok/Pi naturally use the currently installed version on their next turn
- `/tasks` provides interrupt-current, clear-queue, and interrupt-and-clear buttons; each session queues at most 20 inputs
- `/sessions` returns Telegram buttons to switch, interrupt, archive, restore, and confirm deletion; each session shows a status badge (🟡 running / 🟢 idle / ⚫ last turn failed / 🔴 interrupted, resumable / ⏳ N queued)
- `/rename` sets a human-readable session name; `/sessions all` lists archived sessions
- `/model` inspects and switches the current session's model, `/effort` the reasoning effort (takes effect on the next turn; the choices come from each CLI's own model catalog, reused for 10 minutes and refreshed by `/reload`, and the selection is passed straight through, so the gateway never owns model routing)
- Replying to any earlier CLI result automatically switches back to that session before delivering the message
- Codex uses `app-server`; Claude, Grok, and Pi use their own headless JSON streams
- When Codex or Claude receives a new message mid-turn, it joins the current task natively: Codex through `turn/steer`, Claude through its stream-json input, which Claude reads at its next tool call or answers right after the current reply. After the CLI confirms it, a short acknowledgement appears at the bottom and subsequent progress follows it. Telegram publication runs off the input dispatcher; Grok and Pi queue inputs per-session FIFO and continue automatically once the turn finishes
- When a task is interrupted by a gateway restart or shutdown, the gateway asks on restart whether to "reply /resume to continue, or /cancel to give up"; `/resume` continues the same session through the CLI's native resume (pair with `AUTO_RESUME=true` for cases such as restarting the gateway after editing its own code); tasks you stopped with `/interrupt` are not recovery candidates
- Progress stays readable during long Codex tasks: approximately 1,500 UTF-16 units per segment, preferring paragraph boundaries and preserving fenced code. Only the newest segment is edited; older segments remain as reading records. Progress messages are silent (no notification sound), using native Rich Messages with an HTML fallback; no Drafts are used
- Codex's native `commentary` and `final_answer` phases separate progress from the final answer: the final answer has its own message and contains no accumulated progress. Claude's messages that end in a tool call are narration, so its progress also rolls into reading records during the run; the message still streaming stays in the live card until Claude says how it ends, and each `result` names the message that answers it (one more per added request Claude answers after its reply), which gets its own message (a turn without narration stays one message). Grok, Pi, and Codex models without phase metadata stream the answer itself, so they keep one live card that becomes the final answer and is never split into reading records
- With several concurrent sessions, only the "current" session is live-streamed: switching to a session posts a progress card at the bottom that refreshes in place every 10 seconds; the session you switched away from freezes as "running in background" and, once finished, only its own card is updated (long answers and files are never injected at the bottom of the session you are currently reading); switching back to an idle session later copies its last successful result to the bottom of the chat, while after `/clear` you get a short new-conversation notice; duplicate cards from session switches close with a one-line status when their task ends, while archived reading segments are preserved
- Message edits retry on `retry_after` or capped exponential backoff when Telegram rate-limits or fails transiently
- Codex error notifications show a diagnostic or native retry notice while the task remains active. `turn/completed` records the final outcome and releases the session for new or queued input; a disconnect preserves the interrupted task for explicit recovery.
- Updates are handled one at a time across all Bot entrances, so slow self-contained actions run off that path: send-file uploads, `/status` and `/context` probes, the `/model` picker, and Pi `/compact` (input sent during a Pi compaction is queued and starts when it finishes). `/interrupt` and the other bots stay responsive meanwhile, and a repeated tap on a send button during its upload is ignored
- `/tgstats` shows today's and the last 7 days' Telegram new messages, edits, failures, 429s, and local deferrals; aggregated in UTC, retained for 14 days, written to disk at most every 30 seconds and on shutdown, and it records neither message content nor chat_id
- `/status` shows the current CLI, model, effort, running/queued state, context usage, and Codex/Claude account quotas (remaining percentage and reset time, plus Codex additional credits when reported). Codex quota reads time out after 5 seconds; Claude native diagnostics share an 8-second deadline. Failures preserve local session status and saved context usage
- `/context` shows the latest native context-usage snapshot and remaining space when the CLI reports a window size. Codex reports the latest request separately from cumulative session tokens; Claude/Grok use the latest main-agent input, including cache tokens, and Pi uses the latest successful response. Window sizes and balances are never guessed. These commands create no model turn; snapshots survive gateway restarts (live reports are saved with the next state change or on shutdown, so a crash can lose only the latest ones) and are cleared on `/clear`, with context occupancy invalidated after compaction until the next report. Existing Claude sessions can backfill missing context on `/status` or `/context` from their native JSONL history (`CLAUDE_CONFIG_DIR` or `~/.claude`), with the original report time shown; failed/zero-token replies, subagents, discarded branches, and pre-compaction usage are excluded. Reads are limited to the last 8 MiB of the selected session file, and only numeric usage metadata is saved. Other older sessions acquire a snapshot on their next native report. Grok/Pi account quotas are not exposed by the current integrations
- Claude window sizes and plan quotas come from its SDK controls, not a model-name table or terminal scraping. Each query starts a bounded ephemeral CLI process with session persistence, hooks, and MCP startup disabled. `get_context_usage` uses `detail: summary` to avoid token-count API requests; `/status` also reads `get_usage` with `skip_behaviors: true` to avoid scanning unrelated transcripts. The probe sends only control requests, neither resumes nor writes a native conversation, and never substitutes its empty conversation's usage for the selected session's snapshot. Unsupported controls, timeouts, and API-key/provider sessions without plan quotas are reported explicitly
- Native Rich Markdown rendering for tables, headings, lists, task lists, links, quotes, formulas, and code blocks
- Automatic fallback to safe HTML/plain text when Rich Messages are unavailable
- Receives any Telegram file and hands it to the current CLI: documents, images, audio (WAV, MP3, …), video, GIFs, voice and video notes, singly or as an album (stickers are not forwarded). Pi gets JPEG/PNG/GIF/WebP images as image attachments and every other file as a path, like the other CLIs
- When an answer mentions a real file inside the allowed directories, a one-tap "send file/image/video" button is attached; MP4 files are sent as previewable video messages, with the size, duration and thumbnail that `ffprobe`/`ffmpeg` read from the file when they are installed (without them Telegram shows a large video as a square with no preview until it is downloaded). When Codex or Claude marks a final answer, only that answer is scanned, not the progress narration
- Artifacts over 45 MiB (`TELEGRAM_MAX_FILE_BYTES`) are neither auto-sent nor offered a "send file" button; the card only shows a "path" button that returns the local absolute path (the Bot API upload limit is 50 MB, and raising it requires a self-hosted Local Bot API Server)
- Artifacts are not auto-sent when an answer completes; the card keeps its "send file/image/video" buttons: `AUTO_SEND_ARTIFACTS=off` (default) / `images` (auto-send only images ≤10 MB) / `all` (also send existing files mentioned in the answer, which easily leaks source code along with the artifacts). A background completion in a non-current session never auto-sends files
- Session IDs, local state, and CLI context are persisted
- Single-instance file lock
- Direct command extensions: `extensions/*/manifest.json` declares a command and the gateway spawns a local process directly — no AI call, no session context, no tokens burned. `/commands` lists installed commands and `/help` appends their usage; commands support `@@PROGRESS` lines that refresh in place, and their artifacts automatically get "send file/image/video" buttons. See the contract in [`extensions/README.md`](extensions/README.md)
- Optional Relay Pages companion: agents publish long replies as private, password-protected reading pages and answer in Telegram with a summary and the link. It runs as its own service and the gateway does not depend on it; see [`extensions/relay-pages/README.md`](extensions/relay-pages/README.md)

Voice notes reach the CLI as plain OGG files; voice transcription and text-to-speech are not wired up yet.

## Permission Mode

All four CLIs run without approval prompts:

- Codex: `approvalPolicy=never`, `sandbox=danger-full-access`
- Claude Code: `--dangerously-skip-permissions`
- Grok: `--always-approve --permission-mode bypassPermissions`
- Pi: `--approve`

This means a prompt from an allowlisted account can make the CLI read and write local files and execute commands. Treat the Telegram bot token and the allowlisted accounts as high-privilege credentials, and do not open the bot to unknown users or uncontrolled group chats.

`ALLOWED_WORKDIRS` limits session starting directories and the local artifact paths the gateway may send, but it is not a filesystem sandbox for the CLIs; an approval-free CLI can still reach anything its operating-system account can access.

## Configuration

The project only needs a Telegram token, an allowlist, and CLI paths. Create `.env` and make sure only the current user can read it:

```bash
cp .env.example .env
chmod 600 .env
./scripts/run.sh --check
```

To run multiple Telegram bot entrances, just add any non-empty `TELEGRAM_BOT_TOKEN_<name>`; no extra switch is needed:

```dotenv
TELEGRAM_BOT_TOKEN=primary_bot_token
TELEGRAM_BOT_TOKEN_2=second_bot_token
TELEGRAM_BOT_TOKEN_RESEARCH=research_bot_token
```

All bots share the same set of CLI sessions. Each bot conversation keeps its own current session and back-history, so switching sessions in one bot does not affect another. A task's result always returns to the bot that started it; other bots can use `/use <session-id>` to read that session's latest run snapshot or the most recent result cached during the current gateway run. Appending input from another bot while a task runs does not change the original task's reply entrance. The `<name>` is used as a persistent state key, so do not rename it once configured.

Common settings:

```dotenv
TELEGRAM_ALLOW_GROUPS=false
DEFAULT_WORKDIR=/srv/projects
ALLOWED_WORKDIRS=/srv/projects
STREAM_UPDATE_INTERVAL=10
TELEGRAM_MAX_FILE_BYTES=47185920
AUTO_RESUME=false
# Optional: an extra direct command extension directory, must be inside ALLOWED_WORKDIRS
# COMMAND_DIR=/srv/projects/my-commands
ENABLED_CLIS=claude,codex,grok,pi
DEFAULT_CODEX_MODEL=gpt-6-astra
DEFAULT_CODEX_EFFORT=high
GATEWAY_LANGUAGE=en
```

`GATEWAY_LANGUAGE` sets the language of the gateway's own Telegram text: replies, buttons, the `/` menu and `/help`. It is `en` (English) by default; `zh` switches to Chinese. Extensions receive it as `TG_LANGUAGE`. It does not change what the CLIs answer, and logs and the prompts the gateway sends to CLIs stay English. The Chinese text lives in `src/cli_telegram_gateway/i18n.py`; every string passed to `tr()` needs an entry there, which `tests/test_i18n.py` checks.

With `AUTO_RESUME=true`, the gateway automatically continues tasks that were interrupted by an unexpected restart (excluding tasks you stopped with `/interrupt`); the default is `false`, in which case you reply `/resume` to recover manually or `/cancel` to give up.

`ENABLED_CLIS` can restrict one gateway instance to specific CLIs, for example `ENABLED_CLIS=codex`. Disabled CLIs can neither create nor switch sessions, and when Codex is disabled its app-server is not started at all.

`DEFAULT_<CLI>_MODEL` and `DEFAULT_<CLI>_EFFORT` set defaults for that CLI's new sessions; existing sessions also inherit them as long as they have not explicitly overridden them with `/model` or `/effort`. `/model default` and `/effort default` restore inheritance. The Codex settings above use the official model identifier `gpt-6-astra` and `high` reasoning effort, and do not modify the global configuration of other Codex CLIs on the machine.

Attachments are stored in `.runtime/uploads/` with directory permissions `0700` and file permissions `0600`. The default maximum is 20 MiB.

The send limit `TELEGRAM_MAX_FILE_BYTES` defaults to 45 MiB with the cloud Bot API. **An artifact over the configured limit is never auto-sent and never offered a "send file" button**; the card shows a "path" button. For larger files, deploy a co-located Bot API server with `--local` and set `TELEGRAM_LOCAL_API_URL=http://127.0.0.1:8081`. Local mode defaults to 1 GiB and sends file URIs without buffering the artifact in gateway memory. It also supports local `getFile` downloads. See [local deployment and migration](docs/local-bot-api.md); Telegram application credentials and cloud `logOut` are required before switching.

## Direct Command Extensions

Put a directory under the repository's `extensions/` (or point `COMMAND_DIR` somewhere else) and the gateway scans `extensions/*/manifest.json` at startup, registering the corresponding command on Telegram:

```
extensions/bilibili/
├─ manifest.json      # declares command name, exec, cwd, timeout
└─ main.py            # the plugin itself; depends only on argv + TG_* env vars + stdout
```

When the command is invoked, the gateway **runs a local process directly**, starting no AI CLI, creating no session, and consuming no tokens:

```text
/bili https://www.bilibili.com/video/BV1xx411c7mD 1080p
```

- Progress: the plugin writes `@@PROGRESS <text>` to stdout and the gateway refreshes the same message in place; all other output is sent once when the run finishes
- Artifacts: real files on stdout that live inside `ALLOWED_WORKDIRS` get "send file/image/video" buttons, following the existing `TELEGRAM_MAX_FILE_BYTES` and `AUTO_SEND_ARTIFACTS` rules
- Interruption: `/interrupt` terminates the command currently running in this chat when the session is idle (killing the whole process group)
- Troubleshooting: `/commands` shows the loaded extensions, their usage, working directory, and manifest path; a single invalid extension only skips itself

Extensions bundled with the repository:

| Command | Description |
|---|---|
| `/example` | Sample extension demonstrating argv / `@@PROGRESS` / artifact paths |
| `/bili` | Bilibili download: `/bili <URL\|BV-id> [quality]`, supporting cookie and QR login, multi-part videos, and audio-only extraction. Usage and the pitfalls already hit are documented in [`extensions/bilibili/README.md`](extensions/bilibili/README.md) |

Full field reference, plugin contract, and security boundary: [`extensions/README.md`](extensions/README.md).

## Usage

```text
/new
/new codex /srv/projects/my-project
/new claude
/use grok
/sessions
/tasks
/tgstats
/status
/context
/rename codex-abcd main-project
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
/commands
/example hello
```

Plain text goes straight to the current session. When you send a file or image, its caption is used as the prompt; without a caption the gateway uses "please look at and handle this attachment".

`/reload` only restarts the running backend of the configured CLIs; it does not re-read `.env` or the Gateway source. After changing the configuration or the Gateway itself you still need to restart the systemd service. If any target session is still running, the reload is refused rather than interrupting or replaying the task.

The project manages its local `.venv` with `uv`; `scripts/run.sh` prefers to launch through `~/.local/bin/uv` and falls back to the system Python when `uv` is missing.

Run status in a direct message starts as a Rich Message that is edited in place every 10 seconds for the duration of the task (answer, current command, elapsed seconds); when the task completes, the same message is edited into the final answer with artifact buttons attached, and if the edit fails the gateway falls back to a safe HTML message. When Codex or Claude produced progress narration before a marked final answer, that narration stays as reading records and the final answer is sent as its own message. No temporary Drafts are used, because an interrupted Draft cannot be finalized by the gateway and leaves an unremovable "loading" bubble.

## systemd User Service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/telegram-cli-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telegram-cli-gateway
journalctl --user -u telegram-cli-gateway -f
```

## Tests

```bash
uv run python -W error::ResourceWarning -m unittest discover -s tests -v
```

## Three-Bot Isolation Experiment

See [`docker/experiments/README.md`](docker/experiments/README.md) for the three-container isolated deployment of Codex, Claude Code, and Grok. This is a way to deploy independent gateway instances, not to add multi-agent orchestration inside the gateway.
