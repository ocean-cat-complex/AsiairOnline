from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig


IMAGE_EXTENSIONS = {".fit", ".fits", ".jpg", ".jpeg", ".png"}
RAW_EXTENSIONS = {".fit", ".fits"}
THUMB_SUFFIX = "_thn"
CAPTURED_AT_RE = re.compile(r"(20\d{6}-\d{6})")
EXPOSURE_RE = re.compile(r"_(\d+(?:\.\d+)?)(ms|s)_Bin(\d+)", re.IGNORECASE)
GAIN_RE = re.compile(r"_gain(\d+)", re.IGNORECASE)
TEMP_RE = re.compile(r"_(-?\d+(?:\.\d+)?)C(?:_|$)", re.IGNORECASE)
JPEG_QUALITY = 64


@dataclass(frozen=True)
class MaterialRecord:
    id: str
    device: str
    source_label: str
    relative_path: str
    full_path: str
    file_name: str
    extension: str
    size_bytes: int
    mtime: float
    mtime_text: str
    mode: str | None
    frame_type: str | None
    target: str | None
    captured_at: str | None
    exposure: str | None
    exposure_seconds: float | None
    bin: int | None
    gain: int | None
    temperature_c: float | None
    preview_path: str | None
    preview_status: str
    preview_bytes: int | None
    preview_generated_at: str | None
    preview_error: str | None


