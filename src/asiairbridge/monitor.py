from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backup import BackupJob, build_jobs
from .config import AppConfig
from .rpc import asiair_device_rpc
from .state import read_latest_state


SOURCE_STATS_FILE = "source-stats.json"
PATH_STATS_FILE = "path-stats-cache.json"
PROGRESS_SAMPLES_FILE = "progress-samples.json"
NETWORK_STATS_FILE = "network-stats-cache.json"
ASIAIR_EMMC_LABEL = "EMMC Images"


@dataclass(frozen=True)
class PathStats:
    path: Path
    exists: bool
    file_count: int
    bytes: int
    latest_mtime: float | None
    scanned_at: str
    scan_seconds: float
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "exists": self.exists,
            "file_count": self.file_count,
            "bytes": self.bytes,
            "gb": round(self.bytes / 1024**3, 2),
            "latest_mtime": self.latest_mtime,
            "latest_mtime_text": _format_timestamp(self.latest_mtime),
            "scanned_at": self.scanned_at,
            "scan_seconds": round(self.scan_seconds, 2),
            "error": self.error,
        }


def dashboard_snapshot(
    config: AppConfig,
    source_label: str | None = None,
) -> dict[str, Any]:
    source_labels = [source_label] if source_label else None
    jobs = build_jobs(config, "monitor", source_labels=source_labels)
    source_cache = _load_json(_state_file(config, SOURCE_STATS_FILE), {})
    samples = _load_json(_state_file(config, PROGRESS_SAMPLES_FILE), {})
    path_cache = _load_json(_state_file(config, PATH_STATS_FILE), {})
    now = time.time()
    network = collect_network_stats(config, now)

    job_rows = []
    for job in jobs:
        local_stats = _cached_path_stats(config, job.destination_path, path_cache, now)
        source_stats = source_cache.get(_job_key(job))
        sample_info = _update_samples(samples, _job_key(job), local_stats.bytes, now)
        job_rows.append(
            _job_status(
                job=job,
                local_stats=local_stats,
                source_stats=source_stats,
                sample_info=sample_info,
            )
        )

    _write_json(_state_file(config, PATH_STATS_FILE), path_cache)
    _write_json(_state_file(config, PROGRESS_SAMPLES_FILE), samples)

    lock = read_lock(config.project.lock_file)
    lock["active_jobs"] = infer_active_jobs(jobs, lock)
    latest = read_latest_state(config.state_path())
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project": {
            "root": str(config.root),
            "destination_root": str(config.project.destination_root),
            "config_path": str(config.path),
        },
        "lock": lock,
        "latest": latest,
        "network": network,
        "jobs": job_rows,
        "logs": list_recent_logs(config),
    }


def infer_active_jobs(jobs: list[BackupJob], lock: dict[str, Any]) -> list[dict[str, str]]:
    if not lock.get("active") or lock.get("pid_alive") is False:
        return []

    active: list[BackupJob] = []
    lock_devices = set(lock.get("devices") or [])
    lock_sources = set(lock.get("source_labels") or [])
    if lock_devices:
        for job in jobs:
            if job.device.name not in lock_devices:
                continue
            if lock_sources and job.source.label not in lock_sources:
                continue
            active.append(job)

    seen = set()
    rows = []
    for job in active:
        key = (job.device.name, job.source.label)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"device": job.device.name, "source_label": job.source.label})
    return rows


def scan_source_totals(
    config: AppConfig,
    device_names: list[str] | None = None,
    source_labels: list[str] | None = None,
) -> dict[str, Any]:
    jobs = build_jobs(config, "scan", device_names, source_labels)
    cache = _load_json(_state_file(config, SOURCE_STATS_FILE), {})
    results: list[dict[str, Any]] = []
    cache_path = _state_file(config, SOURCE_STATS_FILE)
    for job in jobs:
        item = collect_source_stats(config, job)
        cache[_job_key(job)] = item
        results.append(item)
        _write_json(cache_path, cache)

    return {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "results": results,
    }


