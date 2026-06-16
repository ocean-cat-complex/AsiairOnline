from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import AppConfig, Device
from .monitor import collect_network_stats, read_lock
from .probe import tcp_open
from .rpc import GUIDER_PORT, IMAGER_PORT, _redact_sensitive, asiair_device_rpc


@dataclass(frozen=True)
class MonitorCall:
    method: str
    category: str
    label: str
    params: Any | None = None
    port: int = IMAGER_PORT
    interval_seconds: float = 10.0
    timeout_seconds: float = 1.5
    priority: int = 50

    @property
    def key(self) -> str:
        params = json.dumps(self.params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"{self.port}:{self.method}:{params}"


MONITOR_CALLS: tuple[MonitorCall, ...] = (
    MonitorCall("test_connection", "system", "连接测试", interval_seconds=10, priority=8),
    MonitorCall("get_app_state", "app", "当前页面、拍摄进度、目标/序列和工作流状态", interval_seconds=1, priority=1),
    MonitorCall("get_camera_state", "camera", "主相机连接和工作状态", interval_seconds=3, priority=10),
    MonitorCall("get_camera_exp_and_bin", "camera", "曝光和 Bin", interval_seconds=3, priority=10),
    MonitorCall("get_control_value", "camera", "传感器温度", ["Temperature"], interval_seconds=3, priority=10),
    MonitorCall("get_control_value", "camera", "制冷功率", ["CoolPowerPerc"], interval_seconds=3, priority=10),
    MonitorCall("get_control_value", "camera", "制冷开关", ["CoolerOn"], interval_seconds=5, priority=12),
    MonitorCall("get_disk_volume", "storage", "存储容量", interval_seconds=10, priority=20),
    MonitorCall("get_power_supply", "power", "供电输入状态", interval_seconds=5, priority=20),
    MonitorCall("pi_output_get2", "power", "电源输出状态", interval_seconds=5, priority=4),
    MonitorCall("get_device_state", "app", "设备综合状态快照", interval_seconds=10, timeout_seconds=2, priority=5),
    MonitorCall("pi_is_verified", "system", "设备验证状态", interval_seconds=30, priority=10),
    MonitorCall("pi_get_info", "system", "盒子系统信息", interval_seconds=30, timeout_seconds=2, priority=10),
    MonitorCall("pi_get_time", "system", "盒子系统时间", interval_seconds=10, priority=10),
    MonitorCall("need_reboot", "system", "是否需要重启", interval_seconds=30, priority=10),
    MonitorCall("is_downgraded", "system", "降级状态", interval_seconds=30, priority=10),
    MonitorCall("get_connected_cameras", "camera", "已连接相机", interval_seconds=10, priority=15),
    MonitorCall("get_camera_info", "camera", "相机规格", interval_seconds=30, priority=15),
    MonitorCall("get_controls", "camera", "相机控制项和范围", interval_seconds=30, timeout_seconds=2, priority=15),
    MonitorCall("get_camera_bin", "camera", "Bin 状态", interval_seconds=5, priority=15),
    MonitorCall("get_camera_16bit", "camera", "16-bit 状态", interval_seconds=10, priority=15),
    MonitorCall("get_subframe", "camera", "ROI / 子画幅", interval_seconds=10, priority=15),
    MonitorCall("get_gain_segment", "camera", "增益段", interval_seconds=30, priority=15),
    MonitorCall("can_liveview", "camera", "是否允许实时预览", interval_seconds=10, priority=15),
    MonitorCall("can_abort_expose", "camera", "是否允许中止曝光", interval_seconds=5, priority=15),
    MonitorCall("get_img_name_field", "camera", "图像命名字段", interval_seconds=30, priority=15),
    MonitorCall("get_control_value", "camera", "Gain", ["Gain"], interval_seconds=5, priority=16),
    MonitorCall("get_control_value", "camera", "Exposure", ["Exposure"], interval_seconds=5, priority=16),
    MonitorCall("get_control_value", "camera", "Offset", ["Offset"], interval_seconds=10, priority=16),
    MonitorCall("get_control_value", "camera", "目标温度", ["TargetTemp"], interval_seconds=10, priority=16),
    MonitorCall("get_control_value", "camera", "防结露加热", ["AntiDewHeater"], interval_seconds=10, priority=16),
    MonitorCall("get_control_value", "camera", "硬件 Bin 标志", ["HardwareBin"], interval_seconds=10, priority=16),
    MonitorCall("get_control_value", "camera", "画幅模式", ["FrameSize"], interval_seconds=10, priority=16),
    MonitorCall("get_control_value", "camera", "USB 带宽", ["Bandwidth"], interval_seconds=15, priority=16),
    MonitorCall("get_control_value", "camera", "半速风扇", ["FanHalfSpeed"], interval_seconds=15, priority=16),
    MonitorCall("get_control_value", "camera", "指示灯", ["LedOn"], interval_seconds=15, priority=16),
    MonitorCall("get_image_save_path", "storage", "图像保存路径", interval_seconds=15, priority=20),
    MonitorCall("list_mass_storage", "storage", "已挂载存储设备", interval_seconds=15, priority=20),
    MonitorCall("can_format_emmc", "storage", "eMMC 格式化能力查询", interval_seconds=60, priority=20),
    MonitorCall("get_img_file_page_number", "files", "图像浏览器页数", interval_seconds=30, priority=20),
    MonitorCall("pi_output_get", "power", "旧版电源输出状态", interval_seconds=10, priority=21),
    MonitorCall("pi_get_ap", "network", "热点/AP 状态", interval_seconds=30, timeout_seconds=2, priority=25),
    MonitorCall("pi_station_state", "network", "Wi-Fi station 状态", interval_seconds=30, timeout_seconds=2, priority=25),
    MonitorCall("pi_station_list", "network", "Wi-Fi station 列表", interval_seconds=60, timeout_seconds=2, priority=25),
    MonitorCall("pi_eth0_state", "network", "以太网状态", interval_seconds=30, timeout_seconds=2, priority=25),
    MonitorCall("get_sequence_number", "plans", "序列数量", interval_seconds=15, priority=30),
    MonitorCall("get_sequence", "plans", "当前序列槽位 0", [0], interval_seconds=15, priority=30),
    MonitorCall("get_sequence_setting", "plans", "序列设置", interval_seconds=30, priority=30),
    MonitorCall("get_target_sequences", "plans", "当前目标序列", interval_seconds=15, timeout_seconds=2, priority=30),
    MonitorCall("get_plan", "plans", "计划内容", interval_seconds=60, timeout_seconds=2, priority=30),
    MonitorCall("get_enabled_plan", "plans", "启用中的计划", interval_seconds=15, timeout_seconds=2, priority=30),
    MonitorCall("list_plan", "plans", "计划列表", interval_seconds=60, priority=30),
    MonitorCall("get_connected_focuser", "focuser", "已连接调焦器", interval_seconds=15, priority=35),
    MonitorCall("get_focuser_state", "focuser", "调焦器状态", interval_seconds=5, priority=35),
    MonitorCall("get_focuser_position", "focuser", "调焦器位置", {"ret_obj": True}, interval_seconds=5, priority=35),
    MonitorCall("get_focuser_caps", "focuser", "调焦器能力", interval_seconds=30, priority=35),
    MonitorCall("get_focuser_value", "focuser", "调焦器温度", ["temperature"], interval_seconds=5, priority=35),
    MonitorCall("get_focuser_setting", "focuser", "调焦器设置", interval_seconds=30, priority=35),
    MonitorCall("get_connected_wheels", "filter_wheel", "已连接滤轮", interval_seconds=15, priority=36),
    MonitorCall("get_wheel_state", "filter_wheel", "滤轮状态", interval_seconds=5, priority=36),
    MonitorCall("get_wheel_position", "filter_wheel", "滤轮位置", interval_seconds=5, priority=36),
    MonitorCall("get_wheel_slot_name", "filter_wheel", "滤轮槽位名称", interval_seconds=30, priority=36),
    MonitorCall("get_wheel_setting", "filter_wheel", "滤轮设置", interval_seconds=30, priority=36),
    MonitorCall("scope_get_ra_dec", "mount", "RA / Dec 指向", port=GUIDER_PORT, interval_seconds=3, priority=37),
    MonitorCall("scope_get_track_state", "mount", "跟踪开关", port=GUIDER_PORT, interval_seconds=3, priority=37),
    MonitorCall("scope_get_track_mode", "mount", "跟踪模式", port=GUIDER_PORT, interval_seconds=5, priority=38),
    MonitorCall("scope_get_slew_rate", "mount", "手动移动速率", port=GUIDER_PORT, interval_seconds=5, priority=38),
    MonitorCall("scope_is_moving", "mount", "移动状态", port=GUIDER_PORT, interval_seconds=3, priority=38),
    MonitorCall("scope_get_pierside", "mount", "赤道仪方位", port=GUIDER_PORT, interval_seconds=10, priority=39),
    MonitorCall("scope_get_location", "mount", "经纬度", port=GUIDER_PORT, interval_seconds=30, priority=39),
    MonitorCall("scope_get_cap", "mount", "赤道仪能力", port=GUIDER_PORT, interval_seconds=30, priority=39),
    MonitorCall("get_dither", "guiding", "Dither 设置", interval_seconds=20, priority=40),
    MonitorCall("get_flip_calibration", "guiding", "翻转校准状态", port=GUIDER_PORT, interval_seconds=20, priority=40),
    MonitorCall("get_stack_info", "stacking", "叠加信息", interval_seconds=15, priority=45),
    MonitorCall("get_stack_setting", "stacking", "叠加设置", interval_seconds=30, priority=45),
    MonitorCall("get_batch_stack_setting", "stacking", "批量叠加设置", interval_seconds=30, priority=45),
    MonitorCall("get_calib_frame", "stacking", "校准帧配置", interval_seconds=30, priority=45),
    MonitorCall("get_calib_param", "stacking", "Dark 校准参数", ["dark"], interval_seconds=30, priority=45),
    MonitorCall("get_solve_result", "solve", "当前解算结果", interval_seconds=10, priority=50),
    MonitorCall("get_last_solve_result", "solve", "最近解算结果", interval_seconds=15, priority=50),
    MonitorCall("get_solve_obj", "solve", "解算对象", interval_seconds=30, priority=50),
    MonitorCall("get_find_star_result", "analysis", "星点检测结果", interval_seconds=10, priority=51),
    MonitorCall("get_annotate_result", "analysis", "标注结果", interval_seconds=15, priority=51),
    MonitorCall("get_3p_pa_setting", "polar_align", "三点极轴设置", interval_seconds=30, priority=55),
    MonitorCall("get_3p_pa_state", "polar_align", "三点极轴状态", interval_seconds=15, priority=55),
    MonitorCall("get_polar_align_image", "polar_align", "极轴对齐图像状态", interval_seconds=30, priority=55),
    MonitorCall("get_list", "sky_data", "天体列表", interval_seconds=120, timeout_seconds=2, priority=80),
    MonitorCall("get_constellations", "sky_data", "星座数据", interval_seconds=300, timeout_seconds=2, priority=80),
    MonitorCall("get_comet_position", "sky_data", "彗星位置", interval_seconds=300, timeout_seconds=2, priority=80),
    MonitorCall("get_planet_position", "sky_data", "行星位置", interval_seconds=300, timeout_seconds=2, priority=80),
    MonitorCall("get_obj", "sky_data", "My Favorites 对象", ["My Favorites"], interval_seconds=120, timeout_seconds=2, priority=80),
    MonitorCall("get_rtmp_config", "streaming", "RTMP 配置", interval_seconds=30, priority=65),
    MonitorCall("get_app_setting", "settings", "App 设置", interval_seconds=60, timeout_seconds=2, priority=70),
    MonitorCall("get_test_setting", "settings", "测试设置", interval_seconds=60, timeout_seconds=2, priority=70),
)

CATEGORY_LABELS = {
    "app": "App / 运行状态",
    "system": "系统",
    "camera": "相机",
    "storage": "存储",
    "files": "文件浏览",
    "power": "电源",
    "network": "网络",
    "plans": "计划 / 序列",
    "focuser": "调焦器",
    "filter_wheel": "滤轮",
    "guiding": "导星",
    "mount": "赤道仪",
    "solve": "解算",
    "analysis": "星点 / 标注",
    "stacking": "叠加 / 校准",
    "polar_align": "极轴",
    "sky_data": "天体数据",
    "streaming": "直播",
    "settings": "设置",
}

CATEGORY_ORDER = [
    "app",
    "system",
    "camera",
    "storage",
    "files",
    "power",
    "network",
    "plans",
    "focuser",
    "filter_wheel",
    "guiding",
    "mount",
    "solve",
    "analysis",
    "stacking",
    "polar_align",
    "sky_data",
    "streaming",
    "settings",
]

MAX_INITIAL_DISPLAY_ITEMS = 30
MAX_DICT_KEYS = 36
MAX_STRING = 1200
BACKGROUND_REFRESH_WORKERS = 2
MANUAL_REFRESH_WORKERS = 4
BACKGROUND_REFRESH_BATCH_SIZE = 16
BACKGROUND_STALE_RETRY_LIMIT = 4
BACKGROUND_COLD_RETRY_LIMIT = 3
FAILED_RETRY_BASE_SECONDS = 20.0
FAILED_RETRY_MAX_SECONDS = 300.0
LINK_PROBE_INTERVAL_SECONDS = 1.0
LINK_PING_TIMEOUT_MS = 450
LINK_TCP_TIMEOUT_SECONDS = 0.45


def rpc_monitor_response(
    server: Any,
    device_name: str | None = None,
    force: bool = False,
    focus_category: str | None = None,
) -> dict[str, Any]:
    device = _select_device(server.config, device_name)
    with server.rpc_monitor_lock:
        server.rpc_monitor_active_devices.add(device.name)
        if force:
            server.rpc_monitor_force_refresh.add(device.name)
            server.rpc_monitor_refreshing.add(device.name)
            server.rpc_monitor_force_progress[device.name] = _new_force_progress()
    return _build_snapshot(server, device)


def init_rpc_monitor_state(server: Any) -> None:
    server.rpc_monitor_cache = _load_monitor_cache(server)
    server.rpc_monitor_attempts = {}
    server.rpc_monitor_link_cache = {}
    server.rpc_monitor_refreshing = set()
    server.rpc_monitor_force_refresh = set()
    server.rpc_monitor_force_progress = {}
    server.rpc_monitor_active_devices = {device.name for device in server.config.enabled_devices()}
    server.rpc_monitor_lock = threading.Lock()
    threading.Thread(
        target=_realtime_scheduler_loop,
        args=(server,),
        name="rpc-monitor-realtime",
        daemon=True,
    ).start()
    threading.Thread(
        target=_background_scheduler_loop,
        args=(server,),
        name="rpc-monitor-background",
        daemon=True,
    ).start()


def _realtime_scheduler_loop(server: Any) -> None:
    while True:
        for device in _active_devices(server):
            _refresh_realtime_now(server, device)
        time.sleep(0.25)


def _background_scheduler_loop(server: Any) -> None:
    while True:
        did_work = False
        for device in _active_devices(server):
            force = _consume_force_refresh(server, device)
            calls = _due_calls(server, device, force)
            if not calls:
                if force:
                    _finish_force_progress(server, device)
                continue
            did_work = True
            with server.rpc_monitor_lock:
                server.rpc_monitor_refreshing.add(device.name)
            try:
                workers = MANUAL_REFRESH_WORKERS if force else BACKGROUND_REFRESH_WORKERS
                _run_calls_parallel(server, device, calls, workers, track_progress=force)
            finally:
                if force:
                    _finish_force_progress(server, device)
                with server.rpc_monitor_lock:
                    server.rpc_monitor_refreshing.discard(device.name)
        time.sleep(0.25 if did_work else 0.75)


def _active_devices(server: Any) -> list[Device]:
    with server.rpc_monitor_lock:
        active = set(server.rpc_monitor_active_devices)
    if not active:
        return list(server.config.enabled_devices())
    return [device for device in server.config.enabled_devices() if device.name in active]


def _consume_force_refresh(server: Any, device: Device) -> bool:
    with server.rpc_monitor_lock:
        if device.name not in server.rpc_monitor_force_refresh:
            return False
        server.rpc_monitor_force_refresh.discard(device.name)
        return True


def _select_device(config: AppConfig, device_name: str | None) -> Device:
    devices = config.enabled_devices()
    if device_name:
        for device in devices:
            if device.name == device_name:
                return device
    return config.default_device()


def _refresh_realtime_now(server: Any, device: Device, force: bool = False) -> None:
    now = time.monotonic()
    calls = [call for call in MONITOR_CALLS if call.interval_seconds <= 1.0]
    for index, call in enumerate(calls, start=10_000):
        with server.rpc_monitor_lock:
            entry = server.rpc_monitor_cache.get(_cache_key(device, call))
            last = float(entry.get("_monotonic", 0.0)) if entry else 0.0
            is_due = force or entry is None or now - last >= call.interval_seconds
        if not is_due:
            continue
        result = _run_call(device, call, index)
        _store_monitor_result(server, device, call, result)


def _run_calls_parallel(
    server: Any,
    device: Device,
    calls: list[MonitorCall],
    workers: int,
    track_progress: bool = False,
) -> None:
    if not calls:
        return
    if track_progress:
        _start_force_progress(server, device, len(calls))
    worker_count = max(1, min(workers, len(calls)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=f"rpc-monitor-{device.name}") as executor:
        futures = {
            executor.submit(_run_call, device, call, index): call
            for index, call in enumerate(calls, start=1)
        }
        for future in as_completed(futures):
            call = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = {
                    "_monotonic": time.monotonic(),
                    "device": device.name,
                    "ip": device.ip,
                    "port": call.port,
                    "method": call.method,
                    "params": call.params,
                    "category": call.category,
                    "label": call.label,
                    "interval_seconds": call.interval_seconds,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "ok": False,
                    "code": None,
                    "seconds": None,
                    "result": None,
                    "display_value": "不可用",
                    "display_detail": "",
                    "display_rows": [],
                    "display_table": None,
                    "display_wide": False,
                    "error": str(exc),
                }
            _store_monitor_result(server, device, call, result, track_progress=track_progress, progress_total=len(calls))


def _store_monitor_result(
    server: Any,
    device: Device,
    call: MonitorCall,
    result: dict[str, Any],
    track_progress: bool = False,
    progress_total: int = 0,
) -> None:
    key = _cache_key(device, call)
    with server.rpc_monitor_lock:
        attempts = getattr(server, "rpc_monitor_attempts", {})
        previous_attempt = attempts.get(key) if isinstance(attempts, dict) else None
        previous_failures = _attempt_failure_count(previous_attempt)
        is_ok = bool(result.get("ok"))
        consecutive_failures = 0 if is_ok else previous_failures + 1
        updated_at = result.get("updated_at") or datetime.now().isoformat(timespec="seconds")
        attempts[key] = {
            "_monotonic": time.monotonic(),
            "updated_at": updated_at,
            "ok": is_ok,
            "error": result.get("error"),
            "code": result.get("code"),
            "consecutive_failures": consecutive_failures,
            "last_success_at": updated_at if is_ok else (
                previous_attempt.get("last_success_at") if isinstance(previous_attempt, dict) else None
            ),
            "last_failure_at": None if is_ok else updated_at,
        }
        server.rpc_monitor_attempts = attempts
        previous = server.rpc_monitor_cache.get(key)
        stored = _merge_monitor_entry(previous, result)
        if stored is None:
            server.rpc_monitor_cache.pop(key, None)
        else:
            server.rpc_monitor_cache[key] = stored
        if track_progress:
            progress = server.rpc_monitor_force_progress.setdefault(device.name, _new_force_progress())
            progress["done"] = min(int(progress.get("done", 0)) + 1, int(progress.get("total", progress_total)))
            progress["ok"] = int(progress.get("ok", 0)) + (1 if result.get("ok") else 0)
            progress["fail"] = int(progress.get("fail", 0)) + (0 if result.get("ok") else 1)
            progress["current"] = call.label
            progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _persist_monitor_cache(server, device)


def _merge_monitor_entry(previous: dict[str, Any] | None, result: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("ok"):
        merged = dict(result)
        merged.pop("last_error", None)
        merged.pop("last_error_at", None)
        merged.pop("last_error_seconds", None)
        merged["stale"] = False
        return merged

    if not previous or not previous.get("ok") or not previous.get("updated_at") or previous.get("pending"):
        return None

    preserved = dict(previous)
    preserved["_monotonic"] = time.monotonic()
    preserved["stale"] = True
    preserved["last_error"] = result.get("error") or (
        f"code {result.get('code')}" if result.get("code") is not None else "read failed"
    )
    preserved["last_error_at"] = result.get("updated_at") or datetime.now().isoformat(timespec="seconds")
    preserved["last_error_seconds"] = result.get("seconds")
    return preserved


def _load_monitor_cache(server: Any) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    for device in server.config.enabled_devices():
        path = _monitor_cache_path(server, device)
        loaded = False
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if isinstance(entries, dict):
                for key, entry in entries.items():
                    if isinstance(entry, dict) and entry.get("ok"):
                        entry.pop("_monotonic", None)
                        cache[str(key)] = entry
                loaded = True
        if not loaded:
            cache.update(_load_legacy_monitor_snapshot(server, device))
    return cache


def _load_legacy_monitor_snapshot(server: Any, device: Device) -> dict[str, dict[str, Any]]:
    path = server.config.state_path() / f"rpc-monitor-{device.name}.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    categories = payload.get("categories") if isinstance(payload, dict) else None
    if not isinstance(categories, list):
        return {}

    calls = {
        (call.port, call.method, json.dumps(call.params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))): call
        for call in MONITOR_CALLS
    }
    cache: dict[str, dict[str, Any]] = {}
    for category in categories:
        if not isinstance(category, dict):
            continue
        for item in category.get("items") or []:
            if not isinstance(item, dict) or not item.get("updated_at") or not item.get("ok"):
                continue
            params = json.dumps(item.get("params"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            call = calls.get((int(item.get("port") or IMAGER_PORT), str(item.get("method")), params))
            if call is None:
                continue
            item.pop("_monotonic", None)
            cache[_cache_key(device, call)] = item
    return cache


def _persist_monitor_cache(server: Any, device: Device) -> None:
    prefix = f"{device.name}:"
    with server.rpc_monitor_lock:
        entries = {
            key: _monitor_entry_for_disk(value)
            for key, value in server.rpc_monitor_cache.items()
            if key.startswith(prefix)
        }
    payload = {
        "device": {"name": device.name, "ip": device.ip},
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "entries": entries,
    }
    path = _monitor_cache_path(server, device)
    tmp_path = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    except OSError:
        return


def _monitor_cache_path(server: Any, device: Device) -> Any:
    return server.config.state_path() / "rpc-monitor-cache" / f"{device.name}.json"


def _monitor_entry_for_disk(entry: dict[str, Any]) -> dict[str, Any]:
    payload = dict(entry)
    payload.pop("_monotonic", None)
    return payload


def _new_force_progress(total: int | None = None) -> dict[str, Any]:
    return {
        "active": True,
        "total": total if total is not None else len([call for call in MONITOR_CALLS if call.interval_seconds > 1.0]),
        "done": 0,
        "ok": 0,
        "fail": 0,
        "current": "",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": None,
        "finished_at": None,
    }


def _start_force_progress(server: Any, device: Device, total: int) -> None:
    with server.rpc_monitor_lock:
        progress = server.rpc_monitor_force_progress.setdefault(device.name, _new_force_progress(total))
        progress.update(
            {
                "active": True,
                "total": total,
                "done": 0,
                "ok": 0,
                "fail": 0,
                "current": "开始刷新详情",
                "started_at": progress.get("started_at") or datetime.now().isoformat(timespec="seconds"),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
            }
        )


def _finish_force_progress(server: Any, device: Device) -> None:
    with server.rpc_monitor_lock:
        progress = server.rpc_monitor_force_progress.get(device.name)
        if not progress:
            return
        progress["active"] = False
        progress["done"] = min(int(progress.get("done", 0)), int(progress.get("total", 0)))
        progress["current"] = "详情刷新完成"
        progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
        progress["finished_at"] = datetime.now().isoformat(timespec="seconds")


def _attempt_failure_count(attempt: Any) -> int:
    if not isinstance(attempt, dict):
        return 0
    try:
        return max(0, int(attempt.get("consecutive_failures") or 0))
    except (TypeError, ValueError):
        return 0


def _retry_delay_seconds(call: MonitorCall, failures: int, has_success: bool) -> float:
    if failures <= 0:
        return 0.0
    if call.interval_seconds <= 1.0:
        return 0.0
    if has_success:
        return 0.0
    exponent = min(failures - 1, 5)
    base = max(float(call.interval_seconds), FAILED_RETRY_BASE_SECONDS)
    return min(base * (2 ** exponent), FAILED_RETRY_MAX_SECONDS)


def _next_retry_seconds(now: float, call: MonitorCall, attempt: Any, has_success: bool) -> float:
    failures = _attempt_failure_count(attempt)
    if failures <= 0 or not isinstance(attempt, dict):
        return 0.0
    try:
        last_attempt = float(attempt.get("_monotonic") or 0.0)
    except (TypeError, ValueError):
        last_attempt = 0.0
    if last_attempt <= 0:
        return 0.0
    return max(0.0, _retry_delay_seconds(call, failures, has_success) - (now - last_attempt))


def _due_calls(
    server: Any,
    device: Device,
    force: bool,
) -> list[MonitorCall]:
    now = time.monotonic()
    with server.rpc_monitor_lock:
        cache = server.rpc_monitor_cache
        attempts = getattr(server, "rpc_monitor_attempts", {})
        healthy_due: list[tuple[int, float, MonitorCall]] = []
        stale_due: list[tuple[int, float, MonitorCall]] = []
        cold_due: list[tuple[int, float, MonitorCall]] = []
        for call in MONITOR_CALLS:
            if call.interval_seconds <= 1.0:
                continue
            key = _cache_key(device, call)
            entry = cache.get(key)
            last = float(entry.get("_monotonic", 0.0)) if entry else 0.0
            attempt = attempts.get(key) if isinstance(attempts, dict) else None
            last_attempt = float(attempt.get("_monotonic", 0.0)) if isinstance(attempt, dict) else 0.0
            has_success = bool(entry and entry.get("ok"))
            failures = _attempt_failure_count(attempt)
            if force:
                healthy_due.append((call.priority, last, call))
            elif has_success:
                if now - last < call.interval_seconds:
                    continue
                retry_in = _next_retry_seconds(now, call, attempt, has_success=True)
                if failures > 0 and retry_in > 0:
                    continue
                target = stale_due if failures > 0 else healthy_due
                target.append((call.priority, last, call))
            else:
                retry_in = _next_retry_seconds(now, call, attempt, has_success=False)
                if last_attempt <= 0 or retry_in <= 0:
                    cold_due.append((call.priority, last_attempt, call))

    for bucket in (healthy_due, stale_due, cold_due):
        bucket.sort(key=lambda item: (item[0], item[1], item[2].method, item[2].key))
    if force:
        return [item[2] for item in healthy_due]

    selected = healthy_due[:BACKGROUND_REFRESH_BATCH_SIZE]
    remaining = BACKGROUND_REFRESH_BATCH_SIZE - len(selected)
    if remaining > 0:
        stale_limit = min(remaining, BACKGROUND_STALE_RETRY_LIMIT)
        selected.extend(stale_due[:stale_limit])
        remaining = BACKGROUND_REFRESH_BATCH_SIZE - len(selected)
    if remaining > 0:
        cold_limit = min(remaining, BACKGROUND_COLD_RETRY_LIMIT)
        selected.extend(cold_due[:cold_limit])
    return [item[2] for item in selected]


def _run_call(device: Device, call: MonitorCall, request_id: int) -> dict[str, Any]:
    started = time.perf_counter()
    updated_at = datetime.now().isoformat(timespec="seconds")
    try:
        response = asiair_device_rpc(
            device,
            call.method,
            params=call.params,
            request_id=request_id,
            port=call.port,
            timeout_seconds=call.timeout_seconds,
        )
        redacted = _redact_sensitive(response)
        result = _trim_value(redacted.get("result"))
        code = response.get("code")
        entry = {
            "_monotonic": time.monotonic(),
            "device": device.name,
            "ip": device.ip,
            "endpoint": response.get("_endpoint"),
            "port": call.port,
            "method": call.method,
            "params": call.params,
            "category": call.category,
            "label": call.label,
            "interval_seconds": call.interval_seconds,
            "updated_at": updated_at,
            "ok": code == 0,
            "code": code,
            "seconds": round(time.perf_counter() - started, 3),
            "result": result,
        }
        entry.update(_augment_display(call, result, _display_fields(call, result, code == 0), code == 0))
        return entry
    except Exception as exc:  # noqa: BLE001
        return {
            "_monotonic": time.monotonic(),
            "device": device.name,
            "ip": device.ip,
            "port": call.port,
            "method": call.method,
            "params": call.params,
            "category": call.category,
            "label": call.label,
            "interval_seconds": call.interval_seconds,
            "updated_at": updated_at,
            "ok": False,
            "code": None,
            "seconds": round(time.perf_counter() - started, 3),
            "result": None,
            "display_value": "不可用",
            "display_detail": "",
            "display_rows": [],
            "display_table": None,
            "display_wide": False,
            "error": str(exc),
        }


def _build_snapshot(server: Any, device: Device) -> dict[str, Any]:
    now = time.monotonic()
    with server.rpc_monitor_lock:
        refreshing = (
            device.name in server.rpc_monitor_refreshing
            or device.name in server.rpc_monitor_force_refresh
        )
        refresh_progress = dict(server.rpc_monitor_force_progress.get(device.name) or {})
        entries = {
            call.key: dict(server.rpc_monitor_cache.get(_cache_key(device, call), {}))
            for call in MONITOR_CALLS
        }
        attempt_entries = {
            call.key: dict(getattr(server, "rpc_monitor_attempts", {}).get(_cache_key(device, call), {}))
            for call in MONITOR_CALLS
        }

    categories: list[dict[str, Any]] = []
    for category in CATEGORY_ORDER:
        calls = [call for call in MONITOR_CALLS if call.category == category]
        if not calls:
            continue
        items = []
        for call in calls:
            entry = entries.get(call.key) or _pending_entry(device, call)
            entry["age_seconds"] = _entry_age_seconds(entry, now)
            _annotate_schedule(entry, call, attempt_entries.get(call.key), now)
            entry.pop("_monotonic", None)
            items.append(entry)
        ok_count = sum(1 for item in items if item.get("ok"))
        ready_count = sum(1 for item in items if item.get("updated_at"))
        pending_count = sum(1 for item in items if item.get("pending"))
        backoff_count = sum(1 for item in items if item.get("scheduler_state") == "backoff")
        stale_count = sum(1 for item in items if item.get("stale"))
        categories.append(
            {
                "id": category,
                "label": CATEGORY_LABELS.get(category, category),
                "total_count": len(items),
                "ready_count": ready_count,
                "ok_count": ok_count,
                "fail_count": ready_count - ok_count,
                "pending_count": pending_count,
                "backoff_count": backoff_count,
                "stale_count": stale_count,
                "items": items,
            }
        )

    flat_items = [item for category in categories for item in category["items"]]
    ready_items = [item for item in flat_items if item.get("updated_at")]
    pending_items = [item for item in flat_items if item.get("pending")]
    backoff_items = [item for item in flat_items if item.get("scheduler_state") == "backoff"]
    stale_items = [item for item in flat_items if item.get("stale")]
    link = _link_status(server, device, ready_items)
    camera_cache = _camera_cache_payload(server, device)
    highlights = _merge_camera_cache_highlights(_highlights(flat_items), camera_cache)
    return {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "poll_interval_ms": 1000,
        "refreshing": refreshing,
        "refresh_progress": refresh_progress,
        "safety": {
            "mode": "read-only",
            "notes": [
                "Only verified read-only information calls are included.",
                "No set/start/stop/delete/clear/move/goto/power/network-write/image-download calls are used.",
                "Browser polls the cache once per second; the backend refreshes all active-device categories by tiered intervals.",
                "Repeatedly failing items are retried with backoff so healthy cached items keep their normal refresh cadence.",
            ],
        },
        "device": {
            "name": device.name,
            "ip": device.ip,
        },
        "devices": [
            {"name": item.name, "ip": item.ip, "enabled": item.enabled}
            for item in server.config.enabled_devices()
        ],
        "summary": {
            "total_count": len(flat_items),
            "ready_count": len(ready_items),
            "ok_count": sum(1 for item in ready_items if item.get("ok")),
            "fail_count": sum(1 for item in ready_items if not item.get("ok")),
            "pending_count": len(pending_items),
            "backoff_count": len(backoff_items),
            "stale_count": len(stale_items),
        },
        "highlights": highlights,
        "cache": {
            "rpc_monitor": "persistent",
            "camera": camera_cache.get("cache") if isinstance(camera_cache, dict) else None,
        },
        "link": link,
        "categories": categories,
    }


def _link_status(server: Any, device: Device, ready_items: list[dict[str, Any]]) -> dict[str, Any]:
    now = time.monotonic()
    with server.rpc_monitor_lock:
        cache = getattr(server, "rpc_monitor_link_cache", {})
        cached = cache.get(device.name)

    if cached and now - float(cached.get("_monotonic", 0.0)) < LINK_PROBE_INTERVAL_SECONDS:
        return {key: value for key, value in cached.items() if key != "_monotonic"}

    link = _probe_link(server, device, ready_items)
    with server.rpc_monitor_lock:
        server.rpc_monitor_link_cache[device.name] = {**link, "_monotonic": time.monotonic()}
    return link


def _probe_link(server: Any, device: Device, ready_items: list[dict[str, Any]]) -> dict[str, Any]:
    endpoint_links = []
    for endpoint in device.endpoint_candidates():
        ping_item = _ping_latency(endpoint.ip)
        smb_item = tcp_open(endpoint.ip, server.config.backup.smb_port, timeout_seconds=LINK_TCP_TIMEOUT_SECONDS)
        endpoint_links.append(
            {
                "label": endpoint.label,
                "ip": endpoint.ip,
                "kind": endpoint.kind,
                "priority": endpoint.priority,
                "ping": ping_item,
                "smb": {
                    "ok": smb_item.ok,
                    "port": server.config.backup.smb_port,
                    "detail": smb_item.detail,
                },
                "ok": bool(ping_item.get("ok") or smb_item.ok),
            }
        )

    selected_endpoint = next((item for item in endpoint_links if item["ok"]), endpoint_links[0])
    ping = selected_endpoint["ping"]
    smb = selected_endpoint["smb"]
    rpc = _rpc_latency(ready_items)
    network = collect_network_stats(server.config, time.time()).get("tailscale") or {}
    transfer = _transfer_status(server, device, network)

    primary_ok = bool(ping["ok"] or rpc["ok"])
    degraded = (
        not primary_ok
        or not smb["ok"]
        or (ping.get("latency_ms") is not None and float(ping["latency_ms"]) >= 300)
        or (rpc.get("latency_ms") is not None and float(rpc["latency_ms"]) >= 500)
    )
    if not primary_ok:
        status = "offline"
        quality = "danger"
    elif degraded:
        status = "degraded"
        quality = "warn"
    else:
        status = "online"
        quality = "ok"

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "quality": quality,
        "device": device.name,
        "ip": device.ip,
        "endpoint": selected_endpoint,
        "endpoints": endpoint_links,
        "ping": ping,
        "rpc": rpc,
        "smb": {
            "ok": smb["ok"],
            "port": smb["port"],
            "detail": smb["detail"],
        },
        "tailscale": _tailscale_link(network),
        "transfer": transfer,
    }


def _ping_latency(host: str) -> dict[str, Any]:
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(LINK_PING_TIMEOUT_MS), host]
        timeout = LINK_PING_TIMEOUT_MS / 1000 + 0.5
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(LINK_PING_TIMEOUT_MS / 1000))), host]
        timeout = max(1.0, LINK_PING_TIMEOUT_MS / 1000 + 0.5)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "latency_ms": None,
            "detail": str(exc),
        }

    text = f"{proc.stdout}\n{proc.stderr}".strip()
    detail = _first_reply_line(text) if proc.returncode == 0 else _last_nonempty(text)
    return {
        "ok": proc.returncode == 0,
        "latency_ms": _parse_ping_latency_ms(text),
        "detail": detail or f"exit {proc.returncode}",
    }


