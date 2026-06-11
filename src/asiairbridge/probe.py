from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str


def copy_backend_available() -> ProbeResult:
    path = shutil.which("rsync")
    if path:
        return ProbeResult(True, f"rsync: {path}")
    return ProbeResult(True, "python copy fallback")


def ping_host(host: str, timeout_ms: int = 1000) -> ProbeResult:
    cmd, timeout_seconds = _ping_command(host, timeout_ms)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult(False, str(exc))
    detail = _first_meaningful_line(proc.stdout) or _first_meaningful_line(proc.stderr)
    return ProbeResult(proc.returncode == 0, detail or f"exit {proc.returncode}")


def tcp_open(host: str, port: int, timeout_seconds: float = 2.0) -> ProbeResult:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return ProbeResult(True, f"tcp/{port} open")
    except OSError as exc:
        return ProbeResult(False, f"tcp/{port} closed or unreachable: {exc}")


def path_exists(path: Path) -> ProbeResult:
    try:
        if path.exists():
            return ProbeResult(True, "exists")
        return ProbeResult(False, "not found")
    except OSError as exc:
        return ProbeResult(False, str(exc))


def net_view(host: str) -> ProbeResult:
    return _smb_view(host)


def _ping_command(host: str, timeout_ms: int) -> tuple[list[str], float]:
    timeout_seconds = max(1.0, timeout_ms / 1000 + 0.5)
    if sys.platform == "darwin":
        return ["ping", "-c", "1", "-W", str(timeout_ms), host], timeout_seconds
    wait_seconds = max(1, int((timeout_ms + 999) / 1000))
    return ["ping", "-c", "1", "-W", str(wait_seconds), host], timeout_seconds


def _smb_view(host: str) -> ProbeResult:
    smbutil = shutil.which("smbutil")
    if smbutil:
        cmd = [smbutil, "view", "-N", f"//{host}"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProbeResult(False, str(exc))
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return ProbeResult(proc.returncode == 0, output or f"exit {proc.returncode}")

    tcp = tcp_open(host, 445)
    if tcp.ok:
        return ProbeResult(True, "smbutil not found; tcp/445 is open")
    return ProbeResult(False, f"smbutil not found; {tcp.detail}")


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
