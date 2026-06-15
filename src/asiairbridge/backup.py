from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import AppConfig, Device, SourceRoot
from .probe import ProbeResult, tcp_open


@dataclass(frozen=True)
class BackupJob:
    device: Device
    source: SourceRoot
    source_path: Path
    destination_path: Path
    log_path: Path


@dataclass(frozen=True)
class BackupResult:
    job: BackupJob
    ok: bool
    status: str
    exit_code: int | None
    detail: str
    started_at: str
    finished_at: str
    copy_backend: str


@dataclass(frozen=True)
class CopyBackend:
    name: str
    executable: str | None = None


@dataclass(frozen=True)
class CopyRunResult:
    ok: bool
    exit_code: int | None
    detail: str


def build_jobs(
    config: AppConfig,
    run_id: str,
    device_names: list[str] | None = None,
    source_labels: list[str] | None = None,
) -> list[BackupJob]:
    selected_labels = set(source_labels or [])
    jobs: list[BackupJob] = []
    day_dir = config.logs_path() / datetime.now().strftime("%Y-%m-%d")

    for device in config.get_devices(device_names):
        for source in config.source_roots_for(device):
            if not source.enabled:
                continue
            if selected_labels and source.label not in selected_labels:
                continue
            source_path = source.render(device)
            destination_path = config.project.destination_root / device.name / source.safe_label
            log_name = f"{run_id}_{device.name}_{source.safe_label}.log"
            jobs.append(
                BackupJob(
                    device=device,
                    source=source,
                    source_path=source_path,
                    destination_path=destination_path,
                    log_path=day_dir / log_name,
                )
            )

    return jobs


def run_job(config: AppConfig, job: BackupJob, dry_run: bool) -> BackupResult:
    started_at = datetime.now().isoformat(timespec="seconds")
    port = config.backup.smb_port
    tcp = _tcp_open_any(
        job.device,
        port,
        retry_count=config.backup.retry_count,
        retry_wait_seconds=config.backup.retry_wait_seconds,
    )
    if not tcp.ok:
        finished_at = datetime.now().isoformat(timespec="seconds")
        return BackupResult(
            job=job,
            ok=False,
            status="skipped",
            exit_code=None,
            detail=tcp.detail,
            started_at=started_at,
            finished_at=finished_at,
            copy_backend="none",
        )

    backend = _select_copy_backend()
    if backend is None:
        finished_at = datetime.now().isoformat(timespec="seconds")
        return BackupResult(
            job=job,
            ok=False,
            status="failed",
            exit_code=None,
            detail="no supported copy backend found",
            started_at=started_at,
            finished_at=finished_at,
            copy_backend="none",
        )

    job.log_path.parent.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        job.destination_path.mkdir(parents=True, exist_ok=True)

    copy_result = _run_copy_backend(config, backend, job, dry_run)
    finished_at = datetime.now().isoformat(timespec="seconds")

    return BackupResult(
        job=job,
        ok=copy_result.ok,
        status="ok" if copy_result.ok else "failed",
        exit_code=copy_result.exit_code,
        detail=copy_result.detail,
        started_at=started_at,
        finished_at=finished_at,
        copy_backend=backend.name,
    )


def result_to_dict(result: BackupResult) -> dict[str, object]:
    job = result.job
    return {
        "device": job.device.name,
        "ip": job.device.ip,
        "endpoints": [endpoint.as_dict() for endpoint in job.device.endpoint_candidates()],
        "source_label": job.source.label,
        "source_path": str(job.source_path),
        "destination_path": str(job.destination_path),
        "log_path": str(job.log_path),
        "ok": result.ok,
        "status": result.status,
        "exit_code": result.exit_code,
        "detail": result.detail,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "copy_backend": result.copy_backend,
    }


def _tcp_open_any(
    device: Device,
    port: int,
    *,
    retry_count: int,
    retry_wait_seconds: int,
) -> ProbeResult:
    failures: list[str] = []
    for attempt in range(retry_count + 1):
        failures = []
        for endpoint in device.endpoint_candidates():
            result = tcp_open(endpoint.ip, port, timeout_seconds=5.0)
            if result.ok:
                return result
            failures.append(f"{endpoint.label} {endpoint.ip}: {result.detail}")
        if attempt < retry_count:
            time.sleep(retry_wait_seconds)

    return ProbeResult(False, "; ".join(failures))


def _select_copy_backend() -> CopyBackend | None:
    rsync = shutil.which("rsync")
    if rsync:
        return CopyBackend("rsync", rsync)
    return CopyBackend("python")