def _parse_ping_latency_ms(text: str) -> int | None:
    if re.search(r"(?:time|时间)\s*<\s*1\s*ms", text, flags=re.IGNORECASE):
        return 1
    patterns = (
        r"(?:Average|平均)\s*=\s*(\d+)\s*ms",
        r"(?:time|时间)\s*[=<]\s*(\d+)\s*ms",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _rpc_latency(ready_items: list[dict[str, Any]]) -> dict[str, Any]:
    test_item = next(
        (
            item
            for item in ready_items
            if item.get("method") == "test_connection" and item.get("seconds") is not None
        ),
        None,
    )
    if test_item is None:
        fresh = [
            item
            for item in ready_items
            if item.get("ok") and item.get("seconds") is not None and (item.get("age_seconds") or 999) <= 10
        ]
        if fresh:
            test_item = min(fresh, key=lambda item: float(item.get("seconds") or 999))

    if test_item is None:
        return {"ok": False, "latency_ms": None, "method": None, "age_seconds": None}

    return {
        "ok": bool(test_item.get("ok")),
        "latency_ms": round(float(test_item.get("seconds") or 0) * 1000),
        "method": test_item.get("method"),
        "age_seconds": test_item.get("age_seconds"),
    }


def _tailscale_link(network: dict[str, Any]) -> dict[str, Any]:
    sampled_at = network.get("sampled_at")
    age_seconds = None
    if sampled_at is not None:
        try:
            age_seconds = round(max(0.0, time.time() - float(sampled_at)), 1)
        except (TypeError, ValueError):
            age_seconds = None
    return {
        "ok": bool(network.get("ok")),
        "adapter": network.get("adapter") or "Tailscale",
        "sample_age_seconds": age_seconds,
        "receive_bytes_per_second": network.get("receive_bytes_per_second"),
        "send_bytes_per_second": network.get("send_bytes_per_second"),
        "error": network.get("error"),
    }


def _transfer_status(server: Any, device: Device, network: dict[str, Any]) -> dict[str, Any]:
    lock = read_lock(server.config.project.lock_file)
    active = bool(lock.get("active") and lock.get("pid_alive") is not False)
    lock_devices = set(lock.get("devices") or [])
    active_for_device = active and (not lock_devices or device.name in lock_devices)
    return {
        "active": active_for_device,
        "lock_active": active,
        "source": "tailscale_adapter",
        "device_scoped": False,
        "receive_bytes_per_second": network.get("receive_bytes_per_second"),
        "send_bytes_per_second": network.get("send_bytes_per_second"),
    }


def _first_nonempty(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _last_nonempty(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _first_reply_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if "ttl=" in lowered or "time=" in lowered or "time<" in lowered or "时间=" in stripped or "时间<" in stripped:
            return stripped
    return _first_nonempty(text)


def _entry_age_seconds(entry: dict[str, Any], now: float) -> float | None:
    if entry.get("_monotonic"):
        try:
            return round(max(0.0, now - float(entry["_monotonic"])), 1)
        except (TypeError, ValueError):
            pass
    updated_at = entry.get("updated_at")
    if not updated_at:
        return None
    try:
        return round(max(0.0, time.time() - datetime.fromisoformat(str(updated_at)).timestamp()), 1)
    except ValueError:
        return None


def _pending_entry(device: Device, call: MonitorCall) -> dict[str, Any]:
    return {
        "device": device.name,
        "ip": device.ip,
        "port": call.port,
        "method": call.method,
        "params": call.params,
        "category": call.category,
        "label": call.label,
        "interval_seconds": call.interval_seconds,
        "updated_at": None,
        "ok": False,
        "code": None,
        "seconds": None,
        "result": None,
        "pending": True,
    }


def _annotate_schedule(entry: dict[str, Any], call: MonitorCall, attempt: Any, now: float) -> None:
    has_success = bool(entry.get("ok") and entry.get("updated_at"))
    failures = _attempt_failure_count(attempt)
    retry_after = _retry_delay_seconds(call, failures, has_success)
    next_retry = _next_retry_seconds(now, call, attempt, has_success)
    if failures > 0 and next_retry > 0:
        state = "backoff"
    elif entry.get("pending"):
        state = "pending"
    elif entry.get("stale"):
        state = "stale"
    elif entry.get("ok"):
        state = "ready"
    else:
        state = "failed"

    entry["scheduler_state"] = state
    entry["consecutive_failures"] = failures
    entry["retry_after_seconds"] = round(retry_after, 1) if retry_after else 0
    entry["next_retry_in_seconds"] = round(next_retry, 1) if next_retry else 0
    if isinstance(attempt, dict):
        entry["last_attempt_at"] = attempt.get("updated_at")
        entry["last_success_at"] = attempt.get("last_success_at")
        entry["last_failure_at"] = attempt.get("last_failure_at")
    else:
        entry["last_attempt_at"] = None
        entry["last_success_at"] = None
        entry["last_failure_at"] = None


def _cache_key(device: Device, call: MonitorCall) -> str:
    return f"{device.name}:{call.key}"


def _trim_value(value: Any, depth: int = 0) -> Any:
    if depth >= 5:
        return _compact_scalar(value)
    if isinstance(value, dict):
        items = list(value.items())
        trimmed = {
            str(key): _trim_value(item, depth + 1)
            for key, item in items[:MAX_DICT_KEYS]
        }
        if len(items) > MAX_DICT_KEYS:
            trimmed["__truncated_keys__"] = len(items) - MAX_DICT_KEYS
        return trimmed
    if isinstance(value, list):
        trimmed_list = [_trim_value(item, depth + 1) for item in value[:MAX_INITIAL_DISPLAY_ITEMS]]
        if len(value) > MAX_INITIAL_DISPLAY_ITEMS:
            trimmed_list.append({"__truncated_items__": len(value) - MAX_INITIAL_DISPLAY_ITEMS})
        return trimmed_list
    return _compact_scalar(value)


def _compact_scalar(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_STRING:
        return f"{value[:MAX_STRING]}... [truncated {len(value) - MAX_STRING} chars]"
    return value


def _highlights(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_call: dict[tuple[str, str], dict[str, Any]] = {}
    by_method: dict[str, dict[str, Any]] = {}
    for item in items:
        params = json.dumps(item.get("params"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        by_call[(item["method"], params)] = item
        by_method.setdefault(item["method"], item)

    app = by_method.get("get_app_state", {}).get("result") or {}
    capture = app.get("capture", {}) if isinstance(app, dict) else {}
    progress = capture.get("progress", {}) if isinstance(capture, dict) else {}
    target = progress.get("cur_target", {}) if isinstance(progress, dict) else {}
    sequence = progress.get("cur_seq", {}) if isinstance(progress, dict) else {}
    frame_summary = capture.get("frame_summary", {}) if isinstance(capture, dict) else {}
    camera = by_method.get("get_camera_state", {}).get("result") or {}
    disk = by_method.get("get_disk_volume", {}).get("result") or {}
    save_path = by_method.get("get_image_save_path", {}).get("result") or {}

    temp = _control_value_for(by_call, "Temperature")
    cool_power = _value_for(by_call, "get_control_value", ["CoolPowerPerc"])
    exposure = by_method.get("get_camera_exp_and_bin", {}).get("result") or {}
    target_name = target.get("target_name") if isinstance(target, dict) else None
    sequence_type = sequence.get("frame_type") if isinstance(sequence, dict) else None
    planned_capture = _planned_capture_metadata(by_method, target_name, sequence_type)
    exposure_seconds = planned_capture.get("exposure_seconds")
    if exposure_seconds is None:
        exposure_seconds = _exposure_seconds(exposure)
    bin_value = planned_capture.get("bin")
    if bin_value is None and isinstance(exposure, dict):
        bin_value = exposure.get("bin")
    return {
        "page": app.get("page") if isinstance(app, dict) else None,
        "capture_state": capture.get("state") if isinstance(capture, dict) else None,
        "capture_working": capture.get("is_working") if isinstance(capture, dict) else None,
        "target_name": target_name,
        "target_progress": _progress_pair(target),
        "sequence_type": sequence_type,
        "sequence_progress": _progress_pair(sequence),
        "frame_progress": _progress_pair(frame_summary),
        "camera_name": camera.get("name") if isinstance(camera, dict) else None,
        "camera_state": camera.get("state") if isinstance(camera, dict) else None,
        "exposure_seconds": exposure_seconds,
        "exposure_source": planned_capture.get("source") or "camera_exp_and_bin",
        "bin": bin_value,
        "temperature_c": temp,
        "cooler_power_percent": cool_power,
        "storage_free_mb": disk.get("freeMB") if isinstance(disk, dict) else None,
        "storage_total_mb": disk.get("totalMB") if isinstance(disk, dict) else None,
        "current_storage": save_path.get("cur_storage") if isinstance(save_path, dict) else None,
        "connected_storage": save_path.get("connected_storage") if isinstance(save_path, dict) else None,
    }


def _planned_capture_metadata(
    by_method: dict[str, dict[str, Any]],
    target_name: Any,
    sequence_type: Any,
) -> dict[str, Any]:
    target_text = str(target_name or "").strip()
    if not target_text:
        return {}

    plan_result = by_method.get("get_plan", {}).get("result")
    if isinstance(plan_result, list):
        for plan in sorted(
            plan_result,
            key=lambda item: not bool(item.get("is_plan_started")) if isinstance(item, dict) else True,
        ):
            if not isinstance(plan, dict):
                continue
            for target in plan.get("targets") or []:
                if not isinstance(target, dict) or not _same_target_name(target, target_text):
                    continue
                sequence = _select_planned_sequence(target.get("seqs"), sequence_type)
                if sequence:
                    return _planned_sequence_metadata(sequence, "plan_sequence")

    target_sequences = by_method.get("get_target_sequences", {}).get("result")
    if isinstance(target_sequences, dict) and _same_text(target_sequences.get("group_name"), target_text):
        sequence = _select_planned_sequence(target_sequences.get("slots"), sequence_type)
        if sequence:
            return _planned_sequence_metadata(sequence, "target_sequences")
    return {}


def _same_target_name(target: dict[str, Any], target_text: str) -> bool:
    return any(
        _same_text(target.get(key), target_text)
        for key in ("target_name", "target_original_name", "group_name")
    )


def _same_text(left: Any, right: Any) -> bool:
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _select_planned_sequence(sequences: Any, sequence_type: Any) -> dict[str, Any] | None:
    if not isinstance(sequences, list):
        return None
    typed = [
        item
        for item in sequences
        if isinstance(item, dict)
        and (not sequence_type or _same_text(item.get("type"), sequence_type))
    ]
    candidates = typed or [item for item in sequences if isinstance(item, dict)]
    enabled = [item for item in candidates if bool(item.get("enable"))]
    return (enabled or candidates or [None])[0]


def _planned_sequence_metadata(sequence: dict[str, Any], source: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {"source": source}
    try:
        exp = float(sequence.get("exp"))
    except (TypeError, ValueError):
        exp = 0.0
    if exp > 0:
        metadata["exposure_seconds"] = exp
    if sequence.get("bin") is not None:
        metadata["bin"] = sequence.get("bin")
    if sequence.get("filter") is not None:
        metadata["filter_position"] = sequence.get("filter")
    if sequence.get("type") is not None:
        metadata["sequence_type"] = sequence.get("type")
    return metadata


def _camera_cache_payload(server: Any, device: Device) -> dict[str, Any]:
    camera_cache = getattr(server, "camera_cache", None)
    if camera_cache is None:
        return {}
    try:
        payload = camera_cache.get(device.name, session_id=None)
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _merge_camera_cache_highlights(highlights: dict[str, Any], camera_cache: dict[str, Any]) -> dict[str, Any]:
    merged = dict(highlights)
    app = camera_cache.get("app") if isinstance(camera_cache.get("app"), dict) else {}
    camera = camera_cache.get("camera") if isinstance(camera_cache.get("camera"), dict) else {}
    exposure = camera_cache.get("exposure") if isinstance(camera_cache.get("exposure"), dict) else {}
    controls = camera_cache.get("controls") if isinstance(camera_cache.get("controls"), dict) else {}

    fallback = {
        "page": app.get("page"),
        "capture_state": app.get("capture_state"),
        "capture_working": app.get("capture_working"),
        "camera_name": camera.get("name"),
        "camera_state": camera.get("state"),
        "exposure_seconds": exposure.get("seconds"),
        "bin": exposure.get("bin"),
        "temperature_c": _control_cache_value(controls, "temperature"),
        "cooler_power_percent": _control_cache_value(controls, "cool_power"),
    }
    for key, value in fallback.items():
        if _is_missing(merged.get(key)) and not _is_missing(value):
            merged[key] = value
    return merged


def _control_cache_value(controls: dict[str, Any], key: str) -> Any:
    value = controls.get(key)
    if isinstance(value, dict):
        return value.get("value")
    return value


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _value_for(by_call: dict[tuple[str, str], dict[str, Any]], method: str, params: Any) -> Any:
    key = (method, json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    result = by_call.get(key, {}).get("result")
    if isinstance(result, dict):
        return result.get("value")
    return result


def _control_value_for(by_call: dict[tuple[str, str], dict[str, Any]], name: str) -> Any:
    value = _value_for(by_call, "get_control_value", [name])
    return _normalize_control_value(name, value)


def _progress_pair(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    total = value.get("total")
    lapse = value.get("lapse", value.get("complete_num"))
    if total is None or lapse is None:
        return None
    return {"done": lapse, "total": total}


def _exposure_seconds(value: Any) -> float | None:
    if not isinstance(value, dict) or value.get("exposure") is None:
        return None
    exposure = value["exposure"]
    try:
        return round(float(exposure) / 1_000_000, 3)
    except (TypeError, ValueError):
        return None


def _display_fields(call: MonitorCall, result: Any, ok: bool) -> dict[str, Any]:
    if not ok:
        return {"display_value": "不可用", "display_detail": ""}
    method = call.method
    params = call.params
    if method == "get_app_state" and isinstance(result, dict):
        page = result.get("page")
        capture = result.get("capture", {})
        progress = capture.get("progress", {}) if isinstance(capture, dict) else {}
        target = progress.get("cur_target", {}) if isinstance(progress, dict) else {}
        seq = progress.get("cur_seq", {}) if isinstance(progress, dict) else {}
        target_text = _format_progress(target)
        seq_text = _format_progress(seq)
        value = f"{page or '-'} · {capture.get('state') or '-'}"
        detail = " · ".join(item for item in [target.get("target_name"), target_text, seq.get("frame_type"), seq_text] if item)
        return {"display_value": value, "display_detail": detail}
    if method == "get_device_state" and isinstance(result, dict):
        device = result.get("device", {})
        camera = result.get("camera", {})
        focal_len = device.get("focal_len") if isinstance(device, dict) else None
        chip = camera.get("chip_size") if isinstance(camera, dict) else None
        return {
            "display_value": f"{device.get('name', 'ASIAIR')} · {device.get('firmware_ver_string', '-')}",
            "display_detail": f"焦距 {focal_len or '-'} mm · 传感器 {_format_pair(chip)}",
        }
    if method == "get_camera_state" and isinstance(result, dict):
        return {"display_value": result.get("name", "-"), "display_detail": f"状态 {result.get('state', '-')}"}
    if method == "get_camera_info" and isinstance(result, dict):
        return {
            "display_value": _format_pair(result.get("chip_size")),
            "display_detail": f"{result.get('pixel_size_um', '-')} μm · {'彩色' if result.get('is_color') else '黑白'}",
        }
    if method == "get_connected_cameras" and isinstance(result, list):
        return {"display_value": f"{len(result)} 台", "display_detail": ", ".join(_name_from(item) for item in result[:4])}
    if method == "get_controls" and isinstance(result, list):
        return {"display_value": f"{len(result)} 项", "display_detail": "相机控制项和范围"}
    if method == "get_control_value" and isinstance(result, dict):
        name = result.get("name")
        value = _normalize_control_value(str(name), result.get("value"))
        return {"display_value": _format_control_value(str(name), value, result), "display_detail": f"{name}"}
    if method == "get_camera_exp_and_bin" and isinstance(result, dict):
        exposure = _exposure_seconds(result)
        return {"display_value": f"{exposure:g}s · Bin {result.get('bin', '-')}", "display_detail": "曝光与 Bin"}
    if method == "get_camera_bin":
        return {"display_value": f"Bin {result}", "display_detail": ""}
    if method == "get_camera_16bit":
        return {"display_value": "开启" if result else "关闭", "display_detail": "16-bit 模式"}
    if method == "get_subframe" and isinstance(result, dict):
        return {"display_value": f"{result.get('width', '-')}×{result.get('height', '-')}", "display_detail": f"x={result.get('x', '-')}, y={result.get('y', '-')}"}
    if method == "get_gain_segment" and isinstance(result, list):
        return {"display_value": " / ".join(str(item) for item in result), "display_detail": "增益段"}
    if method in {"can_liveview", "can_abort_expose", "pi_is_verified", "need_reboot", "is_downgraded", "can_format_emmc"}:
        return {"display_value": "是" if result else "否", "display_detail": ""}
    if method == "get_img_name_field":
        return {"display_value": _short_json(result), "display_detail": "命名字段"}
    if method == "get_disk_volume" and isinstance(result, dict):
        free = _mb_to_gb(result.get("freeMB"))
        total = _mb_to_gb(result.get("totalMB"))
        pct = _percent(result.get("freeMB"), result.get("totalMB"))
        return {"display_value": f"{free} / {total} GB", "display_detail": f"剩余 {pct}%"}
    if method == "get_image_save_path":
        return {"display_value": str(result), "display_detail": ""}
    if method == "list_mass_storage" and isinstance(result, list):
        return {"display_value": f"{len(result)} 个挂载项", "display_detail": _short_json(result)}
    if method == "get_img_file_page_number":
        return {"display_value": str(result), "display_detail": "图像浏览器页数"}
    if method == "get_power_supply" and isinstance(result, list):
        voltages = [item[0] for item in result if isinstance(item, list) and item]
        currents = [item[1] for item in result if isinstance(item, list) and len(item) > 1]
        return {
            "display_value": f"{_avg(voltages):.2f} V",
            "display_detail": f"最大电流 {max(currents) if currents else 0:.2f} A · {len(result)} 路",
        }
    if method == "pi_output_get2" and isinstance(result, list):
        on_count = sum(1 for item in result if isinstance(item, dict) and item.get("state"))
        return {"display_value": f"{on_count}/{len(result)} 开启", "display_detail": "电源输出"}
    if method == "pi_output_get":
        return {"display_value": _short_json(result), "display_detail": "旧版输出"}
    if method in {"pi_get_ap", "pi_station_state", "pi_station_list", "pi_eth0_state"}:
        return {"display_value": _network_summary(method, result), "display_detail": "敏感字段已脱敏"}
    if method == "pi_get_info" and isinstance(result, dict):
        return {"display_value": result.get("model", "ASIAIR"), "display_detail": f"{result.get('temp', '-')}°C · {result.get('uname', '')}"}
    if method == "pi_get_time" and isinstance(result, dict):
        return {"display_value": f"{result.get('year')}-{result.get('mon')}-{result.get('day')} {result.get('hour')}:{result.get('min')}:{result.get('sec')}", "display_detail": result.get("time_zone", "")}
    if method == "test_connection":
        return {"display_value": "连接正常", "display_detail": ""}
    if method.startswith("get_sequence") or method in {"get_plan", "get_enabled_plan", "list_plan", "get_target_sequences"}:
        return _plan_display(method, result)
    if method.startswith("get_focuser") or method == "get_connected_focuser":
        return _focuser_display(method, result)
    if method.startswith("get_wheel") or method == "get_connected_wheels":
        return _wheel_display(method, result)
    if method.startswith("scope_"):
        return _mount_display(method, result)
    if method in {"get_dither", "get_flip_calibration"}:
        return {"display_value": _short_json(result), "display_detail": "导星相关只读状态"}
    if method.startswith("get_stack") or method.startswith("get_batch_stack") or method.startswith("get_calib"):
        return {"display_value": _short_json(result), "display_detail": "叠加 / 校准"}
    if method.startswith("get_solve"):
        return {"display_value": _short_json(result), "display_detail": "解算"}
    if method in {"get_find_star_result", "get_annotate_result"}:
        return {"display_value": _short_json(result), "display_detail": "分析结果"}
    if method.startswith("get_3p_pa") or method == "get_polar_align_image":
        return {"display_value": _short_json(result), "display_detail": "极轴状态"}
    if method in {"get_list", "get_constellations", "get_comet_position", "get_planet_position", "get_obj"}:
        if isinstance(result, list):
            return {"display_value": f"{len(result)} 项", "display_detail": "天体数据"}
        return {"display_value": _short_json(result), "display_detail": "天体数据"}
    if method == "get_rtmp_config":
        return {"display_value": _short_json(result), "display_detail": "直播配置"}
    if method in {"get_app_setting", "get_test_setting"}:
        return {"display_value": _short_json(result), "display_detail": "设置"}
    return {"display_value": _short_json(result), "display_detail": ""}


def _augment_display(
    call: MonitorCall,
    result: Any,
    display: dict[str, Any],
    ok: bool,
) -> dict[str, Any]:
    if not ok:
        display.setdefault("display_rows", [])
        display.setdefault("display_table", None)
        display.setdefault("display_wide", False)
        return display

    rows = _display_rows(call, result)
    table = _display_table(call, result)
    if rows:
        display.setdefault("display_rows", rows)
    else:
        display.setdefault("display_rows", [])
    if table:
        display["display_table"] = table
    else:
        display.setdefault("display_table", None)

    if _looks_structured_text(display.get("display_value")):
        display["display_value"] = _complex_summary(call, result)
    if _looks_structured_text(display.get("display_detail")):
        display["display_detail"] = ""
    display.setdefault("display_wide", _should_use_wide_layout(call, rows, table))
    return display


def _display_rows(call: MonitorCall, result: Any) -> list[dict[str, str]]:
    method = call.method
    if isinstance(result, dict):
        if method == "get_img_name_field":
            enabled = [label for key, label in _IMAGE_NAME_LABELS.items() if result.get(key)]
            rows = [
                _row("启用字段", "、".join(enabled) if enabled else "无"),
                _row("自定义后缀", result.get("custom_suffix") or "-"),
            ]
            return rows + [
                _row(label, _format_bool(result.get(key)))
                for key, label in _IMAGE_NAME_LABELS.items()
                if key in result
            ]
        if method == "get_image_save_path":
            volumes = result.get("storage_volume")
            return [
                _row("当前存储", result.get("cur_storage")),
                _row("已连接存储", _join_values(result.get("connected_storage"))),
                _row("Type-C", "已连接" if result.get("is_typec_connected") else "未连接"),
                _row("存储项", f"{len(volumes)} 个" if isinstance(volumes, list) else "-"),
            ]
        if method in {"get_solve_result", "get_last_solve_result"}:
            return [
                _row("状态", result.get("state")),
                _row("RA / DEC", _format_coord_pair(result.get("ra_dec"))),
                _row("视场", _format_size(result.get("fov"), "°")),
                _row("焦距", _format_number(result.get("focal_len"), " mm")),
                _row("旋转角", _format_number(result.get("angle"), "°")),
                _row("星点数", result.get("star_number")),
                _row("耗时", _format_number(result.get("duration_ms"), " ms")),
                _row("图像 ID", result.get("image_id")),
            ]
        if method == "get_find_star_result":
            stars = result.get("stars")
            return [
                _row("图像 ID", result.get("image_id")),
                _row("星点数量", _list_count_text(stars)),
                _row("像素尺度", _format_number(result.get("pixelscale_arcsec"), "\"/px")),
            ]
        if method in {"get_annotate_result", "get_constellations"}:
            annotations = result.get("annotations")
            return [
                _row("图像尺寸", _format_pair(result.get("image_size"))),
                _row("标注数量", _list_count_text(annotations)),
                _row("图像 ID", result.get("image_id")),
            ]
        if method == "get_calib_frame":
            enabled = [
                key
                for key in ("dark", "bias", "flat")
                if isinstance(result.get(key), dict) and result[key].get("enable")
            ]
            return [
                _row("模拟模式", _format_bool(result.get("simulate"))),
                _row("启用帧", "、".join(enabled) if enabled else "无"),
            ]
        if method == "get_planet_position":
            return [_row(_KEY_LABELS.get(key, key), _format_coord_pair(value)) for key, value in result.items()]
        rows = _simple_dict_rows(result)
        if method in {"get_app_setting", "get_test_setting"} and len(result) > len(rows):
            rows.append(_row("其余字段", f"{len(result) - len(rows)} 个"))
        return rows

    if isinstance(result, list):
        if method == "pi_output_get":
            labels = ["输出 1", "输出 2", "输出 3", "总开关"]
            return [_row(labels[index] if index < len(labels) else f"项目 {index + 1}", value) for index, value in enumerate(result[:8])]
        if all(not isinstance(item, (dict, list)) for item in result):
            return [_row(f"项目 {index + 1}", value) for index, value in enumerate(result[:10])]
        return [_row("项目数量", _list_count_text(result))]
    return []


def _display_table(call: MonitorCall, result: Any) -> dict[str, Any] | None:
    method = call.method
    if isinstance(result, dict):
        if method == "get_image_save_path" and isinstance(result.get("storage_volume"), list):
            return _dict_list_table(
                result["storage_volume"],
                ["name", "state", "freeMB", "totalMB", "diskSizeMB"],
                ["名称", "状态", "剩余", "总量", "磁盘大小"],
                "存储卷",
            )
        if method == "get_wheel_setting":
            names = result.get("names") if isinstance(result.get("names"), list) else []
            exp = result.get("exp_sec") if isinstance(result.get("exp_sec"), list) else []
            offset = result.get("offset") if isinstance(result.get("offset"), list) else []
            rows = [
                [index + 1, names[index] if index < len(names) else "-", exp[index] if index < len(exp) else "-", offset[index] if index < len(offset) else "-"]
                for index in range(max(len(names), len(exp), len(offset)))
            ]
            return _table(["槽位", "名称", "曝光(s)", "Offset"], rows, "滤轮槽位")
        if method == "get_calib_frame":
            rows = []
            for key in ("dark", "bias", "flat"):
                frame = result.get(key)
                if isinstance(frame, dict):
                    rows.append([key, _format_bool(frame.get("enable")), frame.get("frame_name") or "-", _format_pair(frame.get("frame_size"))])
            return _table(["类型", "启用", "帧名称", "尺寸"], rows, "校准帧")
        if method == "get_find_star_result" and isinstance(result.get("stars"), list):
            return _dict_list_table(
                result["stars"],
                ["pixelx", "pixely", "radius", "hfd"],
                ["X", "Y", "半径", "HFD"],
                "星点前 8 项",
                limit=8,
            )
        if method in {"get_annotate_result", "get_constellations"} and isinstance(result.get("annotations"), list):
            rows = []
            for item in result["annotations"][:8]:
                if isinstance(item, dict):
                    rows.append([
                        item.get("type"),
                        _join_values(item.get("names")) or item.get("name") or "-",
                        _format_number(item.get("pixelx")),
                        _format_number(item.get("pixely")),
                        _format_number(item.get("radius")),
                    ])
            return _table(["类型", "名称", "X", "Y", "半径"], rows, "标注前 8 项")
        slots = result.get("slots")
        if isinstance(slots, list):
            return _dict_list_table(slots, [], [], "槽位", limit=8)
        return None

    if isinstance(result, list):
        if method == "get_solve_obj":
            rows = [
                [index + 1, _format_number(item[0]), _format_number(item[1]), _format_number(item[2])]
                for index, item in enumerate(result[:10])
                if isinstance(item, list) and len(item) >= 3
            ]
            return _table(["序号", "X", "Y", "值"], rows, "解算对象前 10 项")
        if all(isinstance(item, dict) for item in result):
            return _dict_list_table(result, [], [], "列表", limit=8)
        if all(isinstance(item, list) for item in result):
            rows = [[index + 1, *[_format_value("", value) for value in item[:5]]] for index, item in enumerate(result[:8])]
            max_len = max((len(row) for row in rows), default=1)
            columns = ["序号"] + [f"值 {index}" for index in range(1, max_len)]
            return _table(columns, rows, "列表前 8 项")
    return None


def _simple_dict_rows(value: dict[str, Any], limit: int = 12) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for key, item in value.items():
        if key.startswith("__"):
            continue
        if isinstance(item, dict):
            simple_items = [
                (sub_key, sub_value)
                for sub_key, sub_value in item.items()
                if not isinstance(sub_value, (dict, list))
            ]
            if 0 < len(simple_items) <= 4 and len(rows) + len(simple_items) <= limit:
                rows.extend(_row(f"{_label_for_key(key)} · {_label_for_key(sub_key)}", sub_value) for sub_key, sub_value in simple_items)
            else:
                rows.append(_row(_label_for_key(key), f"{len(item)} 个字段"))
        elif isinstance(item, list):
            rows.append(_row(_label_for_key(key), _format_value(key, item)))
        else:
            rows.append(_row(_label_for_key(key), item))
        if len(rows) >= limit:
            break
    return rows


def _dict_list_table(
    values: list[Any],
    keys: list[str],
    labels: list[str],
    caption: str,
    limit: int = 8,
) -> dict[str, Any] | None:
    dicts = [item for item in values if isinstance(item, dict)]
    if not dicts:
        return None
    if not keys:
        keys = []
        for item in dicts:
            for key in item:
                if key not in keys and not key.startswith("__"):
                    keys.append(key)
                if len(keys) >= 5:
                    break
            if len(keys) >= 5:
                break
    if not labels:
        labels = [_label_for_key(key) for key in keys]
    rows = [
        [_format_value(key, item.get(key)) for key in keys]
        for item in dicts[:limit]
    ]
    return _table(labels, rows, caption)


def _table(columns: list[Any], rows: list[list[Any]], caption: str = "") -> dict[str, Any] | None:
    if not rows:
        return None
    normalized_rows = [[_format_value("", value) for value in row] for row in rows]
    return {
        "caption": caption,
        "columns": [str(column) for column in columns],
        "rows": normalized_rows,
    }


def _row(label: str, value: Any) -> dict[str, str]:
    return {"label": str(label), "value": _format_value(str(label), value)}


def _format_value(key: str, value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return _format_bool(value)
    if isinstance(value, (int, float)):
        lower_key = key.lower()
        if lower_key.endswith("mb") or lower_key in {"剩余", "总量", "磁盘大小"}:
            return f"{float(value) / 1024:.1f} GB"
        if lower_key.endswith("_us"):
            return f"{float(value) / 1_000_000:g}s"
        return _format_number(value)
    if isinstance(value, list):
        if len(value) == 2 and all(isinstance(item, (int, float)) for item in value):
            return _format_coord_pair(value)
        if all(not isinstance(item, (dict, list)) for item in value):
            return _join_values(value)
        return _list_count_text(value)
    if isinstance(value, dict):
        return f"{len(value)} 个字段"
    return str(value)


def _format_bool(value: Any) -> str:
    if value is None:
        return "-"
    return "是" if bool(value) else "否"


def _format_number(value: Any, suffix: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _format_value("", value) if value is not None else "-"
    if number.is_integer():
        return f"{int(number)}{suffix}"
    return f"{number:.3f}".rstrip("0").rstrip(".") + suffix


def _format_coord_pair(value: Any) -> str:
    if isinstance(value, list) and len(value) >= 2:
        return f"{_format_number(value[0])}, {_format_number(value[1])}"
    return _format_value("", value)


def _format_size(value: Any, suffix: str = "") -> str:
    if isinstance(value, list) and len(value) >= 2:
        return f"{_format_number(value[0], suffix)} × {_format_number(value[1], suffix)}"
    return _format_value("", value)


def _join_values(value: Any) -> str:
    if not isinstance(value, list):
        return _format_value("", value)
    values = [str(item) for item in value if item not in (None, "")]
    return "、".join(values) if values else "-"


def _list_count_text(value: Any) -> str:
    if not isinstance(value, list):
        return "-"
    total, hidden = _list_counts(value)
    return f"{total} 项" + (f"（另有 {hidden} 项未展开）" if hidden else "")


def _list_counts(value: list[Any]) -> tuple[int, int]:
    hidden = 0
    visible = len(value)
    if value and isinstance(value[-1], dict) and "__truncated_items__" in value[-1]:
        hidden = int(value[-1].get("__truncated_items__") or 0)
        visible -= 1
    return visible + hidden, hidden


def _label_for_key(key: str) -> str:
    return _KEY_LABELS.get(key, key)


def _looks_structured_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return text.startswith("{") or text.startswith("[") or len(text) > 120


def _complex_summary(call: MonitorCall, result: Any) -> str:
    if isinstance(result, dict):
        if call.method == "get_img_name_field":
            enabled = sum(1 for key in _IMAGE_NAME_LABELS if result.get(key))
            return f"{enabled} 项启用"
        if call.method == "get_image_save_path":
            return f"当前 {result.get('cur_storage') or '-'}"
        if call.method in {"get_solve_result", "get_last_solve_result"}:
            return f"{result.get('state') or '-'} · {result.get('star_number', '-')} 星"
        if call.method == "get_find_star_result":
            return _list_count_text(result.get("stars"))
        if call.method in {"get_annotate_result", "get_constellations"}:
            return _list_count_text(result.get("annotations"))
        if call.method == "get_planet_position":
            return f"{len(result)} 个天体"
        return f"{len(result)} 个字段"
    if isinstance(result, list):
        return _list_count_text(result)
    return str(result)


def _should_use_wide_layout(
    call: MonitorCall,
    rows: list[dict[str, str]],
    table: dict[str, Any] | None,
) -> bool:
    return bool(
        table
        or len(rows) > 8
        or call.method
        in {
            "get_controls",
            "get_image_save_path",
            "get_sequence",
            "get_target_sequences",
            "get_plan",
            "get_enabled_plan",
            "list_plan",
            "get_focuser_setting",
            "get_wheel_setting",
            "get_solve_obj",
            "get_find_star_result",
            "get_annotate_result",
            "get_constellations",
            "get_app_setting",
            "get_test_setting",
        }
    )


_IMAGE_NAME_LABELS = {
    "camera_name": "相机名",
    "date_time": "日期时间",
    "gain": "Gain",
    "bin": "Bin",
    "temp": "温度",
    "rotator_angle": "旋转角",
}

_KEY_LABELS = {
    "id": "ID",
    "name": "名称",
    "state": "状态",
    "type": "类型",
    "enable": "启用",
    "enabled": "启用",
    "filter": "滤镜",
    "suffix": "后缀",
    "exp": "曝光",
    "gain": "Gain",
    "bin": "Bin",
    "repeat": "重复次数",
    "lapsed": "已完成",
    "capture_index": "拍摄序号",
    "autoexp": "自动曝光",
    "cur_storage": "当前存储",
    "connected_storage": "已连接存储",
    "storage_volume": "存储卷",
    "is_typec_connected": "Type-C",
    "totalMB": "总量",
    "freeMB": "剩余",
    "diskSizeMB": "磁盘大小",
    "state_code": "状态码",
    "auto_move": "自动移动",
    "auto_update": "自动更新",
    "paused": "暂停",
    "is_pa_3p": "三点极轴",
    "rotate_angle": "旋转角",
    "resolution": "分辨率",
    "rec_format": "录制格式",
    "enable_bgm": "背景音乐",
    "mirror_horiz": "水平镜像",
    "mirror_vert": "垂直镜像",
    "save_discrete_frame": "保存单帧",
    "light_duration_min": "Light 时长",
    "simulate": "模拟",
    "af_exp_sec": "AF 曝光",
    "af_init_step": "AF 初始步长",
    "af_bin": "AF Bin",
    "af_only_one": "仅一次 AF",
    "fine_step": "细调步长",
    "coarse_step": "粗调步长",
    "autosave": "自动保存 AF",
    "stack": "叠加 AF",
    "af_temp_span": "温差触发",
    "af_time_span_hour": "间隔小时",
    "af_temp_change_on": "温差触发开关",
    "af_time_change_on": "定时触发开关",
    "af_wheel_change": "滤轮变化触发",
    "af_before_capture": "拍摄前 AF",
    "af_merid_flip": "中天翻转 AF",
    "af_track_on": "跟踪时 AF",
    "af_before_stack": "叠加前 AF",
    "amount": "幅度",
    "interval": "间隔",
    "ra_only": "仅 RA",
    "settle_timeout_sec": "稳定超时",
    "settle_arcsec": "稳定阈值",
    "settle_time_sec": "稳定时间",
    "timeout_stop": "超时停止",
    "wait_guide_settle": "等待导星稳定",
    "ra_dec": "RA / DEC",
    "fov": "视场",
    "focal_len": "焦距",
    "angle": "角度",
    "image_id": "图像 ID",
    "star_number": "星点数",
    "duration_ms": "耗时",
    "image_size": "图像尺寸",
    "annotations": "标注",
    "stars": "星点",
    "pixelscale_arcsec": "像素尺度",
    "moon": "月亮",
    "sun": "太阳",
    "autogoto_exp_us": "自动 Goto 曝光",
    "continuous_preview": "连续预览",
    "goto_auto": "自动 Goto",
    "flat_auto_exp": "Flat 自动曝光",
    "guide_camera_name": "导星相机",
    "main_camera_name": "主相机",
    "goto_target_name": "Goto 目标",
    "goto_target_ra": "Goto RA",
    "goto_target_dec": "Goto DEC",
    "guide_rate": "导星速率",
    "new_solve_exec": "新版解算",
    "autogoto_threshold": "自动 Goto 阈值",
    "auto_open_heater": "自动开加热",
    "auto_open_cooler": "自动开制冷",
    "station_5g": "Station 5G",
    "plan_simulate": "计划模拟",
    "goto_simulate": "Goto 模拟",
}


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


def _format_control_value(name: str, value: Any, result: dict[str, Any]) -> str:
    if name in {"Temperature", "TargetTemp"}:
        return f"{value}°C"
    if name == "CoolPowerPerc":
        return f"{value}%"
    if name == "Exposure":
        return f"{value:g}s"
    if name in {"CoolerOn", "AntiDewHeater", "HardwareBin", "FanHalfSpeed", "LedOn"}:
        return "开启" if value else "关闭"
    if name == "FrameSize" and result.get("text"):
        return str(result["text"])
    return str(value)


def _plan_display(method: str, result: Any) -> dict[str, Any]:
    if isinstance(result, list):
        enabled = sum(1 for item in result if isinstance(item, dict) and item.get("enable"))
        return {"display_value": f"{len(result)} 项", "display_detail": f"{enabled} 项启用"}
    if isinstance(result, dict):
        name = result.get("group_name") or result.get("plan_name") or result.get("target_name")
        slots = result.get("slots")
        if isinstance(slots, list):
            return {"display_value": name or f"{len(slots)} 槽位", "display_detail": f"{len(slots)} 槽位"}
        return {"display_value": name or _short_json(result), "display_detail": "计划 / 序列"}
    return {"display_value": str(result), "display_detail": "计划 / 序列"}


def _focuser_display(method: str, result: Any) -> dict[str, Any]:
    if isinstance(result, list):
        return {"display_value": f"{len(result)} 个调焦器", "display_detail": ", ".join(_name_from(item) for item in result[:4])}
    if isinstance(result, dict):
        return {"display_value": result.get("name") or result.get("state") or _short_json(result), "display_detail": _short_json(result)}
    if method == "get_focuser_value":
        return {"display_value": f"{result}°C", "display_detail": "调焦器温度"}
    return {"display_value": str(result), "display_detail": "调焦器"}


def _wheel_display(method: str, result: Any) -> dict[str, Any]:
    if isinstance(result, list):
        return {"display_value": f"{len(result)} 项", "display_detail": _short_json(result)}
    if isinstance(result, dict):
        return {"display_value": result.get("state") or _short_json(result), "display_detail": _short_json(result)}
    return {"display_value": str(result), "display_detail": "滤轮"}


def _mount_display(method: str, result: Any) -> dict[str, Any]:
    if method == "scope_get_ra_dec" and isinstance(result, list):
        ra = _float_at(result, 0)
        dec = _float_at(result, 1)
        return {
            "display_value": f"{_format_ra_hours(ra) or '-'} / {_format_dec_degrees(dec) or '-'}",
            "display_detail": f"原始值 {_short_json(result)}",
        }
    if method == "scope_get_track_state":
        return {"display_value": "开启" if bool(result) else "关闭", "display_detail": "跟踪开关"}
    if method in {"scope_get_track_mode", "scope_get_slew_rate"}:
        return {"display_value": _choice_display(result), "display_detail": _short_json(result)}
    if method == "scope_is_moving":
        moving = {
            "none": "静止",
            "ra": "RA 轴移动",
            "dec": "Dec 轴移动",
            "both": "双轴移动",
        }.get(str(result).lower(), str(result))
        return {"display_value": moving, "display_detail": "移动状态"}
    if method == "scope_get_pierside":
        pier = {
            "pier_east": "东侧",
            "pier_west": "西侧",
            "east": "东侧",
            "west": "西侧",
        }.get(str(result).lower(), str(result))
        return {"display_value": pier, "display_detail": "赤道仪方位"}
    if method == "scope_get_location" and isinstance(result, list):
        lat = _float_at(result, 0)
        lon = _float_at(result, 1)
        return {
            "display_value": f"{lat:.4f}, {lon:.4f}" if lat is not None and lon is not None else _short_json(result),
            "display_detail": "纬度, 经度",
        }
    if method == "scope_get_cap" and isinstance(result, list):
        return {"display_value": f"{len(result)} 项能力", "display_detail": ", ".join(str(item) for item in result[:8])}
    return {"display_value": _short_json(result), "display_detail": "赤道仪"}


def _choice_display(value: Any) -> str:
    if not isinstance(value, dict):
        return _short_json(value)
    choices = value.get("list")
    index = value.get("index")
    if isinstance(choices, list):
        try:
            index_int = int(index)
        except (TypeError, ValueError):
            index_int = None
        if index_int is not None and 0 <= index_int < len(choices):
            return str(choices[index_int])
    return _short_json(value)


def _float_at(value: Any, index: int) -> float | None:
    if not isinstance(value, (list, tuple)) or index >= len(value):
        return None
    try:
        return float(value[index])
    except (TypeError, ValueError):
        return None


def _format_ra_hours(value: Any) -> str | None:
    try:
        hours_float = float(value) % 24.0
    except (TypeError, ValueError):
        return None
    total_tenths = int(round(hours_float * 36000))
    hours = (total_tenths // 36000) % 24
    minutes = (total_tenths % 36000) // 600
    seconds = (total_tenths % 600) / 10.0
    return f"{hours:02d}h {minutes:02d}m {seconds:04.1f}s"


def _format_dec_degrees(value: Any) -> str | None:
    try:
        degrees_float = float(value)
    except (TypeError, ValueError):
        return None
    sign = "-" if degrees_float < 0 else "+"
    total_tenths = int(round(abs(degrees_float) * 36000))
    degrees = total_tenths // 36000
    minutes = (total_tenths % 36000) // 600
    seconds = (total_tenths % 600) / 10.0
    return f"{sign}{degrees:02d}° {minutes:02d}' {seconds:04.1f}\""


def _network_summary(method: str, result: Any) -> str:
    if isinstance(result, list):
        return f"{len(result)} 项"
    if isinstance(result, dict):
        for key in ("state", "ssid", "ip", "mode", "name"):
            if result.get(key):
                return str(result[key])
        return f"{len(result)} 个字段"
    return str(result)


def _format_progress(value: dict[str, Any]) -> str:
    total = value.get("total")
    done = value.get("lapse", value.get("complete_num"))
    if total is None or done is None:
        return ""
    return f"{done}/{total}"


def _format_pair(value: Any) -> str:
    if isinstance(value, list) and len(value) >= 2:
        return f"{value[0]}×{value[1]}"
    return "-"


def _name_from(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("model") or value.get("type") or "-")
    return str(value)


def _mb_to_gb(value: Any) -> str:
    try:
        return f"{float(value) / 1024:.1f}"
    except (TypeError, ValueError):
        return "-"


def _percent(part: Any, total: Any) -> str:
    try:
        if float(total) == 0:
            return "-"
        return f"{float(part) / float(total) * 100:.1f}"
    except (TypeError, ValueError):
        return "-"


def _avg(values: list[Any]) -> float:
    numeric = [float(item) for item in values if isinstance(item, (int, float))]
    return sum(numeric) / len(numeric) if numeric else 0.0


def _short_json(value: Any, limit: int = 96) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else f"{text[:limit]}…"
