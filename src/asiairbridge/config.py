from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config/devices.json")
RESERVED_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]+')


class ConfigError(ValueError):
    """Raised when the project configuration is invalid."""


@dataclass(frozen=True)
class DeviceEndpoint:
    label: str
    ip: str
    kind: str | None = None
    priority: int = 100
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "ip": self.ip,
            "kind": self.kind,
            "priority": self.priority,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class Device:
    name: str
    ip: str
    enabled: bool = True
    endpoints: tuple[DeviceEndpoint, ...] = ()
    source_roots: tuple["SourceRoot", ...] | None = None

    def endpoint_candidates(self) -> tuple[DeviceEndpoint, ...]:
        endpoints = [endpoint for endpoint in self.endpoints if endpoint.enabled]
        if not any(endpoint.ip == self.ip for endpoint in endpoints):
            endpoints.append(DeviceEndpoint("primary", self.ip, priority=0))
        endpoints.sort(key=lambda endpoint: (endpoint.priority, endpoint.label, endpoint.ip))
        return tuple(endpoints)

    def endpoint_ips(self) -> tuple[str, ...]:
        seen: set[str] = set()
        ips: list[str] = []
        for endpoint in self.endpoint_candidates():
            if endpoint.ip in seen:
                continue
            seen.add(endpoint.ip)
            ips.append(endpoint.ip)
        return tuple(ips)

    def endpoint_by_ip(self, ip: str) -> DeviceEndpoint | None:
        for endpoint in self.endpoint_candidates():
            if endpoint.ip == ip:
                return endpoint
        return None


@dataclass(frozen=True)
class SourceRoot:
    label: str
    path_template: str
    path_templates: dict[str, str] | None = None
    enabled: bool = True

    def render(self, device: Device) -> Path:
        return Path(self.render_text(device))

    def render_text(self, device: Device) -> str:
        template = _platform_value(self.path_templates, self.path_template)
        return str(template).format(ip=device.ip, name=device.name)

    @property
    def safe_label(self) -> str:
        cleaned = RESERVED_FILENAME_CHARS.sub("_", self.label).strip(" .")
        return cleaned or "source"


@dataclass(frozen=True)
class ProjectSettings:
    timezone: str
    destination_root: Path
    logs_dir: Path
    state_dir: Path
    lock_file: Path
    default_device: str | None = None
    private_path_prefixes: tuple[Path, ...] = ()


@dataclass(frozen=True)
class BackupSettings:
    dry_run_default: bool
    copy_empty_dirs: bool
    smb_port: int
    exclude_dirs: tuple[str, ...]
    exclude_files: tuple[str, ...]
    source_roots: tuple[SourceRoot, ...]


@dataclass(frozen=True)
class AppConfig:
    path: Path
    root: Path
    project: ProjectSettings
    backup: BackupSettings
    devices: tuple[Device, ...]

    def enabled_devices(self) -> tuple[Device, ...]:
        return tuple(device for device in self.devices if device.enabled)

    def default_device(self) -> Device:
        devices = self.enabled_devices()
        if not devices:
            raise ConfigError("At least one enabled device is required")
        if self.project.default_device:
            for device in devices:
                if device.name == self.project.default_device:
                    return device
        return devices[0]

    def get_devices(self, names: list[str] | None = None) -> tuple[Device, ...]:
        devices = self.enabled_devices()
        if not names:
            return devices
        requested = set(names)
        found = {device.name for device in devices}
        missing = sorted(requested - found)
        if missing:
            raise ConfigError(f"Unknown or disabled device(s): {', '.join(missing)}")
        return tuple(device for device in devices if device.name in requested)

    def source_roots_for(self, device: Device) -> tuple[SourceRoot, ...]:
        return device.source_roots if device.source_roots is not None else self.backup.source_roots

    def display_path(self, value: str | Path) -> str:
        text = str(value.resolve()) if isinstance(value, Path) else str(value)
        if not text:
            return ""
        normalized = _normalize_for_compare(text)
        prefixes = sorted(
            (_normalize_for_compare(str(prefix)) for prefix in self.project.private_path_prefixes),
            key=len,
            reverse=True,
        )
        for prefix in prefixes:
            if not prefix:
                continue
            if normalized == prefix:
                return "..."
            if normalized.startswith(f"{prefix}/"):
                return f".../{normalized[len(prefix) + 1:]}"
        return text

    def logs_path(self) -> Path:
        return self.project.logs_dir

    def state_path(self) -> Path:
        return self.project.state_dir


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    config_path = config_path.resolve()

    try:
        with config_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON: {exc}") from exc

    root = config_path.parent.parent if config_path.parent.name == "config" else config_path.parent
    project = _parse_project(raw.get("project", {}), root)
    backup = _parse_backup(raw.get("backup", {}))
    devices = _parse_devices(raw.get("devices", []))

    names = [device.name for device in devices]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(f"Duplicate device name(s): {', '.join(duplicates)}")
    if not devices:
        raise ConfigError("At least one device is required")

    return AppConfig(
        path=config_path,
        root=root,
        project=project,
        backup=backup,
        devices=devices,
    )


