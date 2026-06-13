from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig, Device


IMAGER_PORT = 4700
GUIDER_PORT = 4400
MAX_RPC_RESPONSE_BYTES = 16 * 1024 * 1024


class _EndpointGate:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.owner_thread: int | None = None
        self.depth = 0
        self.pending_writes = 0
        self.pending_foreground = 0
        self.write_cooldown_until = 0.0


class _DeviceGate:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.critical_owner_thread: int | None = None
        self.critical_depth = 0
        self.pending_critical = 0
        self.critical_cooldown_until = 0.0


_RPC_GATES: dict[tuple[str, int], _EndpointGate] = {}
_DEVICE_GATES: dict[str, _DeviceGate] = {}
_RPC_GATES_LOCK = threading.Lock()


def _rpc_gate(ip: str, port: int) -> _EndpointGate:
    key = (ip, port)
    with _RPC_GATES_LOCK:
        gate = _RPC_GATES.get(key)
        if gate is None:
            gate = _EndpointGate()
            _RPC_GATES[key] = gate
        return gate


def _device_gate(ip: str) -> _DeviceGate:
    with _RPC_GATES_LOCK:
        gate = _DEVICE_GATES.get(ip)
        if gate is None:
            gate = _DeviceGate()
            _DEVICE_GATES[ip] = gate
        return gate


def _is_write_priority(priority: str) -> bool:
    return str(priority).lower() in {"write", "high", "action", "control", "critical"}


def _is_foreground_priority(priority: str) -> bool:
    return str(priority).lower() in {"foreground", "manual", "refresh", "user"}


@contextmanager
def device_priority_session(
    ip: str,
    priority: str = "background",
    queue_timeout_seconds: float | None = None,
    cooldown_seconds: float = 0.4,
):
    """Coordinate all ports of one ASIAIR box during critical write/control windows."""
    gate = _device_gate(ip)
    thread_id = threading.get_ident()
    is_critical = _is_write_priority(priority)
    acquired_critical = False
    queued_critical = False
    queue_deadline = (
        time.monotonic() + queue_timeout_seconds
        if queue_timeout_seconds is not None and queue_timeout_seconds >= 0
        else None
    )

    with gate.condition:
        if gate.critical_owner_thread == thread_id:
            gate.critical_depth += 1
            acquired_critical = True
        else:
            if is_critical:
                gate.pending_critical += 1
                queued_critical = True
            try:
                while True:
                    now = time.monotonic()
                    cooling_down = not is_critical and gate.critical_cooldown_until > now
                    blocked_by_critical = gate.critical_owner_thread is not None
                    blocked_by_pending = not is_critical and gate.pending_critical > 0
                    if not blocked_by_critical and not blocked_by_pending and not cooling_down:
                        break
                    if queue_deadline is not None and now >= queue_deadline:
                        raise TimeoutError(f"ASIAIR device queue busy for {ip}")
                    wait_seconds = max(0.01, gate.critical_cooldown_until - now) if cooling_down else None
                    if queue_deadline is not None:
                        remaining = max(0.01, queue_deadline - now)
                        wait_seconds = min(wait_seconds, remaining) if wait_seconds is not None else remaining
                    gate.condition.wait(wait_seconds)
                if queued_critical:
                    gate.pending_critical -= 1
                    queued_critical = False
                if is_critical:
                    gate.critical_owner_thread = thread_id
                    gate.critical_depth = 1
                    acquired_critical = True
            except Exception:
                if queued_critical:
                    gate.pending_critical -= 1
                    gate.condition.notify_all()
                raise

    try:
        yield
    finally:
        if acquired_critical:
            with gate.condition:
                if gate.critical_owner_thread == thread_id:
                    gate.critical_depth -= 1
                    if gate.critical_depth <= 0:
                        gate.critical_owner_thread = None
                        gate.critical_depth = 0
                        gate.critical_cooldown_until = time.monotonic() + cooldown_seconds
                        gate.condition.notify_all()


