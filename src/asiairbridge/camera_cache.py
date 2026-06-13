from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .camera_ops import camera_status_response
from .config import AppConfig, Device
from .rpc import GUIDER_PORT
from .web_control import control_state


class CameraStateCache:
    def __init__(self, config: AppConfig, interval_seconds: float = 3.0) -> None:
        self.config = config
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._wakes: dict[str, threading.Event] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._payloads: dict[str, dict[str, Any]] = {}
        self._updated_at: dict[str, float] = {}
        self._load_persisted_payloads()

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = []
        for device in self.config.enabled_devices():
            wake = self._wakes.setdefault(device.name, threading.Event())
            thread = threading.Thread(
                target=self._run_device,
                args=(device, wake),
                name=f"asiair-camera-cache-{device.name}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for wake in self._wakes.values():
            wake.set()

    def trigger(self, device_name: str | None = None) -> None:
        if device_name:
            self._wakes.setdefault(device_name, threading.Event()).set()
            return
        for wake in self._wakes.values():
            wake.set()

    def get(self, device_name: str | None, session_id: str | None = None) -> dict[str, Any]:
        device = _select_device(self.config, device_name)
        with self._lock:
            payload = copy.deepcopy(self._payloads.get(device.name))
            updated_at = self._updated_at.get(device.name)

        if payload is None:
            payload = _empty_payload(self.config, device, "camera state cache is warming")
        else:
            payload["lease"] = control_state(self.config, device.name, session_id=session_id)

        age = None if updated_at is None else max(0.0, time.time() - updated_at)
        payload["cache"] = {
            "from_cache": True,
            "updated_at": (
                datetime.fromtimestamp(updated_at).isoformat(timespec="seconds")
                if updated_at is not None
                else None
            ),
            "age_seconds": round(age, 1) if age is not None else None,
            "status": "warming" if updated_at is None else ("partial" if payload.get("partial") else "ready"),
        }
        return payload

    def get_all(self, session_id: str | None = None) -> dict[str, Any]:
        devices: dict[str, dict[str, Any]] = {}
        for device in self.config.enabled_devices():
            devices[device.name] = self.get(device.name, session_id=session_id)
        return {
            "ok": True,
            "snapshot_at": datetime.now().isoformat(timespec="seconds"),
            "devices": devices,
        }

    def store(self, payload: dict[str, Any]) -> None:
        device = payload.get("device") if isinstance(payload, dict) else None
        device_name = device.get("name") if isinstance(device, dict) else None
        if not device_name:
            return
        self._store_device_payload(str(device_name), payload)

    def patch_from_action(self, device_name: str, result: dict[str, Any]) -> None:
        with self._lock:
            payload = copy.deepcopy(self._payloads.get(device_name))
        if payload is None:
            try:
                device = _select_device(self.config, device_name)
            except Exception:  # noqa: BLE001
                return
            payload = _empty_payload(self.config, device, "cache patched from action")

        changed = False
        for write in result.get("writes") or []:
            if not isinstance(write, dict):
                continue
            if write.get("method") == "set_camera_exp_and_bin":
                params = write.get("params")
                if isinstance(params, list) and params and isinstance(params[0], dict):
                    exposure_us = params[0].get("exposure")
                    bin_value = params[0].get("bin")
                    payload["exposure"] = {
                        "us": exposure_us,
                        "seconds": _exposure_seconds(exposure_us),
                        "bin": bin_value,
                    }
                    changed = True

        if changed:
            payload["snapshot_at"] = datetime.now().isoformat(timespec="seconds")
            payload["partial"] = bool(payload.get("errors"))
            self._store_device_payload(device_name, payload)

    def _run_device(self, device: Device, wake: threading.Event) -> None:
        while not self._stop.is_set():
            self._refresh_device(device)
            wake.wait(self.interval_seconds)
            wake.clear()

    def _refresh_device(self, device: Device) -> None:
        try:
            payload = camera_status_response(
                self.config,
                device.name,
                session_id=None,
                rpc_timeout_seconds=2.5,
                queue_timeout_seconds=2.0,
                status_budget_seconds=8.0,
            )
        except Exception as exc:  # noqa: BLE001
            payload = _empty_payload(self.config, device, str(exc))
        self._store_device_payload(device.name, payload)

    def _store_device_payload(self, device_name: str, payload: dict[str, Any]) -> None:
        with self._lock:
            previous = self._payloads.get(device_name)
            merged = _merge_payload(previous, payload)
            self._payloads[device_name] = merged
            self._updated_at[device_name] = time.time()
        self._persist_payload(device_name, merged)

    def _load_persisted_payloads(self) -> None:
        for device in self.config.enabled_devices():
            path = self._cache_file(device.name)
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            payload.pop("lease", None)
            payload.pop("cache", None)
            updated_at = _payload_timestamp(payload)
            if updated_at is None:
                try:
                    updated_at = path.stat().st_mtime
                except OSError:
                    updated_at = time.time()
            with self._lock:
                self._payloads[device.name] = payload
                self._updated_at[device.name] = updated_at

    def _persist_payload(self, device_name: str, payload: dict[str, Any]) -> None:
        path = self._cache_file(device_name)
        body = copy.deepcopy(payload)
        body.pop("lease", None)
        body.pop("cache", None)
        body["_persisted_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            return

    def _cache_file(self, device_name: str) -> Path:
        return self.config.state_path() / "camera-cache" / f"{device_name}.json"


def _merge_payload(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    if previous is None:
        return copy.deepcopy(current)
    merged = copy.deepcopy(current)
    for key in ("app", "camera", "exposure", "controls", "mount", "subframe", "image"):
        merged[key] = _merge_non_empty(previous.get(key), current.get(key))
    if isinstance(merged.get("mount"), dict) and isinstance(current.get("mount"), dict):
        merged["mount"]["errors"] = copy.deepcopy(current["mount"].get("errors") or [])
    return merged


def _merge_non_empty(previous: Any, current: Any) -> Any:
    if isinstance(previous, dict) and isinstance(current, dict):
        result = copy.deepcopy(current)
        for key, previous_value in previous.items():
            result[key] = _merge_non_empty(previous_value, result.get(key))
        return result
    if isinstance(current, list):
        return current if current else copy.deepcopy(previous)
    if current is None or current == "":
        return copy.deepcopy(previous)
    return copy.deepcopy(current)


def _payload_timestamp(payload: dict[str, Any]) -> float | None:
    for key in ("snapshot_at", "_persisted_at"):
        value = payload.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            continue
    return None


def _empty_payload(config: AppConfig, device: Device, error: str) -> dict[str, Any]:
    return {
        "ok": True,
        "partial": True,
        "errors": [{"method": "camera_cache", "error": error}],
        "snapshot_at": datetime.now().isoformat(timespec="seconds"),
        "device": {"name": device.name, "ip": device.ip},
        "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
        "lease": control_state(config, device.name, session_id=None),
        "app": {
            "page": None,
            "capture_state": None,
            "capture_working": False,
            "exposure_mode": None,
        },
        "camera": {
            "name": None,
            "state": None,
            "path": None,
            "chip_size": None,
            "bins": None,
            "pixel_size_um": None,
            "has_cooler": None,
            "is_color": None,
            "is_usb3_host": None,
            "sixteen_bit": None,
            "can_liveview": None,
            "can_abort_expose": None,
        },
        "exposure": {"us": None, "seconds": None, "bin": None},
        "controls": {},
        "mount": {
            "available": False,
            "port": GUIDER_PORT,
            "ra_hours": None,
            "dec_degrees": None,
            "ra_text": None,
            "dec_text": None,
            "raw_ra_dec": None,
            "track_enabled": None,
            "track_mode": {"value": None, "index": None, "list": []},
            "slew_rate": {"value": None, "index": None, "list": []},
            "moving": None,
            "pier_side": None,
            "location": {"latitude": None, "longitude": None},
            "errors": [{"method": "camera_cache", "error": error}],
        },
        "subframe": {"width": None, "height": None, "x": None, "y": None},
        "image": {
            "generated_at": None,
            "age_seconds": None,
            "refreshed": False,
            "image_id": None,
            "width": None,
            "height": None,
            "exposure_ms": None,
            "bin": None,
        },
    }


def _select_device(config: AppConfig, device_name: str | None) -> Device:
    devices = config.enabled_devices()
    if device_name:
        for device in devices:
            if device.name == device_name:
                return device
        raise ValueError(f"Unknown or disabled device: {device_name}")
    return config.default_device()


def _exposure_seconds(exposure_us: Any) -> float | None:
    try:
        return round(float(exposure_us) / 1_000_000, 6)
    except (TypeError, ValueError):
        return None
