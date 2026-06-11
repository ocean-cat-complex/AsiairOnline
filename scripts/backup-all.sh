#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${CONFIG:-$ROOT/config/devices.json}"
PYTHON="${PYTHON:-python3}"

export PYTHONPATH="$ROOT/src"
export PYTHONDONTWRITEBYTECODE=1

mode="--dry-run"
if [[ "${RUN_BACKUP:-0}" == "1" ]]; then
  mode="--no-dry-run"
fi

exec "$PYTHON" -m asiairbridge --config "$CONFIG" backup "$mode" "$@"