@contextmanager
def _endpoint_priority_session(
    ip: str,
    port: int = IMAGER_PORT,
    priority: str = "background",
    queue_timeout_seconds: float | None = None,
):
    """Serialize RPC access per ASIAIR endpoint, letting write sessions outrank polling."""
    gate = _rpc_gate(ip, port)
    thread_id = threading.get_ident()
    is_write = _is_write_priority(priority)
    is_foreground = _is_foreground_priority(priority)
    acquired = False
    queued_write = False
    queued_foreground = False
    queue_deadline = (
        time.monotonic() + queue_timeout_seconds
        if queue_timeout_seconds is not None and queue_timeout_seconds >= 0
        else None
    )

    with gate.condition:
        if gate.owner_thread == thread_id:
            gate.depth += 1
            acquired = True
        else:
            if is_write:
                gate.pending_writes += 1
                queued_write = True
            elif is_foreground:
                gate.pending_foreground += 1
                queued_foreground = True
            try:
                while True:
                    now = time.monotonic()
                    write_cooling_down = not is_write and gate.write_cooldown_until > now
                    blocked_by_write = not is_write and gate.pending_writes > 0
                    blocked_by_foreground = (
                        not is_write
                        and not is_foreground
                        and gate.pending_foreground > 0
                    )
                    if (
                        gate.owner_thread is None
                        and not blocked_by_write
                        and not blocked_by_foreground
                        and not write_cooling_down
                    ):
                        break
                    if queue_deadline is not None and now >= queue_deadline:
                        raise TimeoutError(f"ASIAIR RPC queue busy for {ip}:{port}")
                    wait_seconds = max(0.01, gate.write_cooldown_until - now) if write_cooling_down else None
                    if queue_deadline is not None:
                        remaining = max(0.01, queue_deadline - now)
                        wait_seconds = min(wait_seconds, remaining) if wait_seconds is not None else remaining
                    gate.condition.wait(wait_seconds)
                if queued_write:
                    gate.pending_writes -= 1
                    queued_write = False
                if queued_foreground:
                    gate.pending_foreground -= 1
                    queued_foreground = False
                gate.owner_thread = thread_id
                gate.depth = 1
                acquired = True
            except Exception:
                if queued_write:
                    gate.pending_writes -= 1
                if queued_foreground:
                    gate.pending_foreground -= 1
                if queued_write or queued_foreground:
                    gate.condition.notify_all()
                raise

    try:
        yield
    finally:
        if acquired:
            with gate.condition:
                if gate.owner_thread == thread_id:
                    gate.depth -= 1
                    if gate.depth <= 0:
                        if is_write:
                            gate.write_cooldown_until = time.monotonic() + 0.4
                        gate.owner_thread = None
                        gate.depth = 0
                        gate.condition.notify_all()


@contextmanager
def rpc_priority_session(
    ip: str,
    port: int = IMAGER_PORT,
    priority: str = "background",
    queue_timeout_seconds: float | None = None,
):
    """Coordinate one ASIAIR box and serialize access to the requested endpoint."""
    with device_priority_session(
        ip,
        priority=priority,
        queue_timeout_seconds=queue_timeout_seconds,
    ):
        with _endpoint_priority_session(
            ip,
            port=port,
            priority=priority,
            queue_timeout_seconds=queue_timeout_seconds,
        ):
            yield


@dataclass(frozen=True)
class RpcProbeMethod:
    name: str
    category: str
    risk: str = "RO"
    params: Any | None = None


READONLY_STATUS_METHODS: tuple[RpcProbeMethod, ...] = (
    RpcProbeMethod("pi_is_verified", "system"),
    RpcProbeMethod("pi_get_info", "system"),
    RpcProbeMethod("pi_get_time", "system"),
    RpcProbeMethod("get_app_state", "app"),
    RpcProbeMethod("get_camera_state", "camera"),
    RpcProbeMethod("get_connected_cameras", "camera"),
    RpcProbeMethod("get_camera_info", "camera"),
    RpcProbeMethod("get_controls", "camera"),
    RpcProbeMethod("get_camera_exp_and_bin", "camera"),
    RpcProbeMethod("get_image_save_path", "storage"),
    RpcProbeMethod("get_disk_volume", "storage"),
    RpcProbeMethod("list_mass_storage", "storage"),
    RpcProbeMethod("get_power_supply", "power"),
    RpcProbeMethod("pi_output_get", "power"),
    RpcProbeMethod("pi_output_get2", "power"),
)


