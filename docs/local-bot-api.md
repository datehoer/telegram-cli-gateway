# Local Bot API: sending files up to 1 GiB

The official cloud Bot API limits uploads to 50 MB. A self-hosted server must run with
`--local` to allow uploads up to 2000 MB and sending by local path. The gateway's local
mode defaults to a 1 GiB limit; photos still follow the 10 MB photo rule.

Official references:
- https://core.telegram.org/bots/api#using-a-local-bot-api-server
- https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server
- https://core.telegram.org/api/obtaining_api_id

## Credentials and service setup

Get your own Telegram `api_id` and `api_hash` from `https://my.telegram.org/apps`.
These application credentials are not the Bot Token from BotFather. Put them in
`.telegram-bot-api.env` at the repository root with mode `0600`; Git ignores the file.
Never send it in a chat.

```dotenv
TELEGRAM_API_ID=your_numeric_api_id
TELEGRAM_API_HASH=your_api_hash
```

The deployment files fit this workspace: `/srv/projects/telegram-cli-gateway`, user
UID/GID 1000. The image builds from a pinned official source release, and the port binds
only `127.0.0.1:8081`. The container and the gateway share the same absolute file paths:
project files are read-only, and the Local Bot API's own state directory is writable.
The state directory holds private data too.

```bash
chmod 600 .telegram-bot-api.env
mkdir -p .runtime/local-bot-api/tmp
chmod 700 .runtime/local-bot-api .runtime/local-bot-api/tmp
docker compose -f docker/local-bot-api/compose.yaml build
docker compose -f docker/local-bot-api/compose.yaml up -d
```

Starting the container neither changes the gateway configuration nor logs the bots out
of the cloud.

## Switching and verification

Run the full test suite first, and switch only after every native CLI turn has finished.
The idle check must read `.runtime/state.json` and see `in_flight` empty several times in
a row; abort on a timeout or a read failure.

1. Stop polling in `telegram-cli-gateway.service` and keep a backup of the configuration.
2. Read every configured Bot Token with a private configuration loader and call `logOut`
   for each cloud bot. Keep tokens off the command line, print no request URLs, and do not
   drop the update queue. Stop the switch if any bot fails to log out.
3. Set these two values in `.env`, then start the gateway.

```dotenv
TELEGRAM_LOCAL_API_URL=http://127.0.0.1:8081
TELEGRAM_MAX_FILE_BYTES=1073741824
```

4. Check that the running process started after the changed files were written, and that
   every bot entrance passes a local `getMe`.
5. Send the original video in the same bot and chat that asked for it, confirm Telegram
   returns a successful Message, and check that `file_size` matches the original file.
   Passing unit tests does not prove the video was delivered.

Local uploads pass `file:///` URIs to `sendVideo`/`sendDocument`, so the gateway never
loads the whole file into memory. A bare absolute path would be read by the server as a
remote file identifier and cannot replace the file URI. Uploads wait up to 3600 seconds.
A video's thumbnail travels in the request body, so the server never reads a gateway
temporary file. The server reuses an earlier upload of the same path for as long as it
runs (it keeps no file database), together with the size and thumbnail sent the first
time: a video first sent without them stays a 320x320 square on every resend until the
server restarts or the file is copied to a new path.
After a network failure nothing is uploaded again automatically, to avoid duplicate
messages. When an incoming `getFile` returns an absolute path, the gateway copies it in
chunks and keeps the size limit, private permissions and atomic replacement.

## Rollback

Wait for idle and stop the gateway, call `logOut` for every bot on the local server,
remove `TELEGRAM_LOCAL_API_URL`, set `TELEGRAM_MAX_FILE_BYTES` back to `47185920`, then
start the gateway. After a cloud `logOut`, Telegram imposes a waiting period before the
bot can log in again, so a failed migration cannot promise an immediate return to the
cloud. Stop the local container with `docker compose -f docker/local-bot-api/compose.yaml down`;
the state directory stays. Never replay CLI turns automatically during a migration.
