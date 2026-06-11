from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .camera_cache import CameraStateCache
from .camera_ops import camera_action_response, camera_status_response
from .config import AppConfig, load_config
from .image_preview import cached_image_path, cached_raw_path, current_image_response
from .materials import MaterialLibrary
from .monitor import dashboard_snapshot, read_log_tail, read_lock, scan_source_totals
from .rpc_monitor import RPC_MONITOR_HTML, init_rpc_monitor_state, rpc_monitor_response
from .web_control import ControlLeaseBusyError, control_state, update_control_role

DASHBOARD_SOURCE_LABEL = "EMMC Images"


def run_server(
    config_path: str,
    host: str = "127.0.0.1",
    port: int = 8787,
    allow_remote_actions: bool = False,
    read_only: bool = False,
) -> None:
    config = load_config(config_path)
    server = AsiairBridgeServer(
        (host, port),
        DashboardHandler,
        config,
        allow_remote_actions=allow_remote_actions,
        read_only=read_only,
    )
    print(f"asiairbridge web listening on http://{host}:{port}")
    server.serve_forever()


class AsiairBridgeServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,  # type: ignore[no-untyped-def]
        handler_class,
        config: AppConfig,
        allow_remote_actions: bool = False,
        read_only: bool = False,
    ):
        super().__init__(server_address, handler_class)
        self.config = config
        self.allow_remote_actions = allow_remote_actions
        self.read_only = read_only
        self.camera_operations: dict[str, dict[str, Any]] = {}
        self.camera_operations_lock = threading.Lock()
        self.camera_cache = CameraStateCache(config)
        self.camera_cache.start()
        self.materials = MaterialLibrary(config)
        init_rpc_monitor_state(self)

    def server_close(self) -> None:
        self.camera_cache.stop()
        super().server_close()


