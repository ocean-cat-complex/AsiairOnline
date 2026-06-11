from __future__ import annotations

import json
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from .config import AppConfig, Device
from .rpc import rpc_priority_session


IMAGE_PORT = 4800
IMAGE_METHOD = "get_current_img"
MAX_PACKET_BYTES = 192 * 1024 * 1024
CACHE_MAX_AGE_SECONDS = 30.0
PREVIEW_MAX_EDGE = 2400

_PREVIEW_LOCKS: dict[str, threading.Lock] = {}
_PREVIEW_LOCKS_GUARD = threading.Lock()
_REQUEST_ID = 50_000


@dataclass(frozen=True)
class ImageFrame:
    width: int
    height: int
    image_id: int | None
    bin_value: int | None
    exposure_ms: int | None
    bytes_per_pixel: int
    packet_bytes: int
    zip_bytes: int
    raw_bytes: int
    raw_data: bytes
    endpoint: dict[str, Any] | None = None


@dataclass(frozen=True)
class PreviewFrame:
    width: int
    height: int
    raw_data: bytes
    raw_bytes: int
    sample_step: int
    bytes_per_pixel: int


def current_image_response(
    config: AppConfig,
    device_name: str | None,
    force: bool = False,
) -> dict[str, Any]:
    device = _select_device(config, device_name)
    cache_dir = _cache_dir(config, device)
    png_path = cache_dir / "current.png"
    raw_path = cache_dir / "current.raw16be"
    meta_path = cache_dir / "current.json"
    cached = _read_metadata(meta_path)

    if not force and cached and png_path.is_file():
        return _metadata_response(device, cached, refreshed=False)

    if not force and not png_path.is_file():
        return {
            "ok": False,
            "device": {"name": device.name, "ip": device.ip},
            "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
            "error": "no cached preview image",
            "needs_refresh": True,
        }

    with _preview_lock(device.name):
        cached = _read_metadata(meta_path)
        if not force and cached and png_path.is_file():
            return _metadata_response(device, cached, refreshed=False)

        frame = fetch_current_image(device)
        preview = build_preview_frame(frame, max_edge=PREVIEW_MAX_EDGE)
        normalized_raw = normalize_raw16be(preview.raw_data, preview.bytes_per_pixel)
        png_bytes, stretch = raw16_to_png(normalized_raw, preview.width, preview.height)
        cache_dir.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(png_bytes)
        raw_path.write_bytes(normalized_raw)

        generated_at = datetime.now().isoformat(timespec="seconds")
        metadata = {
            "ok": True,
            "device": {"name": device.name, "ip": device.ip},
            "endpoints": [endpoint.as_dict() for endpoint in device.endpoint_candidates()],
            "endpoint": frame.endpoint,
            "generated_at": generated_at,
            "image": {
                "width": preview.width,
                "height": preview.height,
                "original_width": frame.width,
                "original_height": frame.height,
                "sample_step": preview.sample_step,
                "image_id": frame.image_id,
                "bin": frame.bin_value,
                "exposure_ms": frame.exposure_ms,
                "packet_bytes": frame.packet_bytes,
                "zip_bytes": frame.zip_bytes,
                "raw_bytes": len(normalized_raw),
                "source_raw_bytes": frame.raw_bytes,
                "png_bytes": len(png_bytes),
                "byte_order": "big",
                "source_byte_order": stretch.get("byte_order"),
                "source_bytes_per_pixel": frame.bytes_per_pixel,
                "stretch": stretch,
            },
            "image_url": _image_url(device, generated_at),
            "source": {
                "port": IMAGE_PORT,
                "method": IMAGE_METHOD,
                "format": "asiair-header + zip(raw_data) + 16-bit mono",
            },
        }
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return _metadata_response(device, metadata, refreshed=True)


def cached_image_path(config: AppConfig, device_name: str | None) -> Path:
    device = _select_device(config, device_name)
    return _cache_dir(config, device) / "current.png"


def cached_raw_path(config: AppConfig, device_name: str | None) -> Path:
    device = _select_device(config, device_name)
    return _cache_dir(config, device) / "current.raw16be"


def fetch_current_image(device: Device) -> ImageFrame:
    global _REQUEST_ID

    _REQUEST_ID += 1
    request = {"id": _REQUEST_ID, "method": IMAGE_METHOD}
    payload = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\r\n"
    errors: list[str] = []
    for endpoint in device.endpoint_candidates():
        try:
            with rpc_priority_session(
                endpoint.ip,
                port=IMAGE_PORT,
                priority="image",
                queue_timeout_seconds=8.0,
            ):
                packet = _read_image_packet(endpoint.ip, payload)
            return _image_frame_from_packet(packet, endpoint.as_dict())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{endpoint.label} {endpoint.ip}: {exc}")
    raise TimeoutError(f"ASIAIR image fetch failed for {device.name}; {'; '.join(errors)}")


