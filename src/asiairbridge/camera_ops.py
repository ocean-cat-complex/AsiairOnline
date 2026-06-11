from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from .config import AppConfig, Device
from .image_preview import current_image_response
from .rpc import IMAGER_PORT, asiair_device_rpc, asiair_device_rpc_batch
from .web_control import control_state

ControlSpec = dict[str, Any]

CONTROL_SPECS: dict[str, ControlSpec] = {
    "gain": {"rpc": "Gain", "coerce": int},
    "offset": {"rpc": "Offset", "coerce": int},
    "target_temp": {"rpc": "TargetTemp", "coerce": float},
    "cooler_on": {"rpc": "CoolerOn", "coerce": lambda value: 1 if _to_bool(value) else 0},
    "anti_dew_heater": {"rpc": "AntiDewHeater", "coerce": lambda value: 1 if _to_bool(value) else 0},
    "hardware_bin": {"rpc": "HardwareBin", "coerce": lambda value: 1 if _to_bool(value) else 0},
    "frame_size": {"rpc": "FrameSize", "coerce": int},
    "fan_half_speed": {"rpc": "FanHalfSpeed", "coerce": lambda value: 1 if _to_bool(value) else 0},
    "led_on": {"rpc": "LedOn", "coerce": lambda value: 1 if _to_bool(value) else 0},
}

STATUS_CONTROL_KEYS: tuple[str, ...] = (
    "temperature",
    "cool_power",
    "gain",
    "offset",
    "target_temp",
    "cooler_on",
    "anti_dew_heater",
    "hardware_bin",
    "frame_size",
    "fan_half_speed",
    "led_on",
)

STATUS_CONTROL_RPC_NAMES: dict[str, str] = {
    "temperature": "Temperature",
    "cool_power": "CoolPowerPerc",
    "gain": "Gain",
    "offset": "Offset",
    "target_temp": "TargetTemp",
    "cooler_on": "CoolerOn",
    "anti_dew_heater": "AntiDewHeater",
    "hardware_bin": "HardwareBin",
    "frame_size": "FrameSize",
    "fan_half_speed": "FanHalfSpeed",
    "led_on": "LedOn",
}


