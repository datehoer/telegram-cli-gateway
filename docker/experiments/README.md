# Three-Bot Isolation Experiment

This Compose stack runs one independent gateway each for Codex, Claude Code, and Grok.
Every service has only its own CLI, network, HOME, workspace, runtime, credentials, and
Telegram bot.

## Bot Mapping

- `codex`: one dedicated Telegram bot (bot handle redacted)
- `claude`: one dedicated Telegram bot (bot handle redacted)
- `grok`: one dedicated Telegram bot (bot handle redacted)

The bot tokens live in the Git-ignored `secrets/*.bot-token` files. Compose reads the same
`TELEGRAM_ALLOWED_USERS` from the project-root `.env` and never passes the original bot tokens
into the experiment containers.

CLI credentials are copied from the host's minimal auth files or this directory's private
configuration into each HOME; history, caches, memories, and old sessions are not copied.
Tokens never reach an image layer or a container environment variable.

## Usage

Run from the project root:

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml build
docker compose --env-file .env -f docker/experiments/compose.yaml up -d
docker compose --env-file .env -f docker/experiments/compose.yaml ps
```

Send one of these to each of the three bots the first time:

```text
/new codex
/new claude
/new grok
```

On first startup each workspace is copied from the same image snapshot into
`/workspace/project` and initialized as a fresh Git repository with a single baseline commit.
Candidate code and CLI sessions live in their own named volumes, so restarting a container
loses nothing.

Follow the logs:

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml logs -f codex
```

Export a candidate patch:

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml exec codex \
  git -C /workspace/project diff --binary HEAD
```

Stopping a container does not delete results:

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml stop
```

Do not use `down -v` unless you explicitly intend to delete all three experiment workspaces,
sessions, and runtimes.

The older local `telegram-cli-gateway.service` used the same token as the Codex experiment bot,
so it must stay disabled while the three-container stack is running. To fall back to the original
service, stop the containers first and then re-enable the service:

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml stop
systemctl --user enable --now telegram-cli-gateway.service
```

After rotating any bot or model API token, update the corresponding
`docker/experiments/secrets/` file and recreate the container configuration:

```bash
docker compose --env-file .env -f docker/experiments/compose.yaml up -d --force-recreate
```
