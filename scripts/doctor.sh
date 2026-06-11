#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${CONFIG:-$ROOT/config/devices.json}"
PYTHON="${PYTHON:-python3}"

export PYTHONPATH="$ROOT/src"
export PYTHONDONTWRITEBYTECODE=1

exec "$PYTHON" -m asiairbridge --config "$CONFIG" doctor "$@"
