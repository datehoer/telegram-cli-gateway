# Direct Command Extensions

An extension is a directory plus a `manifest.json` plus one executable entry point. When its command is invoked, the gateway **spawns a local process directly** — it calls no AI CLI, occupies no session context, and consumes no tokens.

```
extensions/
├─ README.md
└─ example/                 # the command name comes from manifest.command; the directory
   ├─ manifest.json         # name is arbitrary, but matching it is recommended
   └─ main.py
```

## Discovery Rules

| Source | Notes |
|---|---|
| `<repo>/extensions/*/manifest.json` | Bundled or locally installed, always scanned |
| The directory `COMMAND_DIR` points at | Optional, must be inside `ALLOWED_WORKDIRS`, for extensions kept elsewhere |

- Editing a manifest or adding an extension requires a gateway restart; **there is no hot reload**.
- A failing extension only skips itself and logs a warning; it does not affect gateway startup or other extensions.
- When `command` collides with a built-in command or with an already loaded extension, the later one is skipped with a warning.

`extensions/relay-pages/` is not a command extension: it has no manifest, so discovery
skips it. It is an optional companion service that turns long CLI replies into private
reading pages; see its [README](relay-pages/README.md).

Local installations can stay under `extensions/` without being tracked by Git.
`extensions/article/` is one such installation and is ignored by this repository.
It uses the existing manifest contract and requires no gateway core changes.

## manifest.json

```json
{
  "schema_version": 1,
  "id": "bilibili",
  "name": "Bilibili Download",
  "command": "bili",
  "usage": "/bili <URL> [1080p|720p]",
  "description": "Download a Bilibili video and send it back to Telegram",
  "exec": ["python3", "main.py"],
  "cwd": "/srv/projects/downloads",
  "timeout_seconds": 1800,
  "max_output_bytes": 16384,
  "enabled": true
}
```

| Field | Required | Notes |
|---|---|---|
| `schema_version` | ✅ | Must be `1` |
| `id` | ✅ | `^[a-z][a-z0-9_]{0,31}$`, used only as an identifier |
| `command` | ✅ | The command name triggered on Telegram; same character set |
| `exec` | ✅ | argv array. **Not passed through a shell**, so do not add your own quoting |
| `name` | ❌ | Display name, defaults to `id` |
| `usage` | ❌ | Usage string shown in `/help` and `/commands` |
| `description` | ❌ | One-line description, also used for Telegram's `/` menu |
| `cwd` | ❌ | Working directory, defaults to `DEFAULT_WORKDIR`; must be inside `ALLOWED_WORKDIRS`; relative paths resolve against the extension directory |
| `timeout_seconds` | ❌ | 1–21600, default 1800; on timeout the process group is `SIGTERM`ed and `SIGKILL`ed 5 seconds later |
| `max_output_bytes` | ❌ | Trailing bytes of output kept for display, default 16384. Raise it for long-running tasks such as downloads |
| `enabled` | ❌ | `false` skips the extension (without a warning) |

## Plugin Contract

What the gateway passes to the plugin:

| Argument | Content |
|---|---|
| `argv[1:]` | The gateway **does not do shell tokenization**. `argv[0]` (the subcommand) is passed via `TG_ARGV0`, and every other newline-separated non-empty line is one argv element |
| `TG_RAW_ARGS` | The **raw text** the user typed, untouched |
| `TG_COMMAND_ID` / `TG_COMMAND` / `TG_COMMAND_NAME` | Extension id, command name, display name |
| `TG_ARGS_JSON` | The newline-split arguments as a JSON array |
| `TG_CHAT_ID` / `TG_USER_ID` | Requester context |
| `TG_WORKDIR` | The resolved `cwd` |
| `TG_MANIFEST_DIR` | Absolute path of the extension directory |
| `cwd` | Same as `TG_WORKDIR` |

What the plugin returns to the gateway:

| Channel | Semantics |
|---|---|
| stdout lines starting with `@@PROGRESS <text>` | Progress. Refreshes the progress card in place and **never enters the final output** |
| Other stdout | Final output, displayed truncated to `max_output_bytes` from the tail |
| stderr | Appended to the display on failure; logged on success |
| Exit code | `0` success, non-zero failure |
| Absolute paths on stdout | If they are inside `ALLOWED_WORKDIRS` and the file really exists, "send file/image/video" buttons are attached (MP4 is sent as video) |

Artifact delivery inherits the gateway's existing rules:

- `TELEGRAM_MAX_FILE_BYTES` (default 45 MiB) is the send limit, derived from the Bot API's 50 MB upload limit
- An artifact over the limit is not auto-sent; the card only shows a "path" button that replies with the local absolute path
- `AUTO_SEND_ARTIFACTS` (default `off`) decides whether artifacts are sent automatically
- Sending files over 50 MB requires a self-hosted Local Bot API Server, which the gateway does not manage

So a long-video extension should bound its own output size, or explicitly return only a path when it exceeds the limit.

## Security Model

The process runs with the gateway account's privileges, **exactly as privileged as an approval-free CLI**. The gateway guarantees only three things:

1. `exec` is an argv array with `shell=False`, so user arguments are never interpreted by a shell;
2. `cwd` and artifact paths must stay inside `ALLOWED_WORKDIRS`;
3. On timeout the whole process group is killed.

**There is no sandbox beyond that.** Installing an extension is equivalent to putting code into the gateway process's own privilege environment, so only install extensions whose source you have reviewed; do not use binary extensions from untrusted sources.

## Minimal Verification

```bash
# Run the plugin itself, simulating the arguments the gateway passes
cd extensions/example
TG_WORKDIR=/srv/projects python3 main.py hello world
```
