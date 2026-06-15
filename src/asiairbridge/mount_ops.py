from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import AppConfig, Device
from .rpc import GUIDER_PORT, IMAGER_PORT, asiair_device_rpc

# The ASIAIR exposes equatorial-mount reads on the GUIDER port (4400), not the
# imager port (4700). All methods used here are read-only; no mount control.
MOVING_LABELS = {
    "none": "静止",
    "slewing": "转动中",
    "moving": "转动中",
    "tracking": "跟踪中",
}
PIERSIDE_LABELS = {
    "pier_east": "东侧 (镜在西)",
    "pier_west": "西侧 (镜在东)",
    "unknown": "未知",
}


def mount_status_response(
    config: AppConfig,
    device_name: str | None,
    *,
    rpc_timeout_seconds: float = 1.2,
    status_budget_seconds: float = 5.0,
) -> dict[str, Any]:
    device = _select_device(config, device_name)
    request_id = 60_000
    started = datetime.now()
    deadline = started.timestamp() + status_budget_seconds
    errors: list[dict[str, Any]] = []
    raw: dict[str, Any] = {}

    def rpc(method: str, port: int = GUIDER_PORT, optional: bool = False) -> Any:
        nonlocal request_id
        remaining = deadline - datetime.now().timestamp()
        if remaining <= 0:
            if not optional:
                errors.append({"method": method, "error": "mount status refresh budget exceeded"})
            return None
        request_id += 1
        endpoint_count = max(1, len(device.endpoint_candidates()))
        timeout_seconds = max(0.2, min(rpc_timeout_seconds, remaining / endpoint_count))
        try:
            response = asiair_device_rpc(
                device,
                method,
                request_id=request_id,
                port=port,
                timeout_seconds=timeout_seconds,
                priority="foreground",
                queue_timeout_seconds=min(1.0, timeout_seconds),
            )
        except Exception as exc:  # noqa: BLE001
            if not optional:
                errors.append({"method": method, "error": str(exc)})
            return None
        raw[method] = {"code": response.get("code"), "result": response.get("result")}
        if response.get("code") != 0:
            return None
        return response.get("result")

    ra_dec = rpc("scope_get_ra_dec")
    track = rpc("scope_get_track_state")
    moving = rpc("scope_is_moving")
    pierside = rpc("scope_get_pierside")
    location = rpc("scope_get_location")
    cap = rpc("scope_get_cap", optional=True)
    app_state = rpc("get_app_state", port=IMAGER_PORT, optional=True)

    connected = isinstance(ra_dec, list) and len(ra_dec) >= 2

    position = None
    if connected:
        ra_h = _num(ra_dec[0])
        dec_d = _num(ra_dec[1])
        lst_h = _num(ra_dec[2]) if len(ra_dec) > 2 else None
        position = {
            "ra_hours": ra_h,
            "dec_degrees": dec_d,
            "lst_hours": lst_h,
            "ra_text": _hms(ra_h),
            "dec_text": _dms(dec_d, signed=True),
            "lst_text": _hms(lst_h),
        }

    site = None
    if isinstance(location, list) and len(location) >= 2:
        lat = _num(location[0])
        lng = _num(location[1])
        site = {
            "lat": lat,
            "lng": lng,
            "lat_text": _dms(lat, signed=True),
            "lng_text": _dms(lng, signed=True),
        }

    target = None
    auto_goto = app_state.get("auto_goto") if isinstance(app_state, dict) else None
    if isinstance(auto_goto, dict) and auto_goto:
        trd = auto_goto.get("target_ra_dec")
        has_coord = isinstance(trd, list) and len(trd) >= 2
        target = {
            "name": auto_goto.get("target_name"),
            "ra_hours": _num(trd[0]) if has_coord else None,
            "dec_degrees": _num(trd[1]) if has_coord else None,
            "ra_text": _hms(_num(trd[0])) if has_coord else None,
            "dec_text": _dms(_num(trd[1]), signed=True) if has_coord else None,
            "angle": auto_goto.get("target_angle"),
            "slewing": bool(auto_goto.get("is_working")),
        }

    moving_text = MOVING_LABELS.get(str(moving), str(moving)) if moving is not None else None

    return {
        "ok": True,
        "device": {"name": device.name, "ip": device.ip},
        "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
        "snapshot_at": datetime.now().isoformat(timespec="seconds"),
        "connected": connected,
        "tracking": track if isinstance(track, bool) else None,
        "moving": moving,
        "moving_text": moving_text,
        "is_moving": moving not in (None, "none", "stopped"),
        "pier_side": pierside,
        "pier_side_text": PIERSIDE_LABELS.get(str(pierside), str(pierside)) if pierside is not None else None,
        "position": position,
        "site": site,
        "capabilities": cap if isinstance(cap, list) else None,
        "target": target,
        "errors": errors,
        "raw": raw,
    }


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hms(hours: float | None) -> str | None:
    if hours is None:
        return None
    total = hours % 24 if hours >= 0 else hours
    h = int(total)
    rem = (total - h) * 60
    m = int(rem)
    s = int(round((rem - m) * 60))
    if s == 60:
        s = 0
        m += 1
    if m == 60:
        m = 0
        h += 1
    return f"{h:02d}:{m:02d}:{s:02d}"


def _dms(degrees: float | None, signed: bool = False) -> str | None:
    if degrees is None:
        return None
    sign = "-" if degrees < 0 else ("+" if signed else "")
    val = abs(degrees)
    d = int(val)
    rem = (val - d) * 60
    m = int(rem)
    s = int(round((rem - m) * 60))
    if s == 60:
        s = 0
        m += 1
    if m == 60:
        m = 0
        d += 1
    return f"{sign}{d:02d}°{m:02d}'{s:02d}\""


def _select_device(config: AppConfig, device_name: str | None) -> Device:
    devices = config.enabled_devices()
    if device_name:
        for device in devices:
            if device.name == device_name:
                return device
        raise ValueError(f"Unknown or disabled device: {device_name}")
    return config.default_device()
