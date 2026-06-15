#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_CONFIG="$ROOT/config/devices.json"
if [[ -f "$ROOT/devices.json" ]]; then
  DEFAULT_CONFIG="$ROOT/devices.json"
fi
CONFIG="${CONFIG:-$DEFAULT_CONFIG}"
PYTHON="${PYTHON:-python3}"

export PYTHONPATH="$ROOT/src"
export PYTHONDONTWRITEBYTECODE=1

mode="--dry-run"
if [[ "${RUN_BACKUP:-0}" == "1" ]]; then
  mode="--no-dry-run"
fi

exec "$PYTHON" -m asiairbridge --config "$CONFIG" backup "$mode" "$@"