def camera_status_response(
    config: AppConfig,
    device_name: str | None,
    session_id: str | None = None,
    rpc_timeout_seconds: float = 1.2,
    queue_timeout_seconds: float = 0.15,
    status_budget_seconds: float = 3.0,
    priority: str = "background",
) -> dict[str, Any]:
    device = _select_device(config, device_name)
    request_id = 70_000
    errors: list[dict[str, Any]] = []
    started = time.perf_counter()
    budget_exceeded = False

    def rpc(method: str, params: Any | None = None, optional: bool = False) -> Any | None:
        nonlocal request_id, budget_exceeded
        if time.perf_counter() - started > status_budget_seconds:
            if not optional and not budget_exceeded:
                errors.append({"method": method, "error": "camera status refresh budget exceeded"})
                budget_exceeded = True
            return None
        request_id += 1
        try:
            response = asiair_device_rpc(
                device,
                method,
                params=params,
                request_id=request_id,
                port=IMAGER_PORT,
                timeout_seconds=rpc_timeout_seconds,
                priority=priority,
                queue_timeout_seconds=queue_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            if not optional:
                errors.append({"method": method, "error": str(exc)})
            return None
        if response.get("code") != 0:
            if not optional:
                errors.append({"method": method, "code": response.get("code")})
            return None
        return response.get("result")

    app_state = rpc("get_app_state") or {}
    capture = app_state.get("capture") if isinstance(app_state, dict) else {}
    camera_state = rpc("get_camera_state") or {}
    camera_info = rpc("get_camera_info") or {}
    exp_bin = rpc("get_camera_exp_and_bin") or {}
    controls = rpc("get_controls") or []
    sixteen_bit = rpc("get_camera_16bit")
    subframe = rpc("get_subframe") or {}
    can_liveview = rpc("can_liveview", optional=True)
    can_abort_expose = rpc("can_abort_expose", optional=True)

    control_catalog = _control_catalog(controls)
    control_values: dict[str, Any] = {}
    for key in STATUS_CONTROL_KEYS:
        rpc_name = STATUS_CONTROL_RPC_NAMES[key]
        if isinstance(controls, list) and controls and rpc_name not in control_catalog:
            continue
        if time.perf_counter() - started > status_budget_seconds:
            break
        result = rpc("get_control_value", [rpc_name], optional=True) or {}
        control_values[key] = _control_snapshot(rpc_name, result, control_catalog.get(rpc_name))

    cached_image: dict[str, Any] | None = None
    try:
        cached_image = current_image_response(config, device.name, force=False)
    except Exception as exc:  # noqa: BLE001
        errors.append({"method": "current_image_response", "error": str(exc)})

    exposure_us = exp_bin.get("exposure") if isinstance(exp_bin, dict) else None
    exposure_seconds = _normalize_control_value("Exposure", exposure_us)
    image_info = cached_image.get("image") if isinstance(cached_image, dict) and cached_image.get("ok") else {}

    return {
        "ok": True,
        "partial": bool(errors),
        "errors": errors,
        "snapshot_at": datetime.now().isoformat(timespec="seconds"),
        "device": {"name": device.name, "ip": device.ip},
        "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
        "lease": control_state(config, device.name, session_id=session_id),
        "app": {
            "page": app_state.get("page") if isinstance(app_state, dict) else None,
            "capture_state": capture.get("state") if isinstance(capture, dict) else None,
            "capture_working": bool(capture.get("is_working")) if isinstance(capture, dict) else False,
            "exposure_mode": capture.get("exposure_mode") if isinstance(capture, dict) else None,
            "capture_lapse_ms": _capture_int(capture, "lapse_ms"),
            "capture_total_ms": _capture_int(capture, "total_ms"),
            "capture_progress": _capture_progress(capture),
        },
        "camera": {
            "name": camera_state.get("name") if isinstance(camera_state, dict) else None,
            "state": camera_state.get("state") if isinstance(camera_state, dict) else None,
            "path": camera_state.get("path") if isinstance(camera_state, dict) else None,
            "chip_size": camera_info.get("chip_size") if isinstance(camera_info, dict) else None,
            "bins": camera_info.get("bins") if isinstance(camera_info, dict) else None,
            "pixel_size_um": camera_info.get("pixel_size_um") if isinstance(camera_info, dict) else None,
            "has_cooler": camera_info.get("has_cooler") if isinstance(camera_info, dict) else None,
            "is_color": camera_info.get("is_color") if isinstance(camera_info, dict) else None,
            "is_usb3_host": camera_info.get("is_usb3_host") if isinstance(camera_info, dict) else None,
            "sixteen_bit": bool(sixteen_bit) if sixteen_bit is not None else None,
            "can_liveview": bool(can_liveview) if can_liveview is not None else None,
            "can_abort_expose": bool(can_abort_expose) if can_abort_expose is not None else None,
        },
        "exposure": {
            "us": exposure_us,
            "seconds": exposure_seconds,
            "bin": exp_bin.get("bin") if isinstance(exp_bin, dict) else None,
        },
        "controls": control_values,
        "subframe": {
            "width": subframe.get("width") if isinstance(subframe, dict) else None,
            "height": subframe.get("height") if isinstance(subframe, dict) else None,
            "x": subframe.get("x") if isinstance(subframe, dict) else None,
            "y": subframe.get("y") if isinstance(subframe, dict) else None,
        },
        "image": {
            "generated_at": cached_image.get("generated_at") if isinstance(cached_image, dict) else None,
            "age_seconds": cached_image.get("age_seconds") if isinstance(cached_image, dict) else None,
            "refreshed": cached_image.get("refreshed") if isinstance(cached_image, dict) else None,
            "image_id": image_info.get("image_id") if isinstance(image_info, dict) else None,
            "width": image_info.get("original_width") or image_info.get("width") if isinstance(image_info, dict) else None,
            "height": image_info.get("original_height") or image_info.get("height") if isinstance(image_info, dict) else None,
            "exposure_ms": image_info.get("exposure_ms") if isinstance(image_info, dict) else None,
            "bin": image_info.get("bin") if isinstance(image_info, dict) else None,
        },
    }


def capture_progress_response(
    config: AppConfig,
    device_name: str | None,
    *,
    rpc_timeout_seconds: float = 1.5,
) -> dict[str, Any]:
    device = _select_device(config, device_name)
    try:
        response = asiair_device_rpc(
            device,
            "get_app_state",
            request_id=72_000,
            port=IMAGER_PORT,
            timeout_seconds=rpc_timeout_seconds,
            priority="foreground",
            queue_timeout_seconds=1.0,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "device": {"name": device.name, "ip": device.ip},
            "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
            "error": str(exc),
        }
    if response.get("code") != 0:
        return {
            "ok": False,
            "device": {"name": device.name, "ip": device.ip},
            "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
            "error": f"get_app_state returned code {response.get('code')}",
        }
    result = response.get("result")
    capture = result.get("capture") if isinstance(result, dict) else {}
    if not isinstance(capture, dict):
        capture = {}
    return {
        "ok": True,
        "device": {"name": device.name, "ip": device.ip},
        "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
        "endpoint": response.get("_endpoint"),
        "snapshot_at": datetime.now().isoformat(timespec="seconds"),
        "page": result.get("page") if isinstance(result, dict) else None,
        "state": capture.get("state"),
        "is_working": bool(capture.get("is_working")),
        "exposure_mode": capture.get("exposure_mode"),
        "lapse_ms": _capture_int(capture, "lapse_ms"),
        "total_ms": _capture_int(capture, "total_ms"),
        "progress": _capture_progress(capture),
    }


def _capture_int(capture: Any, key: str) -> int | None:
    if not isinstance(capture, dict):
        return None
    try:
        return int(capture.get(key))
    except (TypeError, ValueError):
        return None


def _capture_progress(capture: Any) -> float | None:
    lapse = _capture_int(capture, "lapse_ms")
    total = _capture_int(capture, "total_ms")
    if lapse is None or not total or total <= 0:
        return None
    return round(min(max(lapse / total, 0.0), 1.0), 4)


def camera_action_response(
    config: AppConfig,
    device_name: str | None,
    action: str,
    payload: dict[str, Any],
    session_id: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    device = _select_device(config, device_name)
    request_id = 80_000
    writes: list[dict[str, Any]] = []
    ignored_fields: list[str] = []

    def progress(
        step: int,
        total: int,
        label: str,
        *,
        state: str = "running",
        detail: str | None = None,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "step": step,
                "total": total,
                "label": label,
                "state": state,
                "detail": detail,
                "writes": writes,
            }
        )

    def read_rpc(
        method: str,
        params: Any | None = None,
        ok_codes: tuple[int, ...] = (0,),
        timeout_seconds: float = 8.0,
    ) -> Any:
        nonlocal request_id
        request_id += 1
        try:
            response = asiair_device_rpc(
                device,
                method,
                params=params,
                request_id=request_id,
                port=IMAGER_PORT,
                timeout_seconds=timeout_seconds,
                priority="write",
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{method} {params!r} failed: {exc}") from exc
        if response.get("code") not in ok_codes:
            raise RuntimeError(f"{method} failed with code {response.get('code')}: {response.get('error')}")
        return response.get("result")

    def rpc(
        method: str,
        params: Any | None = None,
        ok_codes: tuple[int, ...] = (0,),
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        nonlocal request_id
        request_id += 1
        started = time.perf_counter()
        try:
            response = asiair_device_rpc(
                device,
                method,
                params=params,
                request_id=request_id,
                port=IMAGER_PORT,
                timeout_seconds=timeout_seconds,
                priority="write",
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{method} {params!r} failed: {exc}") from exc
        if response.get("code") not in ok_codes:
            raise RuntimeError(f"{method} failed with code {response.get('code')}")
        writes.append(
            {
                "method": method,
                "params": params,
                "code": response.get("code"),
                "seconds": round(time.perf_counter() - started, 3),
            }
        )
        return response

    def confirm_exp_bin(exposure_us: int, bin_value: int, attempts: int = 3) -> dict[str, Any]:
        last_error: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                current = read_rpc("get_camera_exp_and_bin", timeout_seconds=8.0) or {}
                current_exposure = _to_int(current.get("exposure"), minimum=1)
                current_bin = _to_int(current.get("bin"), minimum=1, maximum=8)
                matched = current_exposure == exposure_us and current_bin == bin_value
                return {
                    "ok": matched,
                    "attempt": attempt,
                    "exposure": current_exposure,
                    "bin": current_bin,
                }
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                time.sleep(0.35)
        return {"ok": False, "error": last_error}

    def set_exp_bin_with_confirm(exposure_us: int, bin_value: int) -> dict[str, Any]:
        try:
            rpc(
                "set_camera_exp_and_bin",
                [{"exposure": exposure_us, "bin": bin_value}],
                timeout_seconds=12.0,
            )
            confirm = confirm_exp_bin(exposure_us, bin_value, attempts=2)
            if not confirm.get("ok"):
                raise RuntimeError(
                    "set_camera_exp_and_bin returned but readback did not match: "
                    f"expected {exposure_us}/Bin{bin_value}, got {confirm}"
                )
            return {"ok": True, "confirmed": True, "confirm": confirm}
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "timed out" not in message.lower() and "timeout" not in message.lower():
                raise
            confirm = confirm_exp_bin(exposure_us, bin_value, attempts=3)
            if confirm.get("ok"):
                writes.append(
                    {
                        "method": "set_camera_exp_and_bin",
                        "params": [{"exposure": exposure_us, "bin": bin_value}],
                        "code": 0,
                        "seconds": None,
                        "note": "write timed out, readback confirmed",
                    }
                )
                return {
                    "ok": True,
                    "confirmed": True,
                    "timed_out": True,
                    "confirm": confirm,
                    "warning": message,
                }
            raise RuntimeError(f"{message}; readback confirm failed: {confirm}") from exc

    action_name = str(action or "").strip().lower()
    if action_name == "apply_exposure":
        progress(1, 3, "校验曝光参数")
        exposure_us = _to_exposure_us(payload.get("exposure_seconds"))
        bin_value = _to_int(payload.get("bin"), minimum=1, maximum=8)
        progress(2, 3, f"写入曝光/Bin：{exposure_us / 1_000_000:g}s / Bin {bin_value}")
        write_result = set_exp_bin_with_confirm(exposure_us, bin_value)
        progress(3, 3, "确认曝光/Bin", detail=str(write_result.get("confirm") or ""), state="done")
        time.sleep(0.08)
        return {
            "ok": True,
            "device": {"name": device.name, "ip": device.ip},
            "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
            "action": action_name,
            "writes": writes,
            "ignored_fields": ignored_fields,
            "write_result": write_result,
            "refresh_after_ms": 600,
        }
    elif action_name == "apply_camera":
        progress(1, 3, "校验相机参数")
        if "sixteen_bit" in payload:
            ignored_fields.append("sixteen_bit")
        planned = [(key, spec) for key, spec in CONTROL_SPECS.items() if key in payload]
        batch: list[tuple[str, Any]] = []
        for key, spec in planned:
            coerce: Callable[[Any], Any] = spec["coerce"]
            batch.append(("set_control_value", [spec["rpc"], coerce(payload.get(key))]))
        if batch:
            progress(2, 3, f"批量写入 {len(batch)} 项相机参数")
            started = time.perf_counter()
            responses = asiair_device_rpc_batch(
                device,
                batch,
                port=IMAGER_PORT,
                priority="write",
                timeout_seconds=8.0,
            )
            elapsed = round(time.perf_counter() - started, 3)
            for (_, spec), response, (_, params) in zip(planned, responses, batch):
                if response is None:
                    raise RuntimeError(f"set_control_value {spec['rpc']} got no response")
                if response.get("code") != 0:
                    raise RuntimeError(
                        f"set_control_value {spec['rpc']} failed with code {response.get('code')}"
                    )
                writes.append(
                    {
                        "method": "set_control_value",
                        "params": params,
                        "code": response.get("code"),
                        "seconds": elapsed,
                        "endpoint": response.get("_endpoint"),
                    }
                )
        progress(3, 3, "相机参数写入完成", state="done")
        time.sleep(0.05)
        return {
            "ok": True,
            "device": {"name": device.name, "ip": device.ip},
            "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
            "action": action_name,
            "writes": writes,
            "ignored_fields": ignored_fields,
            "refresh_after_ms": 1200,
        }
    elif action_name == "shutter":
        progress(1, 5, "检查 16 Bit 状态")
        if read_rpc("get_camera_16bit") is not True:
            raise RuntimeError("Camera is not in 16-bit mode; reopen the camera in ASIAIR before shooting")
        exposure_seconds = payload.get("exposure_seconds")
        bin_value = payload.get("bin")
        if exposure_seconds is not None or bin_value is not None:
            current_exp_bin = (
                read_rpc("get_camera_exp_and_bin") or {}
                if exposure_seconds is None or bin_value is None
                else {}
            )
            exposure_us = (
                _to_exposure_us(exposure_seconds)
                if exposure_seconds is not None
                else _to_int(current_exp_bin.get("exposure"), minimum=1)
            )
            resolved_bin = (
                _to_int(bin_value, minimum=1, maximum=8)
                if bin_value is not None
                else _to_int(current_exp_bin.get("bin"), minimum=1, maximum=8)
            )
            progress(2, 5, f"写入曝光/Bin：{exposure_us / 1_000_000:g}s / Bin {resolved_bin}")
            set_exp_bin_with_confirm(exposure_us, resolved_bin)
        else:
            progress(2, 5, "沿用当前曝光/Bin", state="done")
        progress(3, 5, "切换到预览页面")
        rpc("set_page", ["preview"], timeout_seconds=10.0)
        progress(4, 5, "下发快门")
        rpc("start_exposure", {"keep_autosave_dev": True}, timeout_seconds=12.0)
        progress(5, 5, "快门已发送，等待图像生成", state="done")
        time.sleep(0.08)
        return {
            "ok": True,
            "device": {"name": device.name, "ip": device.ip},
            "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
            "action": action_name,
            "writes": writes,
            "ignored_fields": ignored_fields,
            "refresh_after_ms": 900,
        }
    else:
        raise ValueError(f"Unsupported camera action: {action}")


def _control_catalog(controls: Any) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    if not isinstance(controls, list):
        return catalog
    for item in controls:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        catalog[str(name)] = {
            "type": item.get("type"),
            "min": item.get("min"),
            "max": item.get("max"),
            "step": item.get("step"),
            "text": item.get("text"),
        }
    return catalog


def _control_snapshot(name: str, result: Any, catalog: dict[str, Any] | None) -> dict[str, Any]:
    result_dict = result if isinstance(result, dict) else {}
    raw_value = result_dict.get("value")
    normalized = _normalize_control_value(name, raw_value)
    payload = {
        "rpc_name": name,
        "value": normalized,
        "raw_value": raw_value,
        "text": result_dict.get("text"),
        "display": result_dict.get("text") or _display_control_value(name, normalized),
    }
    if catalog:
        payload.update(catalog)
    return payload


def _display_control_value(name: str, value: Any) -> str:
    if value is None:
        return "--"
    if name in {"CoolerOn", "AntiDewHeater", "HardwareBin", "FanHalfSpeed", "LedOn"}:
        return "On" if bool(value) else "Off"
    if name == "Temperature":
        return f"{value}°C"
    if name == "CoolPowerPerc":
        return f"{value}%"
    if name == "TargetTemp":
        return f"{value}°C"
    return str(value)


def _normalize_control_value(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name == "Temperature":
        try:
            return round(float(value) / 10.0, 1)
        except (TypeError, ValueError):
            return value
    if name == "Exposure":
        try:
            return round(float(value) / 1_000_000, 3)
        except (TypeError, ValueError):
            return value
    return value


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _to_exposure_us(value: Any) -> int:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("exposure_seconds is required") from exc
    if seconds <= 0:
        raise ValueError("exposure_seconds must be greater than 0")
    return int(round(seconds * 1_000_000))


def _to_int(value: Any, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer value: {value}") from exc
    if minimum is not None and number < minimum:
        raise ValueError(f"Value must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"Value must be <= {maximum}")
    return number


def _select_device(config: AppConfig, device_name: str | None) -> Device:
    devices = config.enabled_devices()
    if device_name:
        for device in devices:
            if device.name == device_name:
                return device
        raise ValueError(f"Unknown or disabled device: {device_name}")
    return config.default_device()
