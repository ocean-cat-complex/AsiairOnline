#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_CONFIG="$ROOT/config/devices.json"
if [[ -f "$ROOT/devices.json" ]]; then
  DEFAULT_CONFIG="$ROOT/devices.json"
fi
CONFIG="${CONFIG:-$DEFAULT_CONFIG}"
PYTHON="${PYTHON:-python3}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"

export PYTHONPATH="$ROOT/src"
export PYTHONDONTWRITEBYTECODE=1

args=(--config "$CONFIG" web --host "$HOST" --port "$PORT")
if [[ "${ALLOW_REMOTE_ACTIONS:-0}" == "1" ]]; then
  args+=(--allow-remote-actions)
fi
if [[ "${READ_ONLY:-0}" == "1" ]]; then
  args+=(--read-only)
fi

exec "$PYTHON" -m asiairbridge "${args[@]}" "$@"