def collect_source_stats(config: AppConfig, job: BackupJob) -> dict[str, Any]:
    if job.source.label == ASIAIR_EMMC_LABEL:
        try:
            return collect_asiair_emmc_stats(job)
        except (OSError, ValueError, json.JSONDecodeError, TimeoutError):
            pass

    stats = collect_path_stats(
        job.source_path,
        exclude_dirs=config.backup.exclude_dirs,
        exclude_files=config.backup.exclude_files,
    )
    return {
        "device": job.device.name,
        "source_label": job.source.label,
        "source_path": str(job.source_path),
        "scan_method": "smb_walk",
        "stats": stats.as_dict(),
    }


def collect_asiair_emmc_stats(job: BackupJob) -> dict[str, Any]:
    started = time.perf_counter()
    scanned_at = datetime.now().isoformat(timespec="seconds")
    response = asiair_device_rpc(job.device, "get_disk_volume")
    if int(response.get("code") or 0) != 0:
        raise ValueError(f"ASIAIR get_disk_volume failed: {response}")

    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"ASIAIR get_disk_volume returned no result: {response}")

    total_mb = int(result["totalMB"])
    free_mb = int(result["freeMB"])
    used_mb = max(total_mb - free_mb, 0)
    total_bytes = total_mb * 1024**2
    free_bytes = free_mb * 1024**2
    used_bytes = used_mb * 1024**2
    stats = {
        "path": str(job.source_path),
        "exists": True,
        "file_count": 0,
        "bytes": used_bytes,
        "gb": round(used_bytes / 1024**3, 2),
        "latest_mtime": None,
        "latest_mtime_text": None,
        "scanned_at": scanned_at,
        "scan_seconds": round(time.perf_counter() - started, 2),
        "error": None,
        "method": "asiair_jsonrpc_get_disk_volume",
        "disk_total_mb": total_mb,
        "disk_free_mb": free_mb,
        "disk_used_mb": used_mb,
        "disk_total_bytes": total_bytes,
        "disk_free_bytes": free_bytes,
        "disk_used_bytes": used_bytes,
    }
    return {
        "device": job.device.name,
        "source_label": job.source.label,
        "source_path": str(job.source_path),
        "scan_method": "asiair_jsonrpc_get_disk_volume",
        "stats": stats,
    }


def collect_path_stats(
    path: Path,
    exclude_dirs: tuple[str, ...] = (),
    exclude_files: tuple[str, ...] = (),
) -> PathStats:
    started = time.perf_counter()
    scanned_at = datetime.now().isoformat(timespec="seconds")
    file_count = 0
    total_bytes = 0
    latest_mtime: float | None = None

    try:
        exists = path.exists()
    except OSError as exc:
        return PathStats(path, False, 0, 0, None, scanned_at, time.perf_counter() - started, str(exc))

    if not exists:
        return PathStats(path, False, 0, 0, None, scanned_at, time.perf_counter() - started)

    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [name for name in dirs if not _is_excluded(name, exclude_dirs)]
            for name in files:
                if _is_excluded(name, exclude_files):
                    continue
                file_path = Path(root) / name
                try:
                    stat = file_path.stat()
                except OSError:
                    continue
                file_count += 1
                total_bytes += stat.st_size
                if latest_mtime is None or stat.st_mtime > latest_mtime:
                    latest_mtime = stat.st_mtime
    except OSError as exc:
        return PathStats(
            path,
            True,
            file_count,
            total_bytes,
            latest_mtime,
            scanned_at,
            time.perf_counter() - started,
            str(exc),
        )

    return PathStats(
        path,
        True,
        file_count,
        total_bytes,
        latest_mtime,
        scanned_at,
        time.perf_counter() - started,
    )


