# Agent Guide - AsiairOnline macOS Branch

This repository is the ASIAIR sidecar for the remote observatory website. Read the parent project `AGENTS.md` first when working inside `/Users/apple-1/light_little_toys/Website of Remote Observatory`, then use this file for the sidecar-specific rules.

## Scope

- Current branch target: `asiair_online_macos`.
- Runtime role: independent sidecar service for ASIAIR monitoring, current image preview/cache, read-only mount status, guarded camera controls, local material indexing, and incremental backup.
- Python: `>=3.12`; on this Mac prefer `/opt/homebrew/bin/python3.13`.
- Host assumptions: macOS with Tailscale or private LAN access to ASIAIR devices.

Do not modify the Flask main site's dependencies or virtual environment to run this sidecar.

## Device Model

Model three physical ASIAIR boxes as three `Device` records. Each physical device can have multiple `endpoints`, usually:

- `wired`: Ethernet IP, priority `0`.
- `wifi-bridge`: wireless bridge IP, priority `10`.

Read-only RPC and image reads may fail over across enabled endpoints. Write/control operations should use only the preferred endpoint unless a feature explicitly proves idempotency.

## Files

```text
src/asiairbridge/       Python package and CLI
scripts/*.sh            macOS/Linux operational scripts
docs/asiair-*.html      Runtime frontend pages served by src/asiairbridge/web.py
docs/ops-*.html         Current OPS frontend pages served by the main routes
config/devices.example.json
tests/
```

Runtime/private files are ignored by git:

```text
config/devices.json
devices.json
logs/
state/
.venv/
```

## Commands

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /opt/homebrew/bin/python3.13 -B -m compileall -q src tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /opt/homebrew/bin/python3.13 -B -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /opt/homebrew/bin/python3.13 -B -m asiairbridge --json --config config/devices.example.json plan
git diff --check
```

Operational scripts:

```bash
PYTHON=/opt/homebrew/bin/python3.13 ./scripts/doctor.sh
PYTHON=/opt/homebrew/bin/python3.13 ./scripts/start-web.sh
PYTHON=/opt/homebrew/bin/python3.13 ./scripts/backup-all.sh
RUN_BACKUP=1 PYTHON=/opt/homebrew/bin/python3.13 ./scripts/backup-all.sh
```

Main local pages on port `8787`: `/` or `/monitor-minterm`, `/camera`, `/mount`, and `/materials`.
Core read-only APIs: `/api/status`, `/api/devices`, `/api/rpc-monitor`, `/api/mount-state`, `/api/current-image`, and `/api/current-image-file`.

Live current-image acquisition belongs in this sidecar. `/api/current-image` should read ASIAIR's `get_current_img` preview stream directly from the device, trying port `4800` first and port `4801` second. The packet is an ASIAIR header plus ZIP-compressed `raw_data` containing 8/16-bit mono preview pixels; the sidecar decompresses it, stretches it, and writes only the temporary preview cache under `state/image-preview/`. Do not make live current-image display depend on macOS `/Volumes` mounts, ASIAIR storage scans, the backup destination, or the material-library SQLite index. Downstream display apps such as the 5010 standalone monitor should consume `/api/current-image` and `/api/current-image-file` without adding their own ASIAIR image fallback.

The material-library SQLite index is lazy and should only initialize when `/materials` or `/api/materials/*` is used. Monitoring/status/current-image paths must not create or require that database.

## Safety

- Keep real ASIAIR IPs, SMB paths, and credentials in `config/devices.json`, not in tracked files.
- The web service binds to `127.0.0.1` by default. Use `HOST=0.0.0.0` only for intentional tailnet access.
- Backup is incremental. Do not add mirror-delete behavior.
- Do not reintroduce deleted platform-specific task scripts or copy backends on this branch.