def _image_frame_from_packet(packet: bytes, endpoint: dict[str, Any] | None = None) -> ImageFrame:
    zip_offset = packet.find(b"PK\x03\x04")
    if zip_offset < 0:
        if b"there is no image now" in packet:
            raise ValueError("ASIAIR reported there is no image now")
        raise ValueError("ASIAIR image packet did not contain a ZIP payload")

    header = packet[:zip_offset]
    if len(header) < 32:
        raise ValueError(f"ASIAIR image header is too short: {len(header)} bytes")

    width = int.from_bytes(header[16:18], "big")
    height = int.from_bytes(header[18:20], "big")
    exposure_ms = int.from_bytes(header[24:26], "big")
    image_id = int.from_bytes(header[28:30], "big")
    bin_value = int.from_bytes(header[30:32], "big")

    with ZipFile(BytesIO(packet[zip_offset:])) as archive:
        raw_data = archive.read("raw_data")

    pixel_count = width * height
    if len(raw_data) == pixel_count * 2:
        bytes_per_pixel = 2
    elif len(raw_data) == pixel_count:
        bytes_per_pixel = 1
    else:
        raise ValueError(
            f"Unexpected raw_data size: got {len(raw_data)} bytes for {width}x{height}; "
            f"expected {pixel_count} (8-bit) or {pixel_count * 2} (16-bit)"
        )

    return ImageFrame(
        width=width,
        height=height,
        image_id=image_id,
        bin_value=bin_value,
        exposure_ms=exposure_ms,
        bytes_per_pixel=bytes_per_pixel,
        packet_bytes=len(packet),
        zip_bytes=len(packet) - zip_offset,
        raw_bytes=len(raw_data),
        raw_data=raw_data,
        endpoint=endpoint,
    )


def build_preview_frame(frame: ImageFrame, max_edge: int = PREVIEW_MAX_EDGE) -> PreviewFrame:
    longest = max(frame.width, frame.height)
    step = max(1, (longest + max_edge - 1) // max_edge)
    if step == 1:
        return PreviewFrame(
            width=frame.width,
            height=frame.height,
            raw_data=frame.raw_data,
            raw_bytes=frame.raw_bytes,
            sample_step=1,
            bytes_per_pixel=frame.bytes_per_pixel,
        )

    out_width = max(1, (frame.width + step - 1) // step)
    out_height = max(1, (frame.height + step - 1) // step)
    row_bytes = frame.width * frame.bytes_per_pixel
    reduced = bytearray(out_width * out_height * frame.bytes_per_pixel)
    x_positions = [
        0 if out_width == 1 else round(index * (frame.width - 1) / (out_width - 1))
        for index in range(out_width)
    ]
    y_positions = [
        0 if out_height == 1 else round(index * (frame.height - 1) / (out_height - 1))
        for index in range(out_height)
    ]
    out_index = 0
    for y in y_positions:
        row_start = y * row_bytes
        row = frame.raw_data[row_start : row_start + row_bytes]
        for x in x_positions:
            pixel_start = x * frame.bytes_per_pixel
            pixel_end = pixel_start + frame.bytes_per_pixel
            reduced[out_index : out_index + frame.bytes_per_pixel] = row[pixel_start:pixel_end]
            out_index += frame.bytes_per_pixel

    return PreviewFrame(
        width=out_width,
        height=out_height,
        raw_data=bytes(reduced),
        raw_bytes=len(reduced),
        sample_step=step,
        bytes_per_pixel=frame.bytes_per_pixel,
    )


def normalize_raw16be(raw_data: bytes, bytes_per_pixel: int) -> bytes:
    if bytes_per_pixel == 2:
        return raw_data
    if bytes_per_pixel != 1:
        raise ValueError(f"Unsupported bytes_per_pixel: {bytes_per_pixel}")
    normalized = bytearray(len(raw_data) * 2)
    out_index = 0
    for value in raw_data:
        normalized[out_index] = value
        normalized[out_index + 1] = value
        out_index += 2
    return bytes(normalized)


def raw16_to_png(raw_data: bytes, width: int, height: int) -> tuple[bytes, dict[str, Any]]:
    high_byte_offset = _detect_high_byte_offset(raw_data)
    high_bytes = raw_data[high_byte_offset::2]
    histogram = [0] * 256
    for value in high_bytes:
        histogram[value] += 1

    total = len(high_bytes)
    low = _hist_percentile(histogram, total, 0.01)
    high = _hist_percentile(histogram, total, 0.995)
    if high <= low:
        low, high = min(high_bytes), max(high_bytes)
    if high <= low:
        pixels = bytes(255 if value >= high else 0 for value in high_bytes)
    else:
        scale = 255.0 / (high - low)
        pixels = bytes(
            0 if value <= low else 255 if value >= high else int((value - low) * scale)
            for value in high_bytes
        )

    return _png_grayscale(width, height, pixels), {
        "source": "16-bit mono high byte",
        "byte_order": "little" if high_byte_offset == 1 else "big",
        "low": low,
        "high": high,
        "percentiles": "1%-99.5%",
    }


def _read_image_packet(ip: str, payload: bytes) -> bytes:
    started = time.monotonic()
    packet = b""
    saw_data_at: float | None = None
    with socket.create_connection((ip, IMAGE_PORT), timeout=4.0) as sock:
        sock.settimeout(1.2)
        sock.sendall(payload)
        while len(packet) < MAX_PACKET_BYTES:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                if saw_data_at is not None and time.monotonic() - saw_data_at >= 3.0:
                    break
                if time.monotonic() - started >= 45.0:
                    break
                continue
            if not chunk:
                break
            packet += chunk
            saw_data_at = time.monotonic()

    if not packet:
        raise TimeoutError(f"No image data returned from {ip}:{IMAGE_PORT}")
    return packet


def _png_grayscale(width: int, height: int, pixels: bytes) -> bytes:
    if len(pixels) != width * height:
        raise ValueError("PNG pixel buffer size does not match dimensions")

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = bytearray()
    for y in range(height):
        start = y * width
        rows.append(0)
        rows.extend(pixels[start : start + width])

    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(bytes(rows), level=6)),
            chunk(b"IEND", b""),
        ]
    )