READONLY_HARDWARE_METHODS: tuple[RpcProbeMethod, ...] = (
    RpcProbeMethod("scope_get_cap", "mount"),
    RpcProbeMethod("scope_get_ra_dec", "mount"),
    RpcProbeMethod("scope_get_equ_coord", "mount"),
    RpcProbeMethod("scope_get_location", "mount"),
    RpcProbeMethod("scope_get_pierside", "mount"),
    RpcProbeMethod("scope_get_track_state", "mount"),
    RpcProbeMethod("scope_get_track_mode", "mount"),
    RpcProbeMethod("scope_get_slew_rate", "mount"),
    RpcProbeMethod("scope_get_target_pierside", "mount"),
    RpcProbeMethod("scope_is_moving", "mount"),
    RpcProbeMethod("get_connected_focuser", "focuser"),
    RpcProbeMethod("get_focuser_state", "focuser"),
    RpcProbeMethod("get_focuser_position", "focuser", params={"ret_obj": True}),
    RpcProbeMethod("get_focuser_caps", "focuser"),
    RpcProbeMethod("get_connected_wheels", "filter_wheel"),
    RpcProbeMethod("get_wheel_state", "filter_wheel"),
    RpcProbeMethod("get_wheel_position", "filter_wheel"),
    RpcProbeMethod("get_dither", "guiding"),
    RpcProbeMethod("get_flip_calibration", "guiding"),
    RpcProbeMethod("get_stack_info", "stacking"),
    RpcProbeMethod("get_stack_setting", "stacking"),
    RpcProbeMethod("get_solve_result", "solve"),
    RpcProbeMethod("get_last_solve_result", "solve"),
    RpcProbeMethod("get_find_star_result", "analysis"),
    RpcProbeMethod("get_annotate_result", "analysis"),
)


READONLY_EXTENDED_METHODS: tuple[RpcProbeMethod, ...] = (
    RpcProbeMethod("test_connection", "system"),
    RpcProbeMethod("pi_vl805_version", "system"),
    RpcProbeMethod("pi_get_ap", "network"),
    RpcProbeMethod("pi_station_state", "network"),
    RpcProbeMethod("pi_station_list", "network"),
    RpcProbeMethod("pi_eth0_state", "network"),
    RpcProbeMethod("need_reboot", "system"),
    RpcProbeMethod("is_downgraded", "system"),
    RpcProbeMethod("get_camera_bin", "camera"),
    RpcProbeMethod("get_camera_16bit", "camera"),
    RpcProbeMethod("get_subframe", "camera"),
    RpcProbeMethod("get_gain_segment", "camera"),
    RpcProbeMethod("can_liveview", "camera"),
    RpcProbeMethod("can_abort_expose", "camera"),
    RpcProbeMethod("get_img_name_field", "camera"),
    RpcProbeMethod("get_sequence_number", "plans"),
    RpcProbeMethod("get_sequence", "plans"),
    RpcProbeMethod("get_sequence_setting", "plans"),
    RpcProbeMethod("get_target_sequences", "plans"),
    RpcProbeMethod("get_plan", "plans"),
    RpcProbeMethod("get_enabled_plan", "plans"),
    RpcProbeMethod("list_plan", "plans"),
    RpcProbeMethod("get_wheel_slot_name", "filter_wheel"),
    RpcProbeMethod("get_wheel_setting", "filter_wheel"),
    RpcProbeMethod("get_focuser_value", "focuser"),
    RpcProbeMethod("get_focuser_setting", "focuser"),
    RpcProbeMethod("get_merid_delta", "mount"),
    RpcProbeMethod("get_merid_setting", "mount"),
    RpcProbeMethod("get_list", "sky_data"),
    RpcProbeMethod("get_constellations", "sky_data"),
    RpcProbeMethod("get_comet_position", "sky_data"),
    RpcProbeMethod("get_planet_position", "sky_data"),
    RpcProbeMethod("get_calib_frame", "stacking"),
    RpcProbeMethod("get_calib_param", "stacking"),
    RpcProbeMethod("get_rtmp_config", "streaming"),
    RpcProbeMethod("get_batch_stack_setting", "stacking"),
    RpcProbeMethod("get_3p_pa_setting", "polar_align"),
    RpcProbeMethod("get_3p_pa_state", "polar_align"),
    RpcProbeMethod("get_polar_axis", "polar_align"),
    RpcProbeMethod("get_solve_obj", "solve"),
)