def collect_network_stats(config: AppConfig, now: float) -> dict[str, Any]:
    cache_path = _state_file(config, NETWORK_STATS_FILE)
    cache = _load_json(cache_path, {})
    tailscale = _collect_tailscale_stats(cache.get("tailscale"), now)
    if tailscale is not None:
        cache["tailscale"] = tailscale
        _write_json(cache_path, cache)
    return {"tailscale": cache.get("tailscale")}


def _collect_tailscale_stats(previous: dict[str, Any] | None, now: float) -> dict[str, Any] | None:
    if previous and now - float(previous.get("sampled_at", 0)) < 5:
        return previous
    return _collect_tailscale_cli_stats(previous, now)


def _collect_tailscale_cli_stats(previous: dict[str, Any] | None, now: float) -> dict[str, Any] | None:
    executable = _tailscale_executable()
    if executable is None:
        return None
    try:
        proc = subprocess.run(
            [executable, "status", "--json"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "adapter": "Tailscale",
            "source": "tailscale_cli",
            "sampled_at": now,
            "error": str(exc),
        }
    if proc.returncode != 0:
        return {
            "ok": False,
            "adapter": "Tailscale",
            "source": "tailscale_cli",
            "sampled_at": now,
            "error": proc.stderr.strip() or proc.stdout.strip(),
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "adapter": "Tailscale",
            "source": "tailscale_cli",
            "sampled_at": now,
            "error": str(exc),
        }

    self_info = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    peer_info = payload.get("Peer") if isinstance(payload.get("Peer"), dict) else {}
    received_bytes = int(self_info.get("RxBytes") or 0)
    sent_bytes = int(self_info.get("TxBytes") or 0)
    if received_bytes == 0 and sent_bytes == 0:
        for peer in peer_info.values():
            if not isinstance(peer, dict):
                continue
            received_bytes += int(peer.get("RxBytes") or 0)
            sent_bytes += int(peer.get("TxBytes") or 0)

    receive_rate = None
    send_rate = None
    if previous and previous.get("ok"):
        elapsed = now - float(previous.get("sampled_at", now))
        if elapsed > 0:
            receive_delta = max(received_bytes - int(previous.get("received_bytes") or 0), 0)
            send_delta = max(sent_bytes - int(previous.get("sent_bytes") or 0), 0)
            receive_rate = receive_delta / elapsed
            send_rate = send_delta / elapsed

    return {
        "ok": bool(payload.get("BackendState") == "Running" or payload.get("TUN")),
        "adapter": "Tailscale",
        "source": "tailscale_cli",
        "sampled_at": now,
        "backend_state": payload.get("BackendState"),
        "tailscale_ips": payload.get("TailscaleIPs") or [],
        "received_bytes": received_bytes,
        "sent_bytes": sent_bytes,
        "receive_bytes_per_second": receive_rate,
        "send_bytes_per_second": send_rate,
    }


def _tailscale_executable() -> str | None:
    path = shutil.which("tailscale")
    if path:
        return path
    candidates = [
        Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def read_lock(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"active": False, "path": str(path)}
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "active": True,
            "path": str(path),
            "pid_alive": None,
            "error": str(exc),
        }

    pid = int(payload.get("pid") or 0)
    payload.update(
        {
            "active": True,
            "path": str(path),
            "pid_alive": pid_is_running(pid) if pid else None,
        }
    )
    return payload


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def list_recent_logs(config: AppConfig, limit: int = 12) -> list[dict[str, Any]]:
    logs_dir = config.logs_path()
    if not logs_dir.exists():
        return []
    items = []
    for path in logs_dir.rglob("*.log"):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
        )
    items.sort(key=lambda item: item["modified_at"], reverse=True)
    return items[:limit]


