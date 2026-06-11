# AsiairOnline

ASIAIR monitoring and backup sidecar for macOS hosts on the same Tailscale or private LAN as the devices.

This branch is trimmed for the remote observatory website integration:

- Live ASIAIR JSON-RPC monitoring with endpoint failover.
- Current-image preview and guarded camera controls.
- Local material index backed by the configured backup destination.
- Incremental backup from mounted ASIAIR SMB shares.
- A three-device model where each physical ASIAIR can expose multiple network endpoints.

## Requirements

- macOS with Homebrew arm64 Python 3.13 recommended.
- Tailscale connected to the device subnet, or direct LAN access.
- ASIAIR SMB shares mounted under `/Volumes/<device name>/...` before running backups.

The package requires Python 3.12 or newer.

## Quick Start

Create a private config:

```bash
cp config/devices.example.json config/devices.json
$EDITOR config/devices.json
```

Run local checks:

```bash
PYTHON=/opt/homebrew/bin/python3.13 ./scripts/doctor.sh
```

Preview the backup plan without copying data:

```bash
PYTHON=/opt/homebrew/bin/python3.13 ./scripts/backup-all.sh
```

Run the incremental backup after the dry run looks correct:

```bash
RUN_BACKUP=1 PYTHON=/opt/homebrew/bin/python3.13 ./scripts/backup-all.sh
```

Start the web service on localhost:

```bash
PYTHON=/opt/homebrew/bin/python3.13 ./scripts/start-web.sh
```

Expose it to other machines in the tailnet only when needed:

```bash
HOST=0.0.0.0 PYTHON=/opt/homebrew/bin/python3.13 ./scripts/start-web.sh
```

Then open `http://<server-tailnet-ip>:8787/`.

## Web Pages

- `/monitor-minterm`: dense live monitor.
- `/camera`: current image preview, camera status, exposure controls, and control lease actions.
- `/materials`: local material library browser.

## Configuration

All environment-specific values live in `config/devices.json`, which is ignored by git.

Model each physical ASIAIR as one device with multiple `endpoints`:

```json
{
  "name": "asiair-a",
  "ip": "192.168.8.101",
  "endpoints": [
    {"label": "wired", "ip": "192.168.8.101", "priority": 0},
    {"label": "wifi-bridge", "ip": "192.168.8.102", "priority": 10}
  ]
}
```

Read-only RPC and image reads try enabled endpoints in priority order. Write/control actions use the preferred endpoint by default to avoid duplicate commands if a response times out on one link.

For backups, mount ASIAIR shares under paths that match `path_template`, for example:

```text
/Volumes/asiair-a/EMMC Images
/Volumes/asiair-b/TF Images
/Volumes/asiair-b/Udisk Images
```

Keep ASIAIR, SMB, and Tailscale credentials outside the repository.

## Safety

Backups are incremental and never use mirror-delete behavior. The rsync backend does not pass `--delete`; the Python fallback only copies new or changed files.

The web service binds to `127.0.0.1` by default. Use `HOST=0.0.0.0` only for intentional tailnet access.

## Project Layout

- `config/devices.example.json`: public macOS example configuration.
- `src/asiairbridge/`: CLI, backup logic, JSON-RPC monitor, web server, camera operations, and material library.
- `scripts/*.sh`: macOS/Linux operational entry points.
- `docs/asiair-monitor-minterm-live.html`: live monitor frontend.
- `docs/asiair-image-preview.html`: camera frontend.
- `docs/asiair-materials.html`: material library frontend.
- `logs/`: per-run logs, git-ignored.
- `state/`: runtime state, locks, caches, SQLite databases, and generated previews, git-ignored.
