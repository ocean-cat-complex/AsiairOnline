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

exec "$PYTHON" -m asiairbridge --config "$CONFIG" doctor "$@"
