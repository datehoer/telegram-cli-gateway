#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="/home/openclaw/.local/bin/uv"
if [[ -x "$UV_BIN" ]]; then
  exec "$UV_BIN" run --project "$PROJECT_DIR" telegram-cli-gateway "$@"
fi
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m cli_telegram_gateway "$@"