def asiair_rpc(
    ip: str,
    method: str,
    params: Any | None = None,
    request_id: int = 1,
    port: int = IMAGER_PORT,
    timeout_seconds: float = 5.0,
    priority: str = "background",
    queue_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {"id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    payload = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\r\n"
    deadline = time.monotonic() + timeout_seconds
    buffer = b""

    normalized_priority = str(priority).lower()
    if queue_timeout_seconds is None and normalized_priority not in {"write", "high", "action"}:
        queue_timeout_seconds = 1.0

    with rpc_priority_session(
        ip,
        port=port,
        priority=priority,
        queue_timeout_seconds=queue_timeout_seconds,
    ):
        with socket.create_connection((ip, port), timeout=timeout_seconds) as sock:
            sock.sendall(payload)
            total_bytes = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_RPC_RESPONSE_BYTES:
                    raise ValueError(
                        f"ASIAIR RPC response from {ip}:{port} {method} exceeded "
                        f"{MAX_RPC_RESPONSE_BYTES} bytes without a matching reply"
                    )
                buffer += chunk
                lines = buffer.split(b"\n")
                buffer = lines.pop()
                for line in lines:
                    if not line.strip():
                        continue
                    message = json.loads(line.decode("utf-8", errors="replace"))
                    if message.get("id") == request_id and message.get("method") == method:
                        return message

    raise TimeoutError(f"ASIAIR RPC timeout for {ip}:{port} {method}")


def asiair_rpc_batch(
    ip: str,
    requests: list[tuple[str, Any]],
    *,
    port: int = IMAGER_PORT,
    timeout_seconds: float = 8.0,
    priority: str = "write",
    queue_timeout_seconds: float | None = None,
    base_request_id: int = 90_000,
) -> list[dict[str, Any] | None]:
    """Send several JSON-RPC requests over one gated connection."""
    if not requests:
        return []

    normalized_priority = str(priority).lower()
    if queue_timeout_seconds is None and normalized_priority not in {"write", "high", "action"}:
        queue_timeout_seconds = 1.0

    reqs: list[dict[str, Any]] = []
    for index, (method, params) in enumerate(requests):
        req: dict[str, Any] = {"id": base_request_id + index, "method": method}
        if params is not None:
            req["params"] = params
        reqs.append(req)
    wanted = {req["id"]: req["method"] for req in reqs}
    results: dict[int, dict[str, Any]] = {}

    deadline = time.monotonic() + timeout_seconds
    with rpc_priority_session(
        ip,
        port=port,
        priority=priority,
        queue_timeout_seconds=queue_timeout_seconds,
    ):
        with socket.create_connection((ip, port), timeout=timeout_seconds) as sock:
            for req in reqs:
                sock.sendall(json.dumps(req, separators=(",", ":")).encode("utf-8") + b"\r\n")
            buffer = b""
            total_bytes = 0
            while len(results) < len(reqs):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_RPC_RESPONSE_BYTES:
                    raise ValueError(
                        f"ASIAIR RPC batch from {ip}:{port} exceeded {MAX_RPC_RESPONSE_BYTES} bytes"
                    )
                buffer += chunk
                lines = buffer.split(b"\n")
                buffer = lines.pop()
                for line in lines:
                    if not line.strip():
                        continue
                    message = json.loads(line.decode("utf-8", errors="replace"))
                    mid = message.get("id")
                    if mid in wanted and message.get("method") == wanted[mid] and mid not in results:
                        results[mid] = message

    return [results.get(req["id"]) for req in reqs]


def asiair_device_rpc_batch(
    device: Device,
    requests: list[tuple[str, Any]],
    *,
    port: int = IMAGER_PORT,
    timeout_seconds: float = 8.0,
    priority: str = "write",
    queue_timeout_seconds: float | None = None,
    base_request_id: int = 90_000,
) -> list[dict[str, Any] | None]:
    errors: list[str] = []
    endpoints = device.endpoint_candidates()
    if _is_write_priority(priority):
        endpoints = endpoints[:1]
    for endpoint in endpoints:
        try:
            responses = asiair_rpc_batch(
                endpoint.ip,
                requests,
                port=port,
                timeout_seconds=timeout_seconds,
                priority=priority,
                queue_timeout_seconds=queue_timeout_seconds,
                base_request_id=base_request_id,
            )
            for response in responses:
                if response is not None:
                    response["_endpoint"] = endpoint.as_dict()
            return responses
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{endpoint.label} {endpoint.ip}: {exc}")
    raise TimeoutError(f"ASIAIR RPC batch failed for {device.name}:{port}; {'; '.join(errors)}")


def asiair_device_rpc(
    device: Device,
    method: str,
    params: Any | None = None,
    request_id: int = 1,
    port: int = IMAGER_PORT,
    timeout_seconds: float = 5.0,
    priority: str = "background",
    queue_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    endpoints = device.endpoint_candidates()
    if _is_write_priority(priority):
        endpoints = endpoints[:1]
    for endpoint in endpoints:
        try:
            response = asiair_rpc(
                endpoint.ip,
                method,
                params=params,
                request_id=request_id,
                port=port,
                timeout_seconds=timeout_seconds,
                priority=priority,
                queue_timeout_seconds=queue_timeout_seconds,
            )
            response["_endpoint"] = endpoint.as_dict()
            return response
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{endpoint.label} {endpoint.ip}: {exc}")
    raise TimeoutError(f"ASIAIR RPC failed for {device.name}:{port} {method}; {'; '.join(errors)}")


def run_probe(
    device: Device,
    methods: tuple[RpcProbeMethod, ...],
    port: int = IMAGER_PORT,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    results: list[dict[str, Any]] = []
    for index, method in enumerate(methods, start=1):
        started = time.perf_counter()
        try:
            response = asiair_device_rpc(
                device,
                method.name,
                params=method.params,
                request_id=index,
                port=port,
                timeout_seconds=timeout_seconds,
            )
            redacted_response = _redact_sensitive(response)
            code = response.get("code")
            results.append(
                {
                    "method": method.name,
                    "category": method.category,
                    "risk": method.risk,
                    "ok": code == 0,
                    "code": code,
                    "seconds": round(time.perf_counter() - started, 3),
                    "endpoint": response.get("_endpoint"),
                    "result": redacted_response.get("result"),
                    "response": redacted_response,
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "method": method.name,
                    "category": method.category,
                    "risk": method.risk,
                    "ok": False,
                    "code": None,
                    "seconds": round(time.perf_counter() - started, 3),
                    "error": str(exc),
                }
            )

    payload = {
        "device": device.name,
        "ip": device.ip,
        "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
        "port": port,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "results": results,
    }
    payload["ok_count"] = sum(1 for item in results if item.get("ok"))
    payload["total_count"] = len(results)
    return payload


def run_preview_preflight(
    device: Device,
    port: int = IMAGER_PORT,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    before_app = asiair_device_rpc(device, "get_app_state", request_id=1, port=port, timeout_seconds=timeout_seconds)
    before_camera = asiair_device_rpc(device, "get_camera_state", request_id=2, port=port, timeout_seconds=timeout_seconds)
    before_exp = asiair_device_rpc(
        device,
        "get_camera_exp_and_bin",
        request_id=3,
        port=port,
        timeout_seconds=timeout_seconds,
    )
    app_result = before_app.get("result")
    camera_result = before_camera.get("result")
    exp_result = before_exp.get("result")
    actions: list[dict[str, Any]] = []
    skipped_reason = None
    after_app = before_app
    after_exp = before_exp
    after_camera = before_camera

    if before_app.get("code") != 0 or not isinstance(app_result, dict) or not app_result.get("page"):
        skipped_reason = "current ASIAIR page is unavailable on this port"
    elif before_camera.get("code") != 0 or not isinstance(camera_result, dict):
        skipped_reason = "camera state is unavailable on this port"
    elif (
        before_exp.get("code") != 0
        or not isinstance(exp_result, dict)
        or exp_result.get("exposure") is None
        or exp_result.get("bin") is None
    ):
        skipped_reason = "camera exposure/bin settings are unavailable on this port"
    else:
        page = app_result["page"]
        exposure = exp_result["exposure"]
        bin_value = exp_result["bin"]
        actions.append(
            _try_rpc_action(
                device,
                "set_page",
                [page],
                request_id=4,
                port=port,
                timeout_seconds=timeout_seconds,
            )
        )
        actions.append(
            _try_rpc_action(
                device,
                "set_camera_exp_and_bin",
                [{"exposure": exposure, "bin": bin_value}],
                request_id=5,
                port=port,
                timeout_seconds=timeout_seconds,
            )
        )

        after_app = asiair_device_rpc(device, "get_app_state", request_id=6, port=port, timeout_seconds=timeout_seconds)
        after_exp = asiair_device_rpc(
            device,
            "get_camera_exp_and_bin",
            request_id=7,
            port=port,
            timeout_seconds=timeout_seconds,
        )
        after_camera = asiair_device_rpc(device, "get_camera_state", request_id=8, port=port, timeout_seconds=timeout_seconds)
    payload = {
        "device": device.name,
        "ip": device.ip,
        "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
        "port": port,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "before": {
            "app": before_app.get("result"),
            "camera": before_camera.get("result"),
            "exposure": before_exp.get("result"),
        },
        "actions": actions,
        "after": {
            "app": after_app.get("result"),
            "camera": after_camera.get("result"),
            "exposure": after_exp.get("result"),
        },
    }
    if skipped_reason:
        payload["skipped_reason"] = skipped_reason
    payload["unchanged"] = {
        "page": before_app.get("result") == after_app.get("result"),
        "camera_state": before_camera.get("result") == after_camera.get("result"),
        "exposure": before_exp.get("result") == after_exp.get("result"),
    }
    payload["ok"] = not skipped_reason and all(item.get("ok") for item in actions) and all(payload["unchanged"].values())
    return payload


def run_preview_shot(
    device: Device,
    exposure_seconds: float = 1.0,
    bin_value: int = 4,
    execute: bool = False,
    keep_autosave_dev: bool = True,
    save_image: bool = True,
    restore_settings: bool = True,
    port: int = IMAGER_PORT,
    timeout_seconds: float = 5.0,
    wait_timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    exposure_us = int(exposure_seconds * 1_000_000)
    before_app = asiair_device_rpc(device, "get_app_state", request_id=1, port=port, timeout_seconds=timeout_seconds)
    before_camera = asiair_device_rpc(device, "get_camera_state", request_id=2, port=port, timeout_seconds=timeout_seconds)
    before_exp = asiair_device_rpc(
        device,
        "get_camera_exp_and_bin",
        request_id=3,
        port=port,
        timeout_seconds=timeout_seconds,
    )
    app_result = before_app.get("result")
    camera_result = before_camera.get("result")
    exp_result = before_exp.get("result")
    preconditions = _preview_action_preconditions(before_app, before_camera, before_exp)
    planned_actions = [
        {"method": "set_page", "params": ["preview"]},
        {"method": "set_camera_exp_and_bin", "params": [{"exposure": exposure_us, "bin": bin_value}]},
        {"method": "start_exposure", "params": {"keep_autosave_dev": keep_autosave_dev}},
    ]
    if save_image:
        planned_actions.append({"method": "save_image", "params": None})
    payload: dict[str, Any] = {
        "device": device.name,
        "ip": device.ip,
        "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
        "port": port,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "execute": execute,
        "request": {
            "exposure_seconds": exposure_seconds,
            "exposure_us": exposure_us,
            "bin": bin_value,
            "keep_autosave_dev": keep_autosave_dev,
            "save_image": save_image,
            "restore_settings": restore_settings,
        },
        "before": {
            "app": app_result,
            "camera": camera_result,
            "exposure": exp_result,
        },
        "preconditions": preconditions,
        "planned_actions": planned_actions,
        "actions": [],
        "restore_actions": [],
        "polls": [],
    }
    if not all(item["ok"] for item in preconditions):
        payload["ok"] = False
        payload["ready"] = False
        return payload

    payload["ready"] = True
    if not execute:
        payload["ok"] = True
        return payload

    previous_page = app_result.get("page") if isinstance(app_result, dict) else None
    previous_exposure = exp_result.get("exposure") if isinstance(exp_result, dict) else None
    previous_bin = exp_result.get("bin") if isinstance(exp_result, dict) else None
    request_id = 4
    actions = payload["actions"]
    try:
        for action in planned_actions:
            if action["method"] == "save_image":
                continue
            actions.append(
                _try_rpc_action(
                    device,
                    action["method"],
                    action["params"],
                    request_id=request_id,
                    port=port,
                    timeout_seconds=timeout_seconds,
                )
            )
            request_id += 1
            if not actions[-1]["ok"]:
                break

        if actions and actions[-1]["method"] == "start_exposure" and actions[-1]["ok"]:
            payload["polls"] = _poll_preview_completion(
                device,
                port=port,
                timeout_seconds=timeout_seconds,
                wait_timeout_seconds=wait_timeout_seconds,
                first_request_id=request_id,
            )
            request_id += len(payload["polls"]) * 2
            if save_image:
                actions.append(
                    _try_rpc_action(
                        device,
                        "save_image",
                        None,
                        request_id=request_id,
                        port=port,
                        timeout_seconds=timeout_seconds,
                    )
                )
                request_id += 1
    finally:
        if restore_settings:
            if previous_exposure is not None and previous_bin is not None:
                payload["restore_actions"].append(
                    _try_rpc_action(
                        device,
                        "set_camera_exp_and_bin",
                        [{"exposure": previous_exposure, "bin": previous_bin}],
                        request_id=request_id,
                        port=port,
                        timeout_seconds=timeout_seconds,
                    )
                )
                request_id += 1
            if previous_page:
                payload["restore_actions"].append(
                    _try_rpc_action(
                        device,
                        "set_page",
                        [previous_page],
                        request_id=request_id,
                        port=port,
                        timeout_seconds=timeout_seconds,
                    )
                )

    after_app = asiair_device_rpc(device, "get_app_state", request_id=request_id + 1, port=port, timeout_seconds=timeout_seconds)
    after_camera = asiair_device_rpc(device, "get_camera_state", request_id=request_id + 2, port=port, timeout_seconds=timeout_seconds)
    after_exp = asiair_device_rpc(
        device,
        "get_camera_exp_and_bin",
        request_id=request_id + 3,
        port=port,
        timeout_seconds=timeout_seconds,
    )
    payload["after"] = {
        "app": after_app.get("result"),
        "camera": after_camera.get("result"),
        "exposure": after_exp.get("result"),
    }
    payload["finished_at"] = datetime.now().isoformat(timespec="seconds")
    payload["ok"] = all(item.get("ok") for item in actions) and all(
        item.get("ok") for item in payload["restore_actions"]
    )
    return payload


def build_write_read_plan(
    device: Device,
    test_prefix: str = "asiairbridge_test",
    image_path: str | None = None,
    port: int = IMAGER_PORT,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    reads: list[dict[str, Any]] = []
    request_id = 1

    def read(method: str, params: Any | None = None) -> Any:
        nonlocal request_id
        started = time.perf_counter()
        try:
            response = asiair_device_rpc(
                device,
                method,
                params=params,
                request_id=request_id,
                port=port,
                timeout_seconds=timeout_seconds,
            )
            request_id += 1
            redacted = _redact_sensitive(response)
            reads.append(
                {
                    "method": method,
                    "params": _redact_sensitive(params),
                    "ok": response.get("code") == 0,
                    "code": response.get("code"),
                    "seconds": round(time.perf_counter() - started, 3),
                    "endpoint": response.get("_endpoint"),
                    "result": redacted.get("result"),
                }
            )
            return redacted.get("result") if response.get("code") == 0 else None
        except Exception as exc:  # noqa: BLE001
            request_id += 1
            reads.append(
                {
                    "method": method,
                    "params": _redact_sensitive(params),
                    "ok": False,
                    "code": None,
                    "seconds": round(time.perf_counter() - started, 3),
                    "error": str(exc),
                }
            )
            return None

    lists = read("get_list")
    plans = read("list_plan")
    sequence_number = read("get_sequence_number")
    sequence_zero = read("get_sequence", [0])
    image_info = read("get_img_file_info", [image_path]) if image_path else None

    list_names = [item.get("name") for item in lists if isinstance(item, dict)] if isinstance(lists, list) else []
    test_list = test_prefix
    renamed_list = f"{test_prefix}_renamed"
    object_name = f"{test_prefix}_object"

    workflows: list[dict[str, Any]] = [
        {
            "name": "object_list_create_write_read",
            "status": "dry_run_only",
            "writes_are_sent": False,
            "precheck": {
                "existing_lists": list_names,
                "test_list_exists": test_list in list_names,
            },
            "steps": [
                {
                    "phase": "create",
                    "method": "add_list",
                    "params": [test_list],
                    "risk": "WRITE",
                    "expected_verify": "get_list should include the new list name",
                },
                {
                    "phase": "rename",
                    "method": "rename_list",
                    "params": [test_list, renamed_list],
                    "risk": "WRITE",
                    "expected_verify": "get_list should include the renamed list name",
                },
                {
                    "phase": "write",
                    "method": "add_obj",
                    "params": [
                        renamed_list,
                        {
                            "name": object_name,
                            "ra": 0.0,
                            "dec": 0.0,
                            "note": "asiairbridge dry-run test object",
                        },
                    ],
                    "risk": "WRITE",
                    "params_status": "candidate_shape_unverified",
                    "expected_verify": "get_obj should return the test object under the renamed list",
                },
                {
                    "phase": "read_verify",
                    "method": "get_obj",
                    "params": [renamed_list],
                    "risk": "RO",
                },
                {
                    "phase": "cleanup_candidate",
                    "method": "del_list",
                    "params": [renamed_list],
                    "risk": "DANGER",
                    "execute_policy": "manual-confirmation-only",
                },
            ],
        },
        {
            "name": "plan_sequence_read_boundary",
            "status": "read_only_boundary_defined",
            "writes_are_sent": False,
            "baseline": {
                "sequence_number": sequence_number,
                "sequence_zero": sequence_zero,
                "plans": plans,
            },
            "deferred_writes": [
                "set_sequence",
                "set_sequence_setting",
                "set_plan",
                "import_plan",
            ],
            "reason": "Do not touch production plans or target sequences without an isolated test plan design.",
        },
    ]

    if image_path:
        workflows.append(
            {
                "name": "image_metadata_write_read",
                "status": "dry_run_only",
                "writes_are_sent": False,
                "precheck": {
                    "image_path": image_path,
                    "image_info_available": image_info is not None,
                },
                "steps": [
                    {
                        "phase": "read_before",
                        "method": "get_img_file_info",
                        "params": [image_path],
                        "risk": "RO",
                    },
                    {
                        "phase": "metadata_noop_candidate",
                        "method": "set_img_file_info",
                        "params": [image_path, image_info],
                        "risk": "WRITE",
                        "params_status": "candidate_shape_unverified",
                        "expected_verify": "get_img_file_info should remain unchanged",
                    },
                    {
                        "phase": "rename_deferred",
                        "method": "file_rename",
                        "params": [image_path, image_path],
                        "risk": "WRITE",
                        "execute_policy": "deferred; no-op rename shape is unverified",
                    },
                ],
            }
        )

    return {
        "device": device.name,
        "ip": device.ip,
        "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
        "port": port,
        "profile": "i-write-read-dry-run",
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "execute": False,
        "writes_are_sent": False,
        "test_prefix": test_prefix,
        "image_path": image_path,
        "reads": reads,
        "workflows": workflows,
        "ok": all(item.get("ok") for item in reads),
    }


def _preview_action_preconditions(
    app_response: dict[str, Any],
    camera_response: dict[str, Any],
    exposure_response: dict[str, Any],
) -> list[dict[str, Any]]:
    app_result = app_response.get("result")
    camera_result = camera_response.get("result")
    exp_result = exposure_response.get("result")
    return [
        {
            "check": "imager_app_state_available",
            "ok": app_response.get("code") == 0 and isinstance(app_result, dict) and bool(app_result.get("page")),
            "detail": compact_result(app_result),
        },
        {
            "check": "camera_state_idle",
            "ok": camera_response.get("code") == 0
            and isinstance(camera_result, dict)
            and camera_result.get("state") == "idle",
            "detail": compact_result(camera_result),
        },
        {
            "check": "camera_exposure_available",
            "ok": exposure_response.get("code") == 0
            and isinstance(exp_result, dict)
            and exp_result.get("exposure") is not None
            and exp_result.get("bin") is not None,
            "detail": compact_result(exp_result),
        },
    ]


def _poll_preview_completion(
    device: Device,
    port: int,
    timeout_seconds: float,
    wait_timeout_seconds: float,
    first_request_id: int,
) -> list[dict[str, Any]]:
    polls: list[dict[str, Any]] = []
    deadline = time.monotonic() + wait_timeout_seconds
    request_id = first_request_id
    while time.monotonic() < deadline:
        app = asiair_device_rpc(device, "get_app_state", request_id=request_id, port=port, timeout_seconds=timeout_seconds)
        request_id += 1
        camera = asiair_device_rpc(
            device,
            "get_camera_state",
            request_id=request_id,
            port=port,
            timeout_seconds=timeout_seconds,
        )
        request_id += 1
        camera_result = camera.get("result")
        app_result = app.get("result")
        poll = {
            "seconds_since_epoch": time.time(),
            "app": app_result,
            "camera": camera_result,
        }
        polls.append(poll)
        if isinstance(camera_result, dict) and camera_result.get("state") == "idle":
            capture_state = app_result.get("capture", {}).get("state") if isinstance(app_result, dict) else None
            if capture_state in (None, "idle", "complete"):
                break
        time.sleep(1)
    return polls


def _try_rpc_action(
    device: Device,
    method: str,
    params: Any,
    request_id: int,
    port: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = asiair_device_rpc(
            device,
            method,
            params=params,
            request_id=request_id,
            port=port,
            timeout_seconds=timeout_seconds,
            priority="write",
        )
        return {
            "method": method,
            "params": params,
            "ok": response.get("code") == 0,
            "code": response.get("code"),
            "seconds": round(time.perf_counter() - started, 3),
            "endpoint": response.get("_endpoint"),
            "response": _redact_sensitive(response),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "method": method,
            "params": params,
            "ok": False,
            "code": None,
            "seconds": round(time.perf_counter() - started, 3),
            "error": str(exc),
        }


def write_probe_report(config: AppConfig, payload: dict[str, Any], name: str) -> Path:
    reports_dir = config.state_path() / "rpc-probes"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = reports_dir / f"{timestamp}_{payload['device']}_{name}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def _redact_sensitive(value: Any) -> Any:
    sensitive_keys = {"passwd", "password", "passphrase", "psk", "secret", "token", "auth"}
    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if key.lower() in sensitive_keys else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def compact_result(value: Any, limit: int = 140) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."