def read_log_tail(config: AppConfig, path_text: str, max_lines: int = 120) -> dict[str, Any]:
    path = Path(path_text).resolve()
    logs_dir = config.logs_path().resolve()
    if logs_dir not in path.parents and path != logs_dir:
        raise ValueError("Log path is outside logs directory")
    lines: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-max_lines:]
    except OSError as exc:
        return {"path": str(path), "ok": False, "error": str(exc), "text": ""}
    return {
        "path": str(path),
        "ok": True,
        "text": "".join(lines),
    }


def _job_status(
    job: BackupJob,
    local_stats: PathStats,
    source_stats: dict[str, Any] | None,
    sample_info: dict[str, Any],
) -> dict[str, Any]:
    source_bytes = None
    source_scanned_at = None
    source_error = None
    if source_stats:
        stats = source_stats.get("stats") or {}
        source_bytes = stats.get("bytes")
        source_scanned_at = stats.get("scanned_at")
        source_error = stats.get("error")

    local_bytes = local_stats.bytes
    progress = None
    remaining_bytes = None
    eta_seconds = None
    if isinstance(source_bytes, int) and source_bytes > 0:
        progress = min(local_bytes / source_bytes, 1.0)
        remaining_bytes = max(source_bytes - local_bytes, 0)
        speed = sample_info.get("bytes_per_second") or 0
        if speed > 0 and remaining_bytes > 0:
            eta_seconds = int(remaining_bytes / speed)

    return {
        "device": job.device.name,
        "ip": job.device.ip,
        "endpoints": [endpoint.as_dict() for endpoint in job.device.endpoint_candidates()],
        "source_label": job.source.label,
        "source_path": str(job.source_path),
        "destination_path": str(job.destination_path),
        "local": local_stats.as_dict(),
        "source": source_stats,
        "source_bytes": source_bytes,
        "source_scanned_at": source_scanned_at,
        "source_error": source_error,
        "progress": progress,
        "remaining_bytes": remaining_bytes,
        "eta_seconds": eta_seconds,
        "bytes_per_second": sample_info.get("bytes_per_second"),
    }


def _cached_path_stats(
    config: AppConfig,
    path: Path,
    cache: dict[str, Any],
    now: float,
    ttl_seconds: int = 15,
) -> PathStats:
    key = str(path)
    item = cache.get(key)
    if item and now - float(item.get("cached_at", 0)) < ttl_seconds:
        stats = item.get("stats", {})
        return PathStats(
            path=path,
            exists=bool(stats.get("exists")),
            file_count=int(stats.get("file_count") or 0),
            bytes=int(stats.get("bytes") or 0),
            latest_mtime=stats.get("latest_mtime"),
            scanned_at=str(stats.get("scanned_at")),
            scan_seconds=float(stats.get("scan_seconds") or 0),
            error=stats.get("error"),
        )

    stats = collect_path_stats(path)
    cache[key] = {"cached_at": now, "stats": stats.as_dict()}
    return stats


def _update_samples(
    samples: dict[str, Any],
    key: str,
    bytes_value: int,
    now: float,
) -> dict[str, Any]:
    history = samples.setdefault(key, [])
    history.append({"time": now, "bytes": bytes_value})
    cutoff = now - 20 * 60
    history[:] = [item for item in history if item.get("time", 0) >= cutoff][-1200:]
    if len(history) < 2:
        return {"bytes_per_second": None}

    latest = history[-1]
    latest_bytes = int(latest["bytes"])
    latest_time = float(latest["time"])

    for item in reversed(history[:-1]):
        item_bytes = int(item["bytes"])
        if item_bytes >= latest_bytes:
            continue
        delta_seconds = latest_time - float(item["time"])
        if delta_seconds <= 0:
            continue
        return {"bytes_per_second": (latest_bytes - item_bytes) / delta_seconds}

    return {"bytes_per_second": 0}


def _state_file(config: AppConfig, filename: str) -> Path:
    return config.state_path() / filename


def _job_key(job: BackupJob) -> str:
    return f"{job.device.name}|{job.source.label}"


def _is_excluded(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _format_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