def _hist_percentile(histogram: list[int], total: int, percentile: float) -> int:
    threshold = max(1, int(total * percentile))
    running = 0
    for index, count in enumerate(histogram):
        running += count
        if running >= threshold:
            return index
    return len(histogram) - 1


def _detect_high_byte_offset(raw_data: bytes) -> int:
    even_hist = [0] * 256
    odd_hist = [0] * 256
    even_bytes = raw_data[0::2]
    odd_bytes = raw_data[1::2]
    for value in even_bytes:
        even_hist[value] += 1
    for value in odd_bytes:
        odd_hist[value] += 1

    def score(hist: list[int]) -> float:
        total = sum(hist)
        mean = total / len(hist)
        return sum((count - mean) ** 2 for count in hist)

    return 1 if score(odd_hist) > score(even_hist) else 0


def _metadata_response(device: Device, metadata: dict[str, Any], refreshed: bool) -> dict[str, Any]:
    payload = dict(metadata)
    payload["device"] = {"name": device.name, "ip": device.ip}
    payload["endpoints"] = [endpoint.as_dict() for endpoint in device.endpoint_candidates()]
    payload["refreshed"] = refreshed
    generated_at = payload.get("generated_at")
    payload["age_seconds"] = _age_seconds(generated_at)
    if generated_at:
        payload["image_url"] = _image_url(device, str(generated_at))
        payload["raw_url"] = _raw_url(device, str(generated_at))
    return payload


def _image_url(device: Device, version: str) -> str:
    return f"/api/current-image-file?device={device.name}&v={version}"


def _raw_url(device: Device, version: str) -> str:
    return f"/api/current-image-raw?device={device.name}&v={version}"


def _age_seconds(generated_at: Any) -> float | None:
    if not isinstance(generated_at, str):
        return None
    try:
        dt = datetime.fromisoformat(generated_at)
    except ValueError:
        return None
    return round((datetime.now() - dt).total_seconds(), 1)


def _cache_dir(config: AppConfig, device: Device) -> Path:
    return config.state_path() / "image-preview" / device.name


def _read_metadata(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _preview_lock(device_name: str) -> threading.Lock:
    with _PREVIEW_LOCKS_GUARD:
        lock = _PREVIEW_LOCKS.get(device_name)
        if lock is None:
            lock = threading.Lock()
            _PREVIEW_LOCKS[device_name] = lock
        return lock


def _select_device(config: AppConfig, device_name: str | None) -> Device:
    devices = config.enabled_devices()
    if device_name:
        for device in devices:
            if device.name == device_name:
                return device
        raise ValueError(f"Unknown or disabled device: {device_name}")
    return config.default_device()
