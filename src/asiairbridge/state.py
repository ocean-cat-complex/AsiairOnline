from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


class RunLock:
    def __init__(
        self,
        path: Path,
        force: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.force = force
        self.metadata = metadata or {}
        self._fd: int | None = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            holder_pid = _read_lock_pid(self.path)
            alive = _pid_is_running(holder_pid) if holder_pid else None
            if self.force:
                if alive is True:
                    raise RuntimeError(
                        f"Refusing --force-lock: backup process pid={holder_pid} "
                        f"still appears to be running. Stop it first: {self.path}"
                    )
                self.path.unlink()
            elif alive is False:
                self.path.unlink()

        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            holder_pid = _read_lock_pid(self.path)
            raise RuntimeError(
                f"Another backup run appears active (pid={holder_pid}): {self.path}. "
                f"If you are sure none is running, rerun with --force-lock."
            ) from exc

        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        payload.update(self.metadata)
        os.write(self._fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        os.fsync(self._fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def write_run_state(state_dir: Path, run_id: str, payload: dict[str, Any]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = state_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_path = runs_dir / f"{run_id}.json"
    latest_path = state_dir / "latest.json"

    _write_json(run_path, payload)
    _write_json(latest_path, payload)
    return run_path


def read_latest_state(state_dir: Path) -> dict[str, Any] | None:
    latest_path = state_dir / "latest.json"
    if not latest_path.exists():
        return None
    try:
        with latest_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _read_lock_pid(path: Path) -> int | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return pid or None


def _pid_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)