def devices_payload(config: AppConfig) -> dict[str, Any]:
    default_device = config.default_device()
    devices = []
    for device in config.enabled_devices():
        devices.append(
            {
                "name": device.name,
                "ip": device.ip,
                "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
                "enabled": device.enabled,
                "is_default": device.name == default_device.name,
                "source_roots": [
                    {
                        "label": source.label,
                        "enabled": source.enabled,
                    }
                    for source in config.source_roots_for(device)
                ],
            }
        )
    return {
        "ok": True,
        "default_device": default_device.name,
        "devices": devices,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server: AsiairBridgeServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send_html(INDEX_HTML)
            elif parsed.path == "/monitor":
                self._send_html(RPC_MONITOR_HTML)
            elif parsed.path == "/monitor-minterm":
                self._send_file(
                    self.server.config.root / "docs" / "asiair-monitor-minterm-live.html",
                    "text/html; charset=utf-8",
                )
            elif parsed.path in {"/preview", "/camera"}:
                self._send_file(
                    self.server.config.root / "docs" / "asiair-image-preview.html",
                    "text/html; charset=utf-8",
                )
            elif parsed.path in {"/materials", "/library"}:
                self._send_file(
                    self.server.config.root / "docs" / "asiair-materials.html",
                    "text/html; charset=utf-8",
                )
            elif parsed.path == "/static/asiair-monitor-minterm-live.html":
                self._send_file(
                    self.server.config.root / "docs" / "asiair-monitor-minterm-live.html",
                    "text/html; charset=utf-8",
                )
            elif parsed.path == "/drop/asiair-monitor-minterm-live.html":
                self._send_file(
                    self.server.config.root / "docs" / "asiair-monitor-minterm-live.html",
                    "text/html; charset=utf-8",
                    download_name="asiair-monitor-minterm-live.html",
                )
            elif parsed.path == "/api/status":
                payload = dashboard_snapshot(self.server.config, source_label=DASHBOARD_SOURCE_LABEL)
                payload["dashboard_source_label"] = DASHBOARD_SOURCE_LABEL
                payload["web"] = {
                    "client": self.client_address[0],
                    "actions_allowed": self._actions_allowed(),
                    "scan_allowed": self._scan_allowed(),
                    "allow_remote_actions": self.server.allow_remote_actions,
                    "read_only": self.server.read_only,
                }
                self._send_json(payload)
            elif parsed.path == "/api/devices":
                self._send_json(devices_payload(self.server.config))
            elif parsed.path == "/api/log":
                query = parse_qs(parsed.query)
                path = query.get("path", [""])[0]
                self._send_json(read_log_tail(self.server.config, path))
            elif parsed.path == "/api/rpc-monitor":
                query = parse_qs(parsed.query)
                device = query.get("device", [None])[0]
                category = query.get("category", [None])[0]
                force = query.get("force", ["0"])[0] in {"1", "true", "yes"}
                self._send_json(
                    rpc_monitor_response(
                        self.server,
                        device_name=device,
                        force=force,
                        focus_category=category,
                    )
                )
            elif parsed.path == "/api/current-image":
                query = parse_qs(parsed.query)
                device = query.get("device", [None])[0]
                force = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
                self._send_json(current_image_response(self.server.config, device, force=force))
            elif parsed.path == "/api/current-image-file":
                query = parse_qs(parsed.query)
                device = query.get("device", [None])[0]
                self._send_file(cached_image_path(self.server.config, device), "image/png")
            elif parsed.path == "/api/current-image-raw":
                query = parse_qs(parsed.query)
                device = query.get("device", [None])[0]
                self._send_file(
                    cached_raw_path(self.server.config, device),
                    "application/octet-stream",
                )
            elif parsed.path == "/api/materials/summary":
                self._send_json(self.server.materials.summary())
            elif parsed.path == "/api/materials/browse":
                query = parse_qs(parsed.query)
                self._send_json(
                    self.server.materials.browse(
                        device=query.get("device", [""])[0],
                        source_label=query.get("source", [""])[0],
                        relative_path=query.get("path", [""])[0],
                        q=query.get("q", [None])[0],
                        page=int(query.get("page", ["1"])[0] or 1),
                        page_size=int(query.get("page_size", ["80"])[0] or 80),
                    )
                )
            elif parsed.path == "/api/materials":
                query = parse_qs(parsed.query)
                self._send_json(
                    self.server.materials.list_materials(
                        device=query.get("device", [None])[0],
                        source_label=query.get("source", [None])[0],
                        mode=query.get("mode", [None])[0],
                        frame_type=query.get("frame_type", [None])[0],
                        target=query.get("target", [None])[0],
                        q=query.get("q", [None])[0],
                        page=int(query.get("page", ["1"])[0] or 1),
                        page_size=int(query.get("page_size", ["12"])[0] or 12),
                    )
                )
            elif parsed.path == "/api/materials/preview":
                query = parse_qs(parsed.query)
                item_id = query.get("id", [""])[0]
                force = query.get("force", ["0"])[0] in {"1", "true", "yes"}
                preview = self.server.materials.ensure_preview(item_id, force=force)
                self._send_file(Path(preview["path"]), str(preview["content_type"]))
            elif parsed.path == "/api/materials/thumb":
                query = parse_qs(parsed.query)
                item_id = query.get("id", [""])[0]
                path = self.server.materials.thumbnail_path(item_id)
                if path is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    content_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
                    self._send_file(path, content_type)
            elif parsed.path == "/api/materials/preview-status":
                query = parse_qs(parsed.query)
                item_id = query.get("id", [""])[0]
                self._send_json(self.server.materials.preview_status(item_id))
            elif parsed.path == "/api/control-role":
                query = parse_qs(parsed.query)
                device = query.get("device", [None])[0]
                session_id = query.get("session_id", [""])[0]
                if not device:
                    raise ValueError("device is required")
                self._send_json(control_state(self.server.config, device, session_id=session_id))
            elif parsed.path == "/api/camera-state":
                query = parse_qs(parsed.query)
                device = query.get("device", [None])[0]
                session_id = query.get("session_id", [""])[0]
                live = query.get("live", ["0"])[0] in {"1", "true", "yes"}
                if live:
                    payload = camera_status_response(
                        self.server.config,
                        device_name=device,
                        session_id=session_id,
                        rpc_timeout_seconds=2.5,
                        queue_timeout_seconds=4.0,
                        status_budget_seconds=10.0,
                        priority="foreground",
                    )
                    self.server.camera_cache.store(payload)
                    payload["cache"] = {
                        "from_cache": False,
                        "updated_at": payload.get("snapshot_at"),
                        "age_seconds": 0,
                        "status": "live",
                    }
                    self._send_json(payload)
                else:
                    self._send_json(self.server.camera_cache.get(device, session_id=session_id))
            elif parsed.path == "/api/camera-states":
                query = parse_qs(parsed.query)
                session_id = query.get("session_id", [""])[0]
                self._send_json(self.server.camera_cache.get_all(session_id=session_id))
            elif parsed.path == "/api/camera-operation":
                query = parse_qs(parsed.query)
                device = query.get("device", [""])[0]
                session_id = query.get("session_id", [""])[0]
                operation_id = query.get("operation_id", [""])[0]
                self._send_json(self._camera_operation_state(device, session_id, operation_id))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json_body()
            if parsed.path == "/api/start":
                self._require_actions_allowed()
                payload["source_label"] = DASHBOARD_SOURCE_LABEL
                self._send_json(start_backup(self.server.config, payload))
            elif parsed.path == "/api/scan-source":
                self._require_scan_allowed()
                device = payload.get("device")
                self._send_json(
                    scan_source_totals(
                        self.server.config,
                        [device] if device else None,
                        [DASHBOARD_SOURCE_LABEL],
                    )
                )
            elif parsed.path == "/api/materials/scan":
                self._send_json(self.server.materials.start_scan(force=bool(payload.get("force"))))
            elif parsed.path == "/api/control-role":
                device = str(payload.get("device") or "").strip()
                session_id = str(payload.get("session_id") or "").strip()
                role = str(payload.get("role") or "").strip().lower()
                session_label = str(payload.get("session_label") or "").strip() or None
                if not device:
                    raise ValueError("device is required")
                self._send_json(
                    update_control_role(
                        self.server.config,
                        device_name=device,
                        session_id=session_id,
                        client_ip=self.client_address[0],
                        role=role,
                        session_label=session_label,
                    )
                )
            elif parsed.path == "/api/camera-action":
                device = str(payload.get("device") or "").strip()
                session_id = str(payload.get("session_id") or "").strip()
                action = str(payload.get("action") or "").strip()
                if not device:
                    raise ValueError("device is required")
                if not session_id:
                    raise ValueError("session_id is required")
                operation_id = str(payload.get("operation_id") or "").strip() or (
                    f"{session_id}:{action}:{time.time():.3f}"
                )
                self._update_camera_operation(
                    device,
                    session_id,
                    operation_id,
                    {
                        "step": 0,
                        "total": 1,
                        "label": "准备相机操作",
                        "state": "running",
                        "detail": action,
                    },
                )
                try:
                    self._require_controller_for_device(device, session_id)
                    result = camera_action_response(
                        self.server.config,
                        device_name=device,
                        action=action,
                        payload=payload,
                        session_id=session_id,
                        progress_callback=lambda update: self._update_camera_operation(
                            device,
                            session_id,
                            operation_id,
                            update,
                        ),
                    )
                    self._update_camera_operation(
                        device,
                        session_id,
                        operation_id,
                        {
                            "step": result.get("operation", {}).get("step"),
                            "total": result.get("operation", {}).get("total"),
                            "label": "操作完成",
                            "state": "done",
                        },
                    )
                    self.server.camera_cache.patch_from_action(device, result)
                    self.server.camera_cache.trigger(device)
                    result["operation_id"] = operation_id
                    result["operation"] = self._camera_operation_state(device, session_id, operation_id).get("operation")
                    self._send_json(result)
                except Exception as exc:  # noqa: BLE001
                    self._update_camera_operation(
                        device,
                        session_id,
                        operation_id,
                        {
                            "label": str(exc),
                            "state": "error",
                            "detail": action,
                        },
                    )
                    raise
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except BusyError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
        except ControlLeaseBusyError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.FORBIDDEN)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _actions_allowed(self) -> bool:
        if self.server.read_only:
            return False
        if self.server.allow_remote_actions:
            return True
        return self.client_address[0] in {"127.0.0.1", "::1"}

    def _scan_allowed(self) -> bool:
        return True

    def _require_actions_allowed(self) -> None:
        if not self._actions_allowed():
            raise PermissionError("Remote dashboard access is read-only")

    def _require_scan_allowed(self) -> None:
        if not self._scan_allowed():
            raise PermissionError("Source scanning is disabled")

    def _require_controller_for_device(self, device_name: str, session_id: str) -> None:
        if self.server.read_only:
            raise PermissionError("Web server is running in read-only mode")
        lease = control_state(self.server.config, device_name, session_id=session_id)
        if not lease.get("held_by_self"):
            raise PermissionError(f"{device_name} requires controller mode")

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        text = self.rfile.read(length).decode("utf-8")
        return json.loads(text)

    def _operation_key(self, device_name: str, session_id: str, operation_id: str) -> str:
        return f"{device_name}\0{session_id}\0{operation_id}"

    def _update_camera_operation(
        self,
        device_name: str,
        session_id: str,
        operation_id: str,
        update: dict[str, Any],
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.server.camera_operations_lock:
            key = self._operation_key(device_name, session_id, operation_id)
            current = self.server.camera_operations.get(key, {})
            step = update.get("step")
            total = update.get("total")
            current.update(
                {
                    "device": device_name,
                    "session_id": session_id,
                    "operation_id": operation_id,
                    "updated_at": now,
                    "step": step if step is not None else current.get("step", 0),
                    "total": total if total is not None else current.get("total", 1),
                    "label": update.get("label", current.get("label", "")),
                    "state": update.get("state", current.get("state", "running")),
                    "detail": update.get("detail", current.get("detail")),
                    "writes": update.get("writes", current.get("writes", [])),
                }
            )
            current.setdefault("created_at", now)
            self.server.camera_operations[key] = current

            stale_keys = [
                existing_key
                for existing_key, value in self.server.camera_operations.items()
                if time.time() - _parse_iso_seconds(value.get("updated_at")) > 600
            ]
            for stale_key in stale_keys:
                self.server.camera_operations.pop(stale_key, None)

    def _camera_operation_state(
        self,
        device_name: str,
        session_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        with self.server.camera_operations_lock:
            if operation_id:
                operation = self.server.camera_operations.get(
                    self._operation_key(device_name, session_id, operation_id)
                )
            else:
                operation = None
                prefix = f"{device_name}\0{session_id}\0"
                for key, value in self.server.camera_operations.items():
                    if key.startswith(prefix):
                        if operation is None or str(value.get("updated_at", "")) > str(operation.get("updated_at", "")):
                            operation = value
            return {
                "ok": True,
                "device": device_name,
                "session_id": session_id,
                "operation_id": operation_id,
                "operation": operation,
            }

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(
        self,
        path: Path,
        content_type: str,
        download_name: str | None = None,
    ) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BusyError(RuntimeError):
    pass


def _parse_iso_seconds(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def start_backup(config: AppConfig, payload: dict[str, Any]) -> dict[str, Any]:
    lock = read_lock(config.project.lock_file)
    if lock.get("active") and lock.get("pid_alive") is not False:
        raise BusyError(f"Backup already running, pid={lock.get('pid')}")

    device = payload.get("device")
    source_label = DASHBOARD_SOURCE_LABEL
    dry_run = bool(payload.get("dry_run", True))
    force_lock = bool(payload.get("force_lock", False))

    args = [
        sys.executable,
        "-B",
        "-m",
        "asiairbridge",
        "--config",
        str(config.path),
        "backup",
        "--dry-run" if dry_run else "--no-dry-run",
    ]
    if device:
        args.extend(["--device", str(device)])
    if source_label:
        args.extend(["--source-label", str(source_label)])
    if force_lock:
        args.append("--force-lock")

    process_log_dir = config.state_path() / "web-processes"
    process_log_dir.mkdir(parents=True, exist_ok=True)
    log_path = process_log_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(config.root / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    stdout = log_path.open("a", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            args,
            cwd=config.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    finally:
        stdout.close()

    return {
        "ok": True,
        "pid": proc.pid,
        "dry_run": dry_run,
        "device": device,
        "source_label": source_label,
        "process_log": str(log_path),
    }


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>asiairbridge</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f8;
      --ink: #172026;
      --muted: #66717a;
      --line: #d9dee3;
      --panel: #ffffff;
      --green: #166534;
      --amber: #92400e;
      --red: #b91c1c;
      --blue: #1d4ed8;
      --chip: #eef2f5;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
      font-size: 14px;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 650; letter-spacing: 0; }
    main { padding: 20px 24px 32px; display: grid; gap: 18px; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }
    h2 { margin: 0; font-size: 15px; font-weight: 650; letter-spacing: 0; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    button {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 0 12px;
      font: inherit;
      cursor: pointer;
    }
    button.primary { background: var(--blue); border-color: var(--blue); color: white; }
    button.warn { background: #fff7ed; border-color: #fed7aa; color: var(--amber); }
    button:disabled { color: #9aa3aa; background: #f2f4f6; cursor: not-allowed; }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 12px;
      padding: 16px;
    }
    .metric { border: 1px solid var(--line); border-radius: 8px; padding: 12px; min-height: 78px; }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { margin-top: 8px; font-size: 18px; font-weight: 650; overflow-wrap: anywhere; }
    .ok { color: var(--green); }
    .warn { color: var(--amber); }
    .bad { color: var(--red); }
    .muted { color: var(--muted); }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th { color: var(--muted); font-size: 12px; font-weight: 600; background: #fafbfc; }
    tr:last-child td { border-bottom: 0; }
    .bar { width: 100%; height: 8px; border-radius: 999px; background: #e5e7eb; overflow: hidden; margin-top: 6px; }
    .bar span { display: block; height: 100%; background: var(--green); width: 0%; }
    .chip { display: inline-flex; align-items: center; min-height: 24px; padding: 0 8px; border-radius: 999px; background: var(--chip); color: var(--muted); font-size: 12px; }
    .log-grid { display: grid; grid-template-columns: minmax(260px, 360px) 1fr; gap: 0; }
    .log-list { border-right: 1px solid var(--line); max-height: 460px; overflow: auto; }
    .log-item { display: block; width: 100%; height: auto; text-align: left; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; padding: 10px 12px; }
    pre { margin: 0; padding: 14px; min-height: 320px; max-height: 460px; overflow: auto; white-space: pre-wrap; font-family: Consolas, monospace; font-size: 12px; background: #0f1720; color: #dbe5ef; }
    @media (max-width: 900px) {
      header { align-items: flex-start; flex-direction: column; }
      .summary { grid-template-columns: 1fr; }
      .log-grid { grid-template-columns: 1fr; }
      .log-list { border-right: 0; border-bottom: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <h1>asiairbridge</h1>
    <div class="toolbar">
      <span id="stamp" class="chip">--</span>
      <button id="refresh">刷新</button>
    </div>
  </header>
  <main>
    <section>
      <div class="section-head"><h2>运行状态</h2></div>
      <div id="summary" class="summary"></div>
    </section>

    <section>
      <div class="section-head">
        <h2>设备传输</h2>
        <div id="device-actions" class="toolbar"></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width: 110px;">设备</th>
              <th style="width: 140px;">共享</th>
              <th>目标</th>
              <th style="width: 130px;">已落地</th>
              <th style="width: 150px;">源容量</th>
              <th style="width: 170px;">进度</th>
              <th style="width: 150px;">速度 / ETA</th>
            </tr>
          </thead>
          <tbody id="jobs"></tbody>
        </table>
      </div>
    </section>

    <section>
      <div class="section-head"><h2>日志</h2></div>
      <div class="log-grid">
        <div id="logs" class="log-list"></div>
        <pre id="log-text">选择一个日志。</pre>
      </div>
    </section>
  </main>
  <script>
    let snapshot = null;
    const LOW_SPEED_BPS = 256 * 1024;

    const $ = (id) => document.getElementById(id);
    const fmtBytes = (n) => {
      if (n === null || n === undefined) return "--";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let value = Number(n);
      let i = 0;
      while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
      return `${value.toFixed(i < 2 ? 0 : 2)} ${units[i]}`;
    };
    const fmtDuration = (seconds) => {
      if (!seconds || seconds < 0) return "--";
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      if (h > 0) return `${h}h ${m}m`;
      return `${m}m`;
    };
    const fmtSpeed = (bps) => (bps === null || bps === undefined) ? "--" : `${fmtBytes(bps)}/s`;
    const pct = (value) => value === null || value === undefined ? "--" : `${Math.min(value * 100, 100).toFixed(1)}%`;

    function speedInfo(job, active, network) {
      const completedSpeed = Number(job.bytes_per_second || 0);
      const tailscaleSpeed = Number(network.tailscale?.receive_bytes_per_second || 0);
      const usingVpn = active && completedSpeed <= 0 && tailscaleSpeed > 0;
      const displaySpeed = usingVpn ? tailscaleSpeed : completedSpeed;
      const remainingBytes = Math.max((job.source_bytes || 0) - (job.local?.bytes || 0), 0);
      const eta = job.eta_seconds || (displaySpeed > 0 && remainingBytes > 0 ? Math.floor(remainingBytes / displaySpeed) : null);
      let className = "";
      let hint = usingVpn ? "Tailscale 实时接收" : "本地完成量";
      if (active && remainingBytes > 0) {
        if (displaySpeed <= 0) {
          className = "bad";
          hint = `${hint} · 无增长`;
        } else if (displaySpeed < LOW_SPEED_BPS) {
          className = "warn";
          hint = `${hint} · 低速异常`;
        }
      }
      return { displaySpeed, eta, className, hint, prefix: usingVpn ? "VPN " : "" };
    }

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      return data;
    }

    async function refresh() {
      snapshot = await api("/api/status");
      render(snapshot);
    }

    function render(data) {
      $("stamp").textContent = data.generated_at;
      renderSummary(data);
      renderActions(data);
      renderJobs(data);
      renderLogs(data.logs || []);
    }

    function renderSummary(data) {
      const lock = data.lock || {};
      const totalLocal = (data.jobs || []).reduce((sum, job) => sum + (job.local?.bytes || 0), 0);
      const totalSource = (data.jobs || []).reduce((sum, job) => sum + (job.source_bytes || 0), 0);
      const active = lock.active && lock.pid_alive !== false;
      $("summary").innerHTML = `
        <div class="metric"><div class="label">备份进程</div><div class="value ${active ? "ok" : "muted"}">${active ? "运行中" : "空闲"}</div></div>
        <div class="metric"><div class="label">进程 PID</div><div class="value">${lock.pid || "--"}</div></div>
        <div class="metric"><div class="label">本地已落地</div><div class="value">${fmtBytes(totalLocal)}</div></div>
        <div class="metric"><div class="label">已扫描源容量</div><div class="value">${totalSource ? fmtBytes(totalSource) : "--"}</div></div>
      `;
    }

    function renderActions(data) {
      const active = data.lock?.active && data.lock?.pid_alive !== false;
      const actionsAllowed = Boolean(data.web?.actions_allowed);
      const devices = [...new Set((data.jobs || []).map((job) => job.device))];
      $("device-actions").innerHTML = "";
      if (!actionsAllowed) {
        const readonly = document.createElement("span");
        readonly.className = "chip";
        readonly.textContent = "只读访问";
        $("device-actions").appendChild(readonly);
      }
      for (const device of devices) {
        const scan = document.createElement("button");
        scan.textContent = `扫描 ${device}`;
        scan.disabled = !actionsAllowed;
        scan.onclick = () => scanSource(device, scan);
        $("device-actions").appendChild(scan);

        const dry = document.createElement("button");
        dry.textContent = `${device} dry-run`;
        dry.disabled = active || !actionsAllowed;
        dry.onclick = () => startBackup(device, true);
        $("device-actions").appendChild(dry);

        const run = document.createElement("button");
        run.textContent = `${device} 继续/补齐`;
        run.className = "primary";
        run.disabled = active || !actionsAllowed;
        run.onclick = () => startBackup(device, false);
        $("device-actions").appendChild(run);
      }
    }

    function renderJobs(data) {
      const jobs = data.jobs || [];
      const network = data.network || {};
      const activePairs = new Set((data.lock?.active_jobs || []).map((job) => `${job.device}|${job.source_label}`));
      $("jobs").innerHTML = jobs.map((job) => {
        const active = activePairs.has(`${job.device}|${job.source_label}`);
        const speed = speedInfo(job, active, network);
        const progress = job.progress;
        const width = progress === null || progress === undefined ? 0 : Math.min(progress * 100, 100);
        const sourceText = job.source_bytes ? `${fmtBytes(job.source_bytes)}<br><span class="muted">${job.source_scanned_at || ""}</span>` : `<span class="muted">未扫描</span>`;
        return `
          <tr>
            <td><strong>${job.device}</strong><br><span class="muted">${job.ip}</span></td>
            <td>${job.source_label}</td>
            <td>${job.destination_path}</td>
            <td>${fmtBytes(job.local?.bytes)}<br><span class="muted">${job.local?.file_count || 0} files</span></td>
            <td>${sourceText}</td>
            <td>${pct(progress)}<div class="bar"><span style="width:${width}%"></span></div></td>
            <td><span class="${speed.className}">${speed.prefix}${fmtSpeed(speed.displaySpeed)}</span><br><span class="muted">${fmtDuration(speed.eta)} · ${speed.hint}</span></td>
          </tr>
        `;
      }).join("");
    }

    function renderLogs(logs) {
      $("logs").innerHTML = "";
      if (!logs.length) {
        $("logs").innerHTML = "<div class='log-item muted'>暂无日志</div>";
        return;
      }
      for (const log of logs) {
        const button = document.createElement("button");
        button.className = "log-item";
        button.innerHTML = `<strong>${log.name}</strong><br><span class="muted">${fmtBytes(log.bytes)} · ${log.modified_at}</span>`;
        button.onclick = () => openLog(log.path);
        $("logs").appendChild(button);
      }
    }

    async function openLog(path) {
      const data = await api(`/api/log?path=${encodeURIComponent(path)}`);
      $("log-text").textContent = data.text || data.error || "";
    }

    async function scanSource(device, button) {
      const buttonText = button.textContent;
      button.textContent = `扫描 ${device}...`;
      button.disabled = true;
      try {
        await api("/api/scan-source", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({device})
        });
        await refresh();
      } catch (err) {
        alert(err.message);
      } finally {
        button.textContent = buttonText;
        button.disabled = false;
      }
    }

    async function startBackup(device, dryRun) {
      if (!dryRun && !confirm(`开始 ${device} 真实增量传输？`)) return;
      try {
        await api("/api/start", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({device, dry_run: dryRun})
        });
        await refresh();
      } catch (err) {
        alert(err.message);
      }
    }

    $("refresh").onclick = refresh;
    refresh().catch((err) => alert(err.message));
    setInterval(() => refresh().catch(() => {}), 1000);
  </script>
</body>
</html>
"""

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>asiairbridge</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f6f7;
      --ink: #162028;
      --muted: #69747d;
      --line: #d8dee4;
      --panel: #ffffff;
      --soft: #eef2f5;
      --green: #166534;
      --green-bg: #eaf7ef;
      --blue: #1d4ed8;
      --blue-bg: #eef4ff;
      --amber: #92400e;
      --amber-bg: #fff7ed;
      --red: #b91c1c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
      font-size: 14px;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 650; letter-spacing: 0; }
    main { padding: 20px 24px 32px; display: grid; gap: 18px; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .topline { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .chip {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      background: var(--soft);
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .chip.good { background: var(--green-bg); color: var(--green); }
    .chip.warn { background: var(--amber-bg); color: var(--amber); }
    .chip.bad { background: #fee2e2; color: var(--red); }
    button {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 0 12px;
      font: inherit;
      cursor: pointer;
    }
    button.primary { background: var(--blue); border-color: var(--blue); color: white; }
    button:disabled { color: #9aa3aa; background: #f2f4f6; cursor: not-allowed; }
    .device-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }
    .device-card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
    .device-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfd;
    }
    .device-title { display: grid; gap: 4px; }
    .device-name { font-size: 18px; font-weight: 650; }
    .device-ip { color: var(--muted); font-size: 12px; }
    .device-body { padding: 14px 16px; display: grid; gap: 14px; }
    .metric-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .metric { border: 1px solid var(--line); border-radius: 8px; padding: 10px; min-height: 70px; }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { margin-top: 6px; font-size: 16px; font-weight: 650; overflow-wrap: anywhere; }
    .bar { width: 100%; height: 9px; border-radius: 999px; background: #e5e7eb; overflow: hidden; }
    .bar span { display: block; height: 100%; background: var(--green); width: 0%; }
    .path-line { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }
    h2 { margin: 0; font-size: 15px; font-weight: 650; letter-spacing: 0; }
    .log-grid { display: grid; grid-template-columns: minmax(260px, 360px) 1fr; gap: 0; }
    .log-list { border-right: 1px solid var(--line); max-height: 460px; overflow: auto; }
    .log-item {
      display: block;
      width: 100%;
      height: auto;
      text-align: left;
      border: 0;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      padding: 10px 12px;
      background: #fff;
    }
    pre {
      margin: 0;
      padding: 14px;
      min-height: 320px;
      max-height: 460px;
      overflow: auto;
      white-space: pre-wrap;
      font-family: Consolas, monospace;
      font-size: 12px;
      background: #111827;
      color: #dbe5ef;
    }
    .muted { color: var(--muted); }
    .warn { color: var(--amber); }
    @media (max-width: 1100px) {
      .device-grid { grid-template-columns: 1fr; }
      .log-grid { grid-template-columns: 1fr; }
      .log-list { border-right: 0; border-bottom: 1px solid var(--line); }
    }
    @media (max-width: 620px) {
      header { align-items: flex-start; flex-direction: column; }
      main { padding: 14px; }
      .metric-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>asiairbridge</h1>
      <div class="muted">EMMC Images 素材备份控制台</div>
    </div>
    <div class="topline">
      <span id="mode" class="chip">--</span>
      <span id="stamp" class="chip">--</span>
      <button id="refresh">刷新</button>
    </div>
  </header>
  <main>
    <div id="devices" class="device-grid"></div>

    <section>
      <div class="section-head">
        <h2>EMMC Images 日志</h2>
        <span class="chip">最近运行</span>
      </div>
      <div class="log-grid">
        <div id="logs" class="log-list"></div>
        <pre id="log-text">选择一个日志。</pre>
      </div>
    </section>
  </main>
  <script>
    const SOURCE_LABEL = "EMMC Images";
    const LOW_SPEED_BPS = 256 * 1024;
    let snapshot = null;

    const $ = (id) => document.getElementById(id);
    const fmtBytes = (n) => {
      if (n === null || n === undefined) return "--";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let value = Number(n);
      let i = 0;
      while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
      return `${value.toFixed(i < 2 ? 0 : 2)} ${units[i]}`;
    };
    const fmtDuration = (seconds) => {
      if (!seconds || seconds < 0) return "--";
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      if (h > 0) return `${h}h ${m}m`;
      return `${m}m`;
    };
    const fmtSpeed = (bps) => (bps === null || bps === undefined) ? "--" : `${fmtBytes(bps)}/s`;
    const pct = (value) => value === null || value === undefined ? "--" : `${Math.min(value * 100, 100).toFixed(1)}%`;

    function speedInfo(job, active, network) {
      const completedSpeed = Number(job.bytes_per_second || 0);
      const tailscaleSpeed = Number(network.tailscale?.receive_bytes_per_second || 0);
      const usingVpn = active && completedSpeed <= 0 && tailscaleSpeed > 0;
      const displaySpeed = usingVpn ? tailscaleSpeed : completedSpeed;
      const remainingBytes = Math.max((job.source_bytes || 0) - (job.local?.bytes || 0), 0);
      const eta = job.eta_seconds || (displaySpeed > 0 && remainingBytes > 0 ? Math.floor(remainingBytes / displaySpeed) : null);
      let className = "";
      let hint = usingVpn ? "Tailscale 实时接收" : "本地完成量";
      if (active && remainingBytes > 0) {
        if (displaySpeed <= 0) {
          className = "bad";
          hint = `${hint} · 无增长`;
        } else if (displaySpeed < LOW_SPEED_BPS) {
          className = "warn";
          hint = `${hint} · 低速异常`;
        }
      }
      return { displaySpeed, eta, className, hint, prefix: usingVpn ? "VPN " : "" };
    }

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      return data;
    }

    async function refresh() {
      snapshot = await api("/api/status");
      render(snapshot);
    }

    function render(data) {
      $("stamp").textContent = data.generated_at;
      $("mode").textContent = data.web?.actions_allowed ? "本机可操作" : "只读访问 · 可扫描";
      $("mode").className = data.web?.actions_allowed ? "chip good" : "chip";
      renderDevices(data);
      renderLogs((data.logs || []).filter((log) => log.name.includes("EMMC Images")));
    }

    function renderDevices(data) {
      const actionsAllowed = Boolean(data.web?.actions_allowed);
      const scanAllowed = Boolean(data.web?.scan_allowed);
      const network = data.network || {};
      const lock = data.lock || {};
      const activePairs = new Set((lock.active_jobs || []).map((job) => `${job.device}|${job.source_label}`));
      const globalBusy = Boolean(lock.active && lock.pid_alive !== false);
      const jobs = data.jobs || [];
      $("devices").innerHTML = jobs.map((job) => renderDeviceCard(job, activePairs, globalBusy, actionsAllowed, scanAllowed, network)).join("");
    }

    function renderDeviceCard(job, activePairs, globalBusy, actionsAllowed, scanAllowed, network) {
      const active = activePairs.has(`${job.device}|${job.source_label}`);
      const statusText = active ? "正在传输" : (globalBusy ? "备份锁占用" : "空闲");
      const statusClass = active ? "chip good" : (globalBusy ? "chip warn" : "chip");
      const progress = job.progress;
      const width = progress === null || progress === undefined ? 0 : Math.min(progress * 100, 100);
      const speed = speedInfo(job, active, network);
      const sourceStats = job.source?.stats || {};
      const diskTotal = sourceStats.disk_total_bytes;
      const diskFree = sourceStats.disk_free_bytes;
      const sourceText = job.source_bytes
        ? (diskTotal ? `${fmtBytes(job.source_bytes)} / ${fmtBytes(diskTotal)}` : fmtBytes(job.source_bytes))
        : "未扫描";
      const sourceMethod = sourceStats.method === "asiair_jsonrpc_get_disk_volume" ? "ASIAIR API" : "SMB 扫描";
      const scannedText = job.source_scanned_at
        ? `${sourceMethod} · ${diskFree ? `剩余 ${fmtBytes(diskFree)} · ` : ""}${job.source_scanned_at}`
        : "未扫描源容量时只显示已落地容量";
      const disabled = globalBusy || !actionsAllowed;
      return `
        <article class="device-card">
          <div class="device-head">
            <div class="device-title">
              <div class="device-name">${job.device}</div>
              <div class="device-ip">${job.ip}</div>
            </div>
            <span class="${statusClass}">${statusText}</span>
          </div>
          <div class="device-body">
            <div class="metric-grid">
              <div class="metric">
                <div class="label">本地已落地</div>
                <div class="value">${fmtBytes(job.local?.bytes)}</div>
                <div class="muted">${job.local?.file_count || 0} 个文件</div>
              </div>
              <div class="metric">
                <div class="label">源端容量</div>
                <div class="value">${sourceText}</div>
                <div class="muted">${scannedText}</div>
              </div>
              <div class="metric">
                <div class="label">进度</div>
                <div class="value">${pct(progress)}</div>
                <div class="bar"><span style="width:${width}%"></span></div>
              </div>
              <div class="metric">
                <div class="label">速度 / 预计剩余</div>
                <div class="value ${speed.className}">${speed.prefix}${fmtSpeed(speed.displaySpeed)}</div>
                <div class="muted">${fmtDuration(speed.eta)} · ${speed.hint}</div>
              </div>
            </div>
            <div class="path-line">${SOURCE_LABEL}: ${job.source_path}</div>
            <div class="path-line">目标: ${job.destination_path}</div>
            <div class="actions">
              <button ${!scanAllowed ? "disabled" : ""} onclick="scanSource('${job.device}', this)">扫描容量</button>
              <button ${disabled ? "disabled" : ""} onclick="startBackup('${job.device}', true)">预演</button>
              <button class="primary" ${disabled ? "disabled" : ""} onclick="startBackup('${job.device}', false)">继续备份</button>
            </div>
          </div>
        </article>
      `;
    }

    function renderLogs(logs) {
      $("logs").innerHTML = "";
      if (!logs.length) {
        $("logs").innerHTML = "<div class='log-item muted'>暂无 EMMC Images 日志</div>";
        return;
      }
      for (const log of logs) {
        const button = document.createElement("button");
        button.className = "log-item";
        button.innerHTML = `<strong>${log.name}</strong><br><span class="muted">${fmtBytes(log.bytes)} · ${log.modified_at}</span>`;
        button.onclick = () => openLog(log.path);
        $("logs").appendChild(button);
      }
    }

    async function openLog(path) {
      const data = await api(`/api/log?path=${encodeURIComponent(path)}`);
      $("log-text").textContent = data.text || data.error || "";
    }

    async function scanSource(device, button) {
      const buttonText = button.textContent;
      button.textContent = "扫描中...";
      button.disabled = true;
      try {
        await api("/api/scan-source", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({device})
        });
        await refresh();
      } catch (err) {
        alert(err.message);
      } finally {
        button.textContent = buttonText;
        button.disabled = false;
      }
    }

    async function startBackup(device, dryRun) {
      const label = dryRun ? "预演" : "继续备份";
      if (!dryRun && !confirm(`开始 ${device} 的 ${SOURCE_LABEL} 真实增量传输？`)) return;
      try {
        await api("/api/start", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({device, dry_run: dryRun})
        });
        await refresh();
      } catch (err) {
        alert(`${label}失败：${err.message}`);
      }
    }

    $("refresh").onclick = refresh;
    refresh().catch((err) => alert(err.message));
    setInterval(() => refresh().catch(() => {}), 1000);
  </script>
</body>
</html>
"""