def _run_copy_backend(
    config: AppConfig,
    backend: CopyBackend,
    job: BackupJob,
    dry_run: bool,
) -> CopyRunResult:
    if backend.name == "rsync" and backend.executable:
        cmd = _rsync_command(config, backend.executable, job, dry_run)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=config.backup.job_timeout_hours * 3600,
            )
        except subprocess.TimeoutExpired:
            _write_text_log(
                job.log_path,
                f"rsync timed out after {config.backup.job_timeout_hours}h",
            )
            return CopyRunResult(
                False,
                None,
                f"timed out after {config.backup.job_timeout_hours}h (rsync killed)",
            )
        _write_process_log(job.log_path, cmd, proc)
        return CopyRunResult(proc.returncode == 0, proc.returncode, _summarize_process(proc, "rsync"))

    return _python_copy(config, job, dry_run)


def _rsync_command(
    config: AppConfig,
    rsync: str,
    job: BackupJob,
    dry_run: bool,
) -> list[str]:
    command = [
        rsync,
        "-a",
        "--itemize-changes",
        "--human-readable",
    ]
    if dry_run:
        command.append("--dry-run")
    if not config.backup.copy_empty_dirs:
        command.append("--prune-empty-dirs")
    for pattern in config.backup.exclude_dirs + config.backup.exclude_files:
        command.extend(["--exclude", pattern])
    command.extend([_as_rsync_dir(job.source_path), _as_rsync_dir(job.destination_path)])
    return command


def _python_copy(config: AppConfig, job: BackupJob, dry_run: bool) -> CopyRunResult:
    if not job.source_path.exists():
        return CopyRunResult(False, None, f"source not found: {job.source_path}")
    if not job.source_path.is_dir():
        return CopyRunResult(False, None, f"source is not a directory: {job.source_path}")

    copied = 0
    skipped = 0
    created_dirs = 0
    planned_bytes = 0
    try:
        for root, dirs, files in os.walk(job.source_path):
            dirs[:] = [name for name in dirs if not _is_excluded(name, config.backup.exclude_dirs)]
            source_root = Path(root)
            relative_root = source_root.relative_to(job.source_path)
            destination_root = job.destination_path / relative_root
            if config.backup.copy_empty_dirs and not dry_run:
                destination_root.mkdir(parents=True, exist_ok=True)
                created_dirs += 1
            for name in files:
                if _is_excluded(name, config.backup.exclude_files):
                    continue
                source_file = source_root / name
                destination_file = destination_root / name
                try:
                    source_stat = source_file.stat()
                except OSError:
                    skipped += 1
                    continue
                if _destination_is_current(destination_file, source_stat.st_size, source_stat.st_mtime):
                    skipped += 1
                    continue
                copied += 1
                planned_bytes += source_stat.st_size
                if not dry_run:
                    destination_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_file, destination_file)
    except OSError as exc:
        return CopyRunResult(False, 1, str(exc))

    mode = "would copy" if dry_run else "copied"
    detail = (
        f"python {mode} {copied} file(s), {planned_bytes} bytes; "
        f"skipped {skipped}; created_dirs {0 if dry_run else created_dirs}"
    )
    _write_text_log(job.log_path, detail)
    return CopyRunResult(True, 0, detail)


def _summarize_process(proc: subprocess.CompletedProcess[str], backend: str) -> str:
    lines = []
    for stream in (proc.stdout, proc.stderr):
        for line in stream.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
    if not lines:
        return f"{backend} exit {proc.returncode}"
    return lines[-1][:500]


def _write_process_log(
    path: Path,
    cmd: list[str],
    proc: subprocess.CompletedProcess[str],
) -> None:
    body = [
        f"command: {_format_command(cmd)}",
        f"exit_code: {proc.returncode}",
        "",
        "[stdout]",
        proc.stdout,
        "",
        "[stderr]",
        proc.stderr,
    ]
    _write_text_log(path, "\n".join(body))


def _write_text_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(text.rstrip())
        fh.write("\n")


def _as_rsync_dir(path: Path) -> str:
    text = str(path)
    return text if text.endswith(("/", "\\")) else f"{text}/"


def _is_excluded(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _destination_is_current(path: Path, size: int, mtime: float) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    return stat.st_size == size and int(stat.st_mtime) >= int(mtime)


def _format_command(cmd: list[str]) -> str:
    return " ".join(_quote_arg(item) for item in cmd)


def _quote_arg(value: str) -> str:
    if not value or any(char.isspace() for char in value):
        return repr(value)
    return value
