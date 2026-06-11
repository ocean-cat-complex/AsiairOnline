# AsiairOnline

ASIAIR monitoring and backup sidecar for macOS hosts on the same Tailscale or private LAN as the devices.

This macOS branch provides:

- Live ASIAIR JSON-RPC monitoring with endpoint failover.
- Current-image preview and guarded camera controls.
- Local material index backed by the configured backup destination.
- Incremental, dry-run-by-default backup from mounted ASIAIR SMB shares.
- A three-device model where each physical ASIAIR can expose multiple network endpoints.

## Requirements

- macOS with Homebrew arm64 Python 3.13 recommended.
- Tailscale connected to the device subnet, or direct LAN access.
- ASIAIR SMB shares mounted under `/Volumes/<device name>/...` before running backups.
- Python 3.12 or newer.

## Quick Start

Create a private config:

```bash
cp config/devices.example.json config/devices.json
$EDITOR config/devices.json
```

This branch also accepts a private root-level `devices.json` for local testing. Both private config paths are ignored by git.

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

Open `http://127.0.0.1:8787/`; the root URL lands on the live monitor.

Expose it to other machines in the tailnet only when needed:

```bash
HOST=0.0.0.0 PYTHON=/opt/homebrew/bin/python3.13 ./scripts/start-web.sh
```

## Web Dashboard

| Path | Page |
| --- | --- |
| `/` or `/monitor-minterm` | Live device monitor, the MINTERM ops console |
| `/camera` | Current-image preview, camera status, exposure controls, and control lease actions |
| `/materials` | Local material library browser |

The old backup-console landing page and legacy `/monitor` page are removed. The `/api/*` JSON endpoints, including `/api/status`, `/api/devices`, `/api/rpc-monitor`, `/api/materials/*`, and camera APIs, remain available.

### Read-only vs. Writable

Access to write actions is gated in layers:

- The server binds `127.0.0.1` by default.
- `--read-only` runs a monitoring-only server.
- Without `--read-only`, loopback clients can trigger scans, backups, and camera actions when they hold the control lease.
- Non-loopback tailnet clients remain read-only unless the server starts with `--allow-remote-actions`.
- Camera control additionally requires holding the per-device control lease in the UI.

Run exactly one writable controller per ASIAIR device. Additional viewers should use read-only mode or one shared backend exposed through Tailscale.

## Configuration

All environment-specific values live in `config/devices.json` or local `devices.json`.

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

## Reliability

- Backups are dry-run by default; a real copy requires `RUN_BACKUP=1` or `--no-dry-run`.
- A stale lock left by a crashed or killed backup is reclaimed automatically once its PID is confirmed dead.
- `--force-lock` refuses to clear a lock whose owner is still alive, preventing two concurrent runs against the same destination.
- Run-state and dashboard cache files are written atomically with a temp file and `os.replace`.
- Corrupt or truncated `latest.json` is treated as absent instead of taking down the dashboard.
- Device RPC reads are bounded by the per-call timeout budget and a response-size cap.
- Configuration is range-validated at load time and bad path templates report clear `ConfigError` messages.

## Safety

Backups are incremental and never use mirror-delete behavior. The rsync backend does not pass `--delete`; the Python fallback only copies new or changed files.

The web service binds to `127.0.0.1` by default. Use `HOST=0.0.0.0` only for intentional tailnet access, and add `--allow-remote-actions` only when remote write control is explicitly required.

## Project Layout

- `config/devices.example.json`: public macOS example configuration.
- `src/asiairbridge/`: CLI, backup logic, JSON-RPC monitor, web server, camera operations, and material library.
- `scripts/*.sh`: macOS/Linux operational entry points.
- `docs/asiair-monitor-minterm-live.html`: live monitor frontend.
- `docs/asiair-image-preview.html`: camera frontend.
- `docs/asiair-materials.html`: material library frontend.
- `logs/`: per-run logs, git-ignored.
- `state/`: runtime state, locks, caches, SQLite databases, and generated previews, git-ignored.