def _parse_project(raw: dict[str, Any], root: Path) -> ProjectSettings:
    private_path_prefixes = tuple(
        _resolve_path(root, _platform_value(item)) for item in raw.get("private_path_prefixes", [])
    )
    return ProjectSettings(
        timezone=str(raw.get("timezone", "Asia/Shanghai")),
        destination_root=_resolve_path(root, _platform_value(_required(raw, "destination_root"))),
        logs_dir=_resolve_path(root, _platform_value(raw.get("logs_dir", "logs"))),
        state_dir=_resolve_path(root, _platform_value(raw.get("state_dir", "state"))),
        lock_file=_resolve_path(root, _platform_value(raw.get("lock_file", "state/backup.lock"))),
        default_device=str(raw.get("default_device") or "") or None,
        private_path_prefixes=private_path_prefixes,
    )


def _parse_backup(raw: dict[str, Any]) -> BackupSettings:
    source_roots = tuple(_parse_source_root(item) for item in raw.get("source_roots", []))
    if not source_roots:
        raise ConfigError("backup.source_roots must contain at least one source")

    return BackupSettings(
        dry_run_default=bool(raw.get("dry_run_default", True)),
        copy_empty_dirs=bool(raw.get("copy_empty_dirs", True)),
        smb_port=int(raw.get("smb_port", 445)),
        exclude_dirs=tuple(str(item) for item in raw.get("exclude_dirs", [])),
        exclude_files=tuple(str(item) for item in raw.get("exclude_files", [])),
        source_roots=source_roots,
    )


def _parse_devices(raw: list[dict[str, Any]]) -> tuple[Device, ...]:
    devices: list[Device] = []
    for item in raw:
        source_roots = item.get("source_roots")
        ip = str(_required(item, "ip"))
        devices.append(
            Device(
                name=str(_required(item, "name")),
                ip=ip,
                enabled=bool(item.get("enabled", True)),
                endpoints=_parse_endpoints(item, ip),
                source_roots=(
                    tuple(_parse_source_root(source) for source in source_roots)
                    if source_roots is not None
                    else None
                ),
            )
        )
    return tuple(devices)


def _parse_endpoints(raw: dict[str, Any], primary_ip: str) -> tuple[DeviceEndpoint, ...]:
    endpoint_items = raw.get("endpoints")
    if endpoint_items is None:
        endpoint_items = raw.get("ips")

    endpoints: list[DeviceEndpoint] = []
    if endpoint_items is None:
        endpoints.append(DeviceEndpoint("primary", primary_ip, priority=0))
    elif isinstance(endpoint_items, list):
        for index, item in enumerate(endpoint_items):
            if isinstance(item, str):
                endpoints.append(
                    DeviceEndpoint(
                        "primary" if item == primary_ip else f"endpoint-{index + 1}",
                        item,
                        priority=0 if item == primary_ip else 100 + index,
                    )
                )
            elif isinstance(item, dict):
                ip = str(_required(item, "ip"))
                endpoints.append(
                    DeviceEndpoint(
                        label=str(item.get("label") or ("primary" if ip == primary_ip else f"endpoint-{index + 1}")),
                        ip=ip,
                        kind=str(item.get("kind") or "") or None,
                        priority=int(item.get("priority", 0 if ip == primary_ip else 100 + index)),
                        enabled=bool(item.get("enabled", True)),
                    )
                )
            else:
                raise ConfigError("device.endpoints entries must be strings or objects")
    else:
        raise ConfigError("device.endpoints must be a list when provided")

    if not any(endpoint.ip == primary_ip for endpoint in endpoints):
        endpoints.append(DeviceEndpoint("primary", primary_ip, priority=0))

    deduped: dict[str, DeviceEndpoint] = {}
    for endpoint in sorted(endpoints, key=lambda item: (item.priority, item.label, item.ip)):
        deduped.setdefault(endpoint.ip, endpoint)
    return tuple(deduped.values())


def _parse_source_root(raw: dict[str, Any]) -> SourceRoot:
    path_templates = raw.get("path_templates") or raw.get("path_template_by_platform")
    if path_templates is not None and not isinstance(path_templates, dict):
        raise ConfigError("source_root.path_templates must be an object when provided")
    return SourceRoot(
        label=str(_required(raw, "label")),
        path_template=str(_required(raw, "path_template")),
        path_templates=(
            {str(key): str(value) for key, value in path_templates.items()}
            if path_templates is not None
            else None
        ),
        enabled=bool(raw.get("enabled", True)),
    )


def _required(raw: dict[str, Any], key: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise ConfigError(f"Missing required config key: {key}")
    return raw[key]


def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (root / path).resolve()


def _platform_value(value: Any, default: Any | None = None) -> Any:
    if value is None:
        return default
    if not isinstance(value, dict):
        return value
    for key in _platform_keys():
        if key in value:
            return value[key]
    if "default" in value:
        return value["default"]
    return default if default is not None else next(iter(value.values()))


def _platform_keys() -> tuple[str, ...]:
    keys: list[str] = []
    if sys.platform == "darwin":
        keys.extend(["darwin", "macos", "posix"])
    elif sys.platform.startswith("linux"):
        keys.extend(["linux", "posix"])
    else:
        keys.extend([sys.platform, "posix"])
    keys.append("default")
    return tuple(dict.fromkeys(keys))


def _normalize_for_compare(value: str) -> str:
    return value.replace("\\", "/").rstrip("/")
