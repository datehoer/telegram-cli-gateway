#!/bin/sh
set -eu

umask 077

backend="${AGENT_BACKEND:?AGENT_BACKEND is required}"
token_file="/run/secrets/telegram_bot_token"
if [ ! -s "$token_file" ]; then
    echo "Telegram bot token secret is missing" >&2
    exit 1
fi
TELEGRAM_BOT_TOKEN="$(tr -d '\r\n' < "$token_file")"
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "Telegram bot token secret is empty" >&2
    exit 1
fi
export TELEGRAM_BOT_TOKEN

case "$backend" in
    codex)
        install -d -m 0700 "$HOME/.codex"
        if [ ! -s "$HOME/.codex/auth.json" ]; then
            install -m 0600 /run/secrets/codex_auth "$HOME/.codex/auth.json"
        fi
        install -m 0600 /run/secrets/codex_config "$HOME/.codex/config.toml"
        ;;
    claude)
        install -d -m 0700 "$HOME/.claude"
        install -m 0600 /run/secrets/claude_settings "$HOME/.claude/settings.json"
        ;;
    grok)
        install -d -m 0700 "$HOME/.grok"
        if [ ! -s "$HOME/.grok/auth.json" ]; then
            install -m 0600 /run/secrets/grok_auth "$HOME/.grok/auth.json"
        fi
        install -m 0600 /run/secrets/grok_config "$HOME/.grok/config.toml"
        ;;
    *)
        echo "Unsupported AGENT_BACKEND: $backend" >&2
        exit 1
        ;;
esac

workspace="/workspace/project"
if [ ! -f "$workspace/.git/HEAD" ]; then
    mkdir -p "$workspace"
    cp -a /opt/gateway/. "$workspace/"
    git -C "$workspace" init -b main >/dev/null
    git -C "$workspace" config user.name "Experiment Agent"
    git -C "$workspace" config user.email "agent@container.invalid"
    git -C "$workspace" add -A
    git -C "$workspace" commit -m "experiment baseline" >/dev/null
fi

exec "$@"