class MaterialLibrary:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = config.project.destination_root
        self.state_dir = config.state_path() / "materials"
        self.preview_dir = self.state_dir / "previews"
        self.db_path = self.state_dir / "materials.db"
        self._lock = threading.RLock()
        self._rescan_pending = False
        self._preview_lock_guard = threading.Lock()
        self._preview_locks: dict[str, threading.Lock] = {}
        self._preview_semaphore = threading.BoundedSemaphore(1)
        self._scan_status: dict[str, Any] = {
            "running": False,
            "started_at": None,
            "finished_at": None,
            "scanned_files": 0,
            "indexed_items": 0,
            "current": "",
            "error": None,
        }
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS materials (
                    id TEXT PRIMARY KEY,
                    device TEXT NOT NULL,
                    source_label TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    full_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    mtime_text TEXT NOT NULL,
                    mode TEXT,
                    frame_type TEXT,
                    target TEXT,
                    captured_at TEXT,
                    exposure TEXT,
                    exposure_seconds REAL,
                    bin INTEGER,
                    gain INTEGER,
                    temperature_c REAL,
                    preview_path TEXT,
                    preview_status TEXT NOT NULL DEFAULT 'missing',
                    preview_bytes INTEGER,
                    preview_generated_at TEXT,
                    preview_error TEXT,
                    indexed_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "preview_path", "TEXT")
            self._ensure_column(conn, "preview_status", "TEXT NOT NULL DEFAULT 'missing'")
            self._ensure_column(conn, "preview_bytes", "INTEGER")
            self._ensure_column(conn, "preview_generated_at", "TEXT")
            self._ensure_column(conn, "preview_error", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_device ON materials(device)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_source ON materials(source_label)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_target ON materials(target)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_frame ON materials(frame_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_mtime ON materials(mtime DESC)")

    def _ensure_column(self, conn: sqlite3.Connection, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(materials)")}
        if column not in columns:
            conn.execute(f"ALTER TABLE materials ADD COLUMN {column} {definition}")

    def scan_status(self) -> dict[str, Any]:
        with self._lock:
            status = dict(self._scan_status)
        status["db_path"] = str(self.db_path)
        status["db_path_display"] = self.config.display_path(self.db_path)
        status["library_root"] = str(self.root)
        status["library_root_display"] = self.config.display_path(self.root)
        status["preview_dir"] = str(self.preview_dir)
        status["preview_dir_display"] = self.config.display_path(self.preview_dir)
        return status

    def start_scan(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._scan_status.get("running"):
                # Queue one follow-up pass instead of dropping the request —
                # the post-backup trigger must not be lost to an in-flight scan.
                self._rescan_pending = True
                status = self.scan_status()
                status["queued"] = True
                return status
            self._scan_status = {
                "running": True,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "scanned_files": 0,
                "indexed_items": 0,
                "current": "",
                "error": None,
                "force": force,
            }
        thread = threading.Thread(target=self._scan_worker, name="materials-scan", daemon=True)
        thread.start()
        return self.scan_status()

    def _scan_worker(self) -> None:
        seen: set[str] = set()
        indexed = 0
        scanned = 0
        scan_started = datetime.now().isoformat(timespec="seconds")
        try:
            # Sweep preview temp files orphaned by a crash mid-generation.
            try:
                for stale in self.preview_dir.rglob("*.tmp.jpg"):
                    stale.unlink()
            except OSError:
                pass
            self._init_db()
            if not self.root.exists():
                raise FileNotFoundError(str(self.root))
            with self._connect() as conn:
                conn.execute("BEGIN")
                for path in self._iter_material_files():
                    scanned += 1
                    if scanned % 100 == 0:
                        self._update_scan_progress(scanned=scanned, indexed=indexed, current=str(path))
                    record = self._record_for_path(path)
                    if record is None:
                        continue
                    seen.add(record.id)
                    indexed += 1
                    self._upsert_record(conn, record)
                    if indexed % 500 == 0:
                        conn.commit()
                        conn.execute("BEGIN")
                if scanned == 0:
                    # An empty walk (root unreachable / transient FS error)
                    # must not wipe a previously healthy index.
                    raise FileNotFoundError(f"scan found no files under {self.root}")
                # Watermark purge: every row seen this scan was upserted with a
                # fresh indexed_at >= scan_started; older rows are gone from
                # disk. Avoids the ~32k SQLite bound-variable limit of NOT IN.
                conn.execute("DELETE FROM materials WHERE indexed_at < ?", (scan_started,))
                conn.commit()
            self._finish_scan(scanned=scanned, indexed=indexed, error=None)
        except Exception as exc:  # noqa: BLE001
            self._finish_scan(scanned=scanned, indexed=indexed, error=str(exc))
        with self._lock:
            pending = self._rescan_pending
            self._rescan_pending = False
        if pending:
            self.start_scan(force=True)

    def _iter_material_files(self) -> Any:
        for root, dirs, files in os.walk(self.root):
            dirs[:] = [name for name in dirs if not _is_hidden_or_system(name)]
            for name in files:
                path = Path(root) / name
                suffix = path.suffix.lower()
                if suffix not in IMAGE_EXTENSIONS:
                    continue
                if suffix in {".jpg", ".jpeg"} and path.stem.lower().endswith(THUMB_SUFFIX):
                    continue
                yield path

    def _record_for_path(self, path: Path) -> MaterialRecord | None:
        try:
            relative = path.relative_to(self.root)
            stat = path.stat()
        except (OSError, ValueError):
            return None

        parts = relative.parts
        if len(parts) < 3:
            return None

        device = parts[0]
        source_label = parts[1]
        mode = parts[2] if len(parts) >= 3 else None
        frame_type = parts[3] if len(parts) >= 4 else None
        target = parts[4] if len(parts) >= 5 else None
        extension = path.suffix.lower()
        item_id = _stable_id(relative.as_posix())
        preview_path = self._preview_path_for(item_id) if extension in RAW_EXTENSIONS else path
        preview_status = "ready" if preview_path.is_file() else "missing"
        preview_bytes = preview_path.stat().st_size if preview_path.is_file() else None

        metadata = _metadata_from_filename(path.name)
        return MaterialRecord(
            id=item_id,
            device=device,
            source_label=source_label,
            relative_path=relative.as_posix(),
            full_path=str(path),
            file_name=path.name,
            extension=extension,
            size_bytes=int(stat.st_size),
            mtime=float(stat.st_mtime),
            mtime_text=_format_timestamp(stat.st_mtime),
            mode=mode,
            frame_type=frame_type,
            target=target,
            captured_at=metadata.get("captured_at"),
            exposure=metadata.get("exposure"),
            exposure_seconds=metadata.get("exposure_seconds"),
            bin=metadata.get("bin"),
            gain=metadata.get("gain"),
            temperature_c=metadata.get("temperature_c"),
            preview_path=str(preview_path),
            preview_status=preview_status,
            preview_bytes=preview_bytes,
            preview_generated_at=None,
            preview_error=None,
        )

    def _preview_path_for(self, item_id: str) -> Path:
        return self.preview_dir / item_id[:2] / f"{item_id}.jpg"

    def _upsert_record(self, conn: sqlite3.Connection, record: MaterialRecord) -> None:
        conn.execute(
            """
            INSERT INTO materials (
                id, device, source_label, relative_path, full_path, file_name, extension,
                size_bytes, mtime, mtime_text, mode, frame_type, target, captured_at,
                exposure, exposure_seconds, bin, gain, temperature_c, preview_path,
                preview_status, preview_bytes, preview_generated_at, preview_error, indexed_at
            ) VALUES (
                :id, :device, :source_label, :relative_path, :full_path, :file_name, :extension,
                :size_bytes, :mtime, :mtime_text, :mode, :frame_type, :target, :captured_at,
                :exposure, :exposure_seconds, :bin, :gain, :temperature_c, :preview_path,
                :preview_status, :preview_bytes, :preview_generated_at, :preview_error, :indexed_at
            )
            ON CONFLICT(id) DO UPDATE SET
                device=excluded.device,
                source_label=excluded.source_label,
                relative_path=excluded.relative_path,
                full_path=excluded.full_path,
                file_name=excluded.file_name,
                extension=excluded.extension,
                size_bytes=excluded.size_bytes,
                mtime=excluded.mtime,
                mtime_text=excluded.mtime_text,
                mode=excluded.mode,
                frame_type=excluded.frame_type,
                target=excluded.target,
                captured_at=excluded.captured_at,
                exposure=excluded.exposure,
                exposure_seconds=excluded.exposure_seconds,
                bin=excluded.bin,
                gain=excluded.gain,
                temperature_c=excluded.temperature_c,
                preview_path=excluded.preview_path,
                preview_status=CASE
                    WHEN materials.size_bytes != excluded.size_bytes OR materials.mtime != excluded.mtime
                    THEN excluded.preview_status
                    WHEN materials.preview_status IS NULL
                    THEN excluded.preview_status
                    ELSE materials.preview_status
                END,
                preview_bytes=CASE
                    WHEN materials.size_bytes != excluded.size_bytes OR materials.mtime != excluded.mtime
                    THEN excluded.preview_bytes
                    ELSE COALESCE(materials.preview_bytes, excluded.preview_bytes)
                END,
                preview_generated_at=CASE
                    WHEN materials.size_bytes != excluded.size_bytes OR materials.mtime != excluded.mtime
                    THEN excluded.preview_generated_at
                    ELSE materials.preview_generated_at
                END,
                preview_error=CASE
                    WHEN materials.size_bytes != excluded.size_bytes OR materials.mtime != excluded.mtime
                    THEN excluded.preview_error
                    ELSE materials.preview_error
                END,
                indexed_at=excluded.indexed_at
            """,
            {**record.__dict__, "indexed_at": datetime.now().isoformat(timespec="seconds")},
        )

    def _update_scan_progress(self, scanned: int, indexed: int, current: str) -> None:
        with self._lock:
            self._scan_status.update(
                {
                    "scanned_files": scanned,
                    "indexed_items": indexed,
                    "current": current,
                }
            )

    def _finish_scan(self, scanned: int, indexed: int, error: str | None) -> None:
        with self._lock:
            self._scan_status.update(
                {
                    "running": False,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "scanned_files": scanned,
                    "indexed_items": indexed,
                    "current": "",
                    "error": error,
                }
            )

    def list_materials(
        self,
        device: str | None = None,
        source_label: str | None = None,
        mode: str | None = None,
        frame_type: str | None = None,
        target: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 36,
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = max(1, min(80, int(page_size or 36)))
        where: list[str] = []
        params: list[Any] = []
        _add_filter(where, params, "device", device)
        _add_filter(where, params, "source_label", source_label)
        _add_filter(where, params, "mode", mode)
        _add_filter(where, params, "frame_type", frame_type)
        _add_filter(where, params, "target", target)
        if q:
            where.append("(file_name LIKE ? OR target LIKE ? OR relative_path LIKE ?)")
            pattern = f"%{q}%"
            params.extend([pattern, pattern, pattern])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        offset = (page - 1) * page_size
        with self._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM materials {clause}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT * FROM materials
                {clause}
                ORDER BY COALESCE(captured_at, mtime_text) DESC, mtime DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        items = [_row_to_item(row) for row in rows]
        return {
            "ok": True,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "library_root": str(self.root),
            "library_root_display": self.config.display_path(self.root),
            "page": page,
            "page_size": page_size,
            "total": total,
            "items": items,
            "scan": self.scan_status(),
        }

    def browse(
        self,
        device: str,
        source_label: str,
        relative_path: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 80,
    ) -> dict[str, Any]:
        device = str(device or "").strip()
        source_label = str(source_label or "").strip()
        if not device:
            raise ValueError("device is required")
        if not source_label:
            raise ValueError("source is required")
        folder_path = _clean_folder_path(relative_path)
        folder_parts = tuple(part for part in folder_path.split("/") if part)
        page = max(1, int(page or 1))
        page_size = max(1, min(200, int(page_size or 80)))
        pattern = str(q or "").strip().lower()

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM materials
                WHERE device=? AND source_label=?
                ORDER BY COALESCE(captured_at, mtime_text) DESC, mtime DESC
                """,
                [device, source_label],
            ).fetchall()

        folders: dict[str, dict[str, Any]] = {}
        images: list[dict[str, Any]] = []
        prefix_len = 2 + len(folder_parts)
        for row in rows:
            parts = tuple(str(row["relative_path"]).split("/"))
            if len(parts) <= prefix_len:
                continue
            if tuple(parts[0:2]) != (device, source_label):
                continue
            if folder_parts and tuple(parts[2 : 2 + len(folder_parts)]) != folder_parts:
                continue
            tail = parts[prefix_len:]
            if not tail:
                continue
            if len(tail) > 1:
                folder_name = tail[0]
                if pattern and pattern not in "/".join(tail).lower():
                    pass
                entry = folders.setdefault(
                    folder_name,
                    {
                        "name": folder_name,
                        "path": _join_folder_path(folder_path, folder_name),
                        "item_count": 0,
                        "cover_thumb_url": None,
                    },
                )
                entry["item_count"] += 1
                if entry["cover_thumb_url"] is None and self._thumbnail_path_for_row(row) is not None:
                    entry["cover_thumb_url"] = f"/api/materials/thumb?id={row['id']}"
                continue

            item = _row_to_item(row)
            item["thumb_url"] = f"/api/materials/thumb?id={row['id']}" if self._thumbnail_path_for_row(row) else None
            if pattern:
                haystack = " ".join(
                    str(item.get(key) or "")
                    for key in ("file_name", "target", "relative_path", "captured_at", "exposure")
                ).lower()
                if pattern not in haystack:
                    continue
            images.append(item)

        sorted_folders = sorted(folders.values(), key=lambda item: str(item["name"]).lower())
        total_images = len(images)
        offset = (page - 1) * page_size
        paged_images = images[offset : offset + page_size]
        return {
            "ok": True,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "library_root": str(self.root),
            "library_root_display": self.config.display_path(self.root),
            "device": device,
            "source": source_label,
            "path": folder_path,
            "path_parts": list(folder_parts),
            "breadcrumbs": _breadcrumbs(device, source_label, folder_parts),
            "folders": sorted_folders,
            "images": paged_images,
            "image_total": total_images,
            "folder_total": len(sorted_folders),
            "page": page,
            "page_size": page_size,
            "scan": self.scan_status(),
        }

    def summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0])
            preview_ready = int(
                conn.execute("SELECT COUNT(*) FROM materials WHERE preview_status='ready'").fetchone()[0]
            )
            preview_failed = int(
                conn.execute("SELECT COUNT(*) FROM materials WHERE preview_status='failed'").fetchone()[0]
            )
            devices = _group_count(conn, "device")
            sources = _group_count(conn, "source_label")
            modes = _group_count(conn, "mode")
            frame_types = _group_count(conn, "frame_type")
            targets = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT target AS value, COUNT(*) AS count
                    FROM materials
                    WHERE target IS NOT NULL AND target != ''
                    GROUP BY target
                    ORDER BY count DESC, target
                    LIMIT 200
                    """
                )
            ]
            latest = [
                _row_to_item(row)
                for row in conn.execute("SELECT * FROM materials ORDER BY mtime DESC LIMIT 12").fetchall()
            ]
        return {
            "ok": True,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "library_root": str(self.root),
            "library_root_display": self.config.display_path(self.root),
            "db_path": str(self.db_path),
            "db_path_display": self.config.display_path(self.db_path),
            "preview_dir": str(self.preview_dir),
            "preview_dir_display": self.config.display_path(self.preview_dir),
            "total": total,
            "preview_ready": preview_ready,
            "preview_failed": preview_failed,
            "devices": devices,
            "sources": sources,
            "modes": modes,
            "frame_types": frame_types,
            "targets": targets,
            "latest": latest,
            "scan": self.scan_status(),
        }

    def ensure_preview(self, item_id: str, force: bool = False) -> dict[str, Any]:
        item_id = str(item_id or "").strip()
        if not item_id:
            raise ValueError("id is required")
        lock = self._lock_for_item(item_id)
        with lock:
            row = self._get_row(item_id)
            if row is None:
                raise FileNotFoundError(item_id)
            source_path = Path(str(row["full_path"]))
            if not source_path.is_file():
                self._set_preview_failed(item_id, f"source missing: {source_path}")
                raise FileNotFoundError(str(source_path))
            extension = str(row["extension"]).lower()
            if extension not in RAW_EXTENSIONS:
                return self._ready_preview_payload(row, source_path, "image/jpeg" if extension in {".jpg", ".jpeg"} else "image/png")

            preview_path = Path(str(row["preview_path"] or self._preview_path_for(item_id)))
            if preview_path.is_file() and not force:
                self._set_preview_ready(item_id, preview_path, None, None)
                return self._ready_preview_payload(row, preview_path, "image/jpeg")

            with self._preview_semaphore:
                row = self._get_row(item_id)
                if row is None:
                    raise FileNotFoundError(item_id)
                preview_path = Path(str(row["preview_path"] or self._preview_path_for(item_id)))
                if preview_path.is_file() and not force:
                    self._set_preview_ready(item_id, preview_path, None, None)
                    return self._ready_preview_payload(row, preview_path, "image/jpeg")
                self._set_preview_building(item_id)
                started = time.monotonic()
                try:
                    meta = generate_stf_preview(source_path, preview_path)
                    self._set_preview_ready(item_id, preview_path, meta, time.monotonic() - started)
                    return {
                        "ok": True,
                        "id": item_id,
                        "path": preview_path,
                        "content_type": "image/jpeg",
                        "generated": True,
                        "meta": meta,
                    }
                except Exception as exc:  # noqa: BLE001
                    self._set_preview_failed(item_id, str(exc))
                    raise

    def preview_status(self, item_id: str) -> dict[str, Any]:
        row = self._get_row(item_id)
        if row is None:
            raise FileNotFoundError(item_id)
        return {"ok": True, "item": _row_to_item(row)}

    def thumbnail_path(self, item_id: str) -> Path | None:
        row = self._get_row(str(item_id or "").strip())
        if row is None:
            return None
        return self._thumbnail_path_for_row(row)

    def _lock_for_item(self, item_id: str) -> threading.Lock:
        with self._preview_lock_guard:
            lock = self._preview_locks.get(item_id)
            if lock is None:
                lock = threading.Lock()
                self._preview_locks[item_id] = lock
            return lock

    def _get_row(self, item_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM materials WHERE id=?", [item_id]).fetchone()

    def _set_preview_building(self, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE materials
                SET preview_status='building', preview_error=NULL
                WHERE id=?
                """,
                [item_id],
            )

    def _set_preview_ready(
        self,
        item_id: str,
        path: Path,
        meta: dict[str, Any] | None,
        elapsed_seconds: float | None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        size = path.stat().st_size if path.is_file() else None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE materials
                SET preview_path=?, preview_status='ready', preview_bytes=?,
                    preview_generated_at=COALESCE(?, preview_generated_at),
                    preview_error=NULL
                WHERE id=?
                """,
                [str(path), size, now if meta is not None or elapsed_seconds is not None else None, item_id],
            )

    def _set_preview_failed(self, item_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE materials
                SET preview_status='failed', preview_error=?
                WHERE id=?
                """,
                [error[:1000], item_id],
            )

    def _ready_preview_payload(self, row: sqlite3.Row, path: Path, content_type: str) -> dict[str, Any]:
        return {
            "ok": True,
            "id": row["id"],
            "path": path,
            "content_type": content_type,
            "generated": False,
        }

    def _thumbnail_path_for_row(self, row: sqlite3.Row) -> Path | None:
        source_path = Path(str(row["full_path"]))
        extension = str(row["extension"] or "").lower()
        if extension in RAW_EXTENSIONS:
            candidate = source_path.with_name(f"{source_path.stem}{THUMB_SUFFIX}.jpg")
        elif extension in {".jpg", ".jpeg", ".png"}:
            candidate = source_path
        else:
            return None
        if not candidate.is_file():
            return None
        try:
            candidate.resolve().relative_to(self.root.resolve())
        except ValueError:
            return None
        return candidate


def generate_stf_preview(source_path: Path, output_path: Path) -> dict[str, Any]:
    header, data = read_fits_image(source_path)
    stretched, stretch_meta = stf_stretch_to_uint8(data)
    return save_preview_jpeg(stretched, output_path, header, stretch_meta)


def read_fits_image(path: Path) -> tuple[dict[str, Any], Any]:
    import numpy as np

    cards: list[str] = []
    with path.open("rb") as fh:
        while True:
            block = fh.read(2880)
            if not block:
                raise ValueError("FITS header missing END")
            block_has_end = False
            for index in range(0, len(block), 80):
                card = block[index : index + 80].decode("ascii", errors="replace")
                cards.append(card)
                if card.startswith("END"):
                    block_has_end = True
                    break
            if block_has_end:
                break

        header = _parse_fits_cards(cards)
        bitpix = int(header.get("BITPIX", 0))
        axis_count = int(header.get("NAXIS", 0))
        if axis_count < 2:
            raise ValueError("FITS image must have at least 2 axes")
        axes = [int(header.get(f"NAXIS{index}", 0)) for index in range(1, axis_count + 1)]
        if axes[0] <= 0 or axes[1] <= 0:
            raise ValueError("FITS image has invalid dimensions")

        dtype = _fits_dtype(bitpix)
        count = math.prod(axes)
        raw = fh.read(count * abs(bitpix) // 8)
        if len(raw) < count * abs(bitpix) // 8:
            raise ValueError("FITS data is shorter than expected")
        array = np.frombuffer(raw, dtype=dtype, count=count).reshape(tuple(reversed(axes)))
        if array.ndim > 2:
            array = array[0]
        bscale = float(header.get("BSCALE", 1) or 1)
        bzero = float(header.get("BZERO", 0) or 0)
        data = array.astype(np.float32) * bscale + bzero
        header["width"] = int(axes[0])
        header["height"] = int(axes[1])
        return header, data


def _fits_dtype(bitpix: int) -> str:
    if bitpix == 8:
        return ">u1"
    if bitpix == 16:
        return ">i2"
    if bitpix == 32:
        return ">i4"
    if bitpix == -32:
        return ">f4"
    if bitpix == -64:
        return ">f8"
    raise ValueError(f"Unsupported FITS BITPIX: {bitpix}")


def stf_stretch_to_uint8(data: Any) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    finite = data[np.isfinite(data)]
    if finite.size == 0:
        raise ValueError("FITS image has no finite pixels")

    sample_step = max(1, int(math.sqrt(finite.size / 1_200_000)))
    sample = data[::sample_step, ::sample_step]
    sample = sample[np.isfinite(sample)].astype(np.float64)
    fill_value = _dominant_fill_value(sample)
    if fill_value is not None:
        clipped_sample = sample[sample != fill_value]
        if clipped_sample.size > max(1000, int(sample.size * 0.05)):
            sample = clipped_sample
    median = float(np.median(sample))
    mad = float(np.median(np.abs(sample - median)))
    sigma = 1.4826 * mad if mad > 0 else float(np.std(sample))
    if sigma <= 0:
        black = float(np.quantile(sample, 0.001))
    else:
        black = max(float(np.min(sample)), median - 2.8 * sigma)
    white = float(np.quantile(sample, 0.9999))
    if white <= black + 1:
        white = float(np.max(sample))
    if white <= black + 1:
        white = black + 1
    normalized_median = min(max((median - black) / (white - black), 1e-6), 1 - 1e-6)
    midtone = _midtone_transfer_value(normalized_median, 0.25)

    values = np.clip((data.astype(np.float32) - black) / (white - black), 0, 1)
    stretched = _midtone_transfer(values, midtone)
    output = np.rint(np.clip(stretched, 0, 1) * 255).astype(np.uint8)
    if fill_value is not None:
        output[data == fill_value] = 0
    return output, {
        "algorithm": "STF-style median/MAD + midtones transfer",
        "black": black,
        "white": white,
        "median": median,
        "mad": mad,
        "sigma": sigma,
        "midtone": midtone,
        "target_background": 0.25,
        "sample_step": sample_step,
        "fill_value": fill_value,
    }


def _midtone_transfer_value(source_median: float, target_background: float) -> float:
    source = min(max(float(source_median), 1e-6), 1 - 1e-6)
    target = min(max(float(target_background), 1e-6), 1 - 1e-6)
    value = ((target - 1) * source) / (((2 * target - 1) * source) - target)
    return min(max(float(value), 1e-6), 1 - 1e-6)


def _midtone_transfer(values: Any, midtone: float) -> Any:
    import numpy as np

    with np.errstate(divide="ignore", invalid="ignore"):
        denominator = ((2 * midtone - 1) * values) - midtone
        result = ((midtone - 1) * values) / denominator
    return np.where(np.isfinite(result), result, 0)


def _dominant_fill_value(sample: Any) -> float | None:
    import numpy as np

    if sample.size < 1000:
        return None
    values, counts = np.unique(sample, return_counts=True)
    if not len(values):
        return None
    index = int(np.argmax(counts))
    dominant_fraction = float(counts[index]) / float(sample.size)
    dominant_value = float(values[index])
    high_reference = float(np.quantile(sample, 0.90))
    if dominant_fraction >= 0.30 and dominant_value >= high_reference:
        return dominant_value
    return None


def save_preview_jpeg(data: Any, output_path: Path, header: dict[str, Any], stretch_meta: dict[str, Any]) -> dict[str, Any]:
    from PIL import Image

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.fromarray(data, mode="L")
    tmp_path = output_path.with_suffix(".tmp.jpg")
    image.save(tmp_path, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    selected_size = tmp_path.stat().st_size
    tmp_path.replace(output_path)
    return {
        "width": int(header.get("width") or data.shape[1]),
        "height": int(header.get("height") or data.shape[0]),
        "bytes": selected_size,
        "jpeg_quality": JPEG_QUALITY,
        "stretch": stretch_meta,
    }


def _parse_fits_cards(cards: list[str]) -> dict[str, Any]:
    header: dict[str, Any] = {}
    for card in cards:
        if card.startswith("END"):
            break
        if "=" not in card[:10]:
            continue
        key = card[:8].strip()
        value_text = card[10:80].split("/", 1)[0].strip()
        header[key] = _parse_fits_value(value_text)
    return header


def _parse_fits_value(value_text: str) -> Any:
    if not value_text:
        return None
    if value_text.startswith("'"):
        end = value_text.find("'", 1)
        return value_text[1:end].strip() if end > 0 else value_text.strip("' ")
    if value_text in {"T", "F"}:
        return value_text == "T"
    try:
        if any(mark in value_text.upper() for mark in (".", "E")):
            return float(value_text)
        return int(value_text)
    except ValueError:
        return value_text


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["preview_url"] = f"/api/materials/preview?id={item['id']}"
    item["preview_status_url"] = f"/api/materials/preview-status?id={item['id']}"
    return item


def _clean_folder_path(value: str | None) -> str:
    text = str(value or "").replace("\\", "/").strip("/")
    if not text:
        return ""
    parts = [part for part in text.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("invalid material path")
    return "/".join(parts)


def _join_folder_path(current: str, child: str) -> str:
    return "/".join(part for part in (current.strip("/"), child.strip("/")) if part)


def _breadcrumbs(device: str, source_label: str, folder_parts: tuple[str, ...]) -> list[dict[str, str]]:
    crumbs = [
        {"label": "素材库", "path": ""},
        {"label": device, "path": ""},
        {"label": source_label, "path": ""},
    ]
    parts: list[str] = []
    for part in folder_parts:
        parts.append(part)
        crumbs.append({"label": part, "path": "/".join(parts)})
    return crumbs


def _group_count(conn: sqlite3.Connection, column: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT {column} AS value, COUNT(*) AS count
            FROM materials
            WHERE {column} IS NOT NULL AND {column} != ''
            GROUP BY {column}
            ORDER BY count DESC, {column}
            """
        )
    ]


def _add_filter(where: list[str], params: list[Any], column: str, value: str | None) -> None:
    if value:
        where.append(f"{column}=?")
        params.append(value)


def _is_hidden_or_system(name: str) -> bool:
    return name in {
        "@eaDir",
        "#recycle",
        "$RECYCLE.BIN",
        "System Volume Information",
        ".Spotlight-V100",
        ".Trashes",
        ".fseventsd",
    }


def _stable_id(relative_path: str) -> str:
    return hashlib.sha1(relative_path.replace("\\", "/").lower().encode("utf-8")).hexdigest()[:24]


def _metadata_from_filename(name: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    captured_match = CAPTURED_AT_RE.search(name)
    if captured_match:
        raw = captured_match.group(1)
        try:
            metadata["captured_at"] = datetime.strptime(raw, "%Y%m%d-%H%M%S").isoformat(timespec="seconds")
        except ValueError:
            metadata["captured_at"] = raw
    exposure_match = EXPOSURE_RE.search(name)
    if exposure_match:
        value = float(exposure_match.group(1))
        unit = exposure_match.group(2).lower()
        seconds = value / 1000.0 if unit == "ms" else value
        metadata["exposure"] = f"{value:g}{unit}"
        metadata["exposure_seconds"] = seconds
        metadata["bin"] = int(exposure_match.group(3))
    gain_match = GAIN_RE.search(name)
    if gain_match:
        metadata["gain"] = int(gain_match.group(1))
    temp_match = TEMP_RE.search(name)
    if temp_match:
        metadata["temperature_c"] = float(temp_match.group(1))
    return metadata


def _format_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")
