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
STF_HISTOGRAM_BINS = 4096
STF_BLACK_CLIP_SIGMA = -2.8
STF_TARGET_BACKGROUND = 0.25
STF_WHITE_PERCENTILE = 0.9995
STF_MIN_RANGE = 1.0 / 65535.0
STF_STRETCH_ALGORITHM = "STF-style median/MAD + midtones transfer"

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
        cached = _restretch_cached_preview_if_needed(cached, raw_path, png_path, meta_path)
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
            cached = _restretch_cached_preview_if_needed(cached, raw_path, png_path, meta_path)
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
            "preview_generated_at": generated_at,
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
    total_pixels = width * height
    if len(raw_data) < total_pixels * 2:
        raise ValueError("raw_data is shorter than the requested image dimensions")

    histogram = _raw16_histogram(raw_data, total_pixels, high_byte_offset, STF_HISTOGRAM_BINS)
    stretch = _stf_stretch_from_histogram(histogram, total_pixels, STF_HISTOGRAM_BINS)
    lut = _stf_lut(stretch)
    pixels = _apply_raw16_lut(raw_data, total_pixels, high_byte_offset, lut)

    return _png_grayscale(width, height, pixels), {
        "source": "16-bit mono",
        "algorithm": STF_STRETCH_ALGORITHM,
        "byte_order": "little" if high_byte_offset == 1 else "big",
        "low": round(stretch["black"] * 65535),
        "high": round(stretch["white"] * 65535),
        "black": round(stretch["black"] * 65535),
        "white": round(stretch["white"] * 65535),
        "median": round(stretch["median"] * 65535),
        "mad": round(stretch["mad"] * 65535),
        "sigma": round(stretch["sigma"] * 65535),
        "midtone": stretch["midtone"],
        "source_background": stretch["source_background"],
        "target_background": STF_TARGET_BACKGROUND,
        "black_clip_sigma": STF_BLACK_CLIP_SIGMA,
        "white_percentile": STF_WHITE_PERCENTILE,
        "histogram_bins": STF_HISTOGRAM_BINS,
        "percentiles": "STF median/MAD; white 99.95%",
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


def _raw16_histogram(raw_data: bytes, total_pixels: int, high_byte_offset: int, bins: int) -> list[int]:
    histogram = [0] * bins
    for pair_start in range(0, total_pixels * 2, 2):
        if high_byte_offset == 0:
            value = (raw_data[pair_start] << 8) | raw_data[pair_start + 1]
        else:
            value = (raw_data[pair_start + 1] << 8) | raw_data[pair_start]
        histogram[min(bins - 1, (value * bins) >> 16)] += 1
    return histogram


def _stf_stretch_from_histogram(histogram: list[int], total: int, bins: int) -> dict[str, float]:
    median = _hist_percentile_normalized(histogram, total, 0.5, bins)
    mad = _hist_mad_normalized(histogram, total, median, bins)
    sigma = 1.4826 * mad

    if sigma > 0:
        black = max(0.0, min(0.98, median + (STF_BLACK_CLIP_SIGMA * sigma)))
    else:
        black = _hist_percentile_normalized(histogram, total, 0.001, bins)

    white = _hist_percentile_normalized(histogram, total, STF_WHITE_PERCENTILE, bins)
    if not _finite_number(white) or white <= black + STF_MIN_RANGE:
        white = _hist_percentile_normalized(histogram, total, 0.9999, bins)
    if not _finite_number(white) or white <= black + STF_MIN_RANGE:
        white = 1.0

    source_background = _clamp((median - black) / max(STF_MIN_RANGE, white - black), 0.001, 0.999)
    midtone = _midtone_for_target(STF_TARGET_BACKGROUND, source_background)
    return {
        "black": _clamp(black, 0.0, 0.999),
        "white": _clamp(white, black + STF_MIN_RANGE, 1.0),
        "median": _clamp(median, 0.0, 1.0),
        "mad": max(0.0, mad),
        "sigma": max(0.0, sigma),
        "source_background": source_background,
        "midtone": midtone,
    }


def _hist_percentile_normalized(histogram: list[int], total: int, percentile: float, bins: int) -> float:
    threshold = max(1, int(total * percentile))
    running = 0
    for index, count in enumerate(histogram):
        running += count
        if running >= threshold:
            return (index + 0.5) / bins
    return 1.0


def _hist_mad_normalized(histogram: list[int], total: int, median: float, bins: int) -> float:
    median_bin = min(bins - 1, max(0, round(median * (bins - 1))))
    threshold = max(1, int(total * 0.5))
    running = histogram[median_bin]
    for distance in range(1, bins):
        left = median_bin - distance
        right = median_bin + distance
        if left >= 0:
            running += histogram[left]
        if right < bins:
            running += histogram[right]
        if running >= threshold:
            return distance / bins
    return 0.0


def _stf_lut(stretch: dict[str, float]) -> bytes:
    black = stretch["black"]
    white = stretch["white"]
    midtone = stretch["midtone"]
    inv_range = 1.0 / max(STF_MIN_RANGE, white - black)
    lut = bytearray(65536)
    for value in range(65536):
        normalized = ((value / 65535.0) - black) * inv_range
        if normalized <= 0:
            output = 0
        elif normalized >= 1:
            output = 255
        else:
            output = round(_midtone_transfer(midtone, normalized) * 255)
        lut[value] = min(255, max(0, output))
    return bytes(lut)


def _apply_raw16_lut(raw_data: bytes, total_pixels: int, high_byte_offset: int, lut: bytes) -> bytes:
    pixels = bytearray(total_pixels)
    out_index = 0
    for pair_start in range(0, total_pixels * 2, 2):
        if high_byte_offset == 0:
            value = (raw_data[pair_start] << 8) | raw_data[pair_start + 1]
        else:
            value = (raw_data[pair_start + 1] << 8) | raw_data[pair_start]
        pixels[out_index] = lut[value]
        out_index += 1
    return bytes(pixels)


def _midtone_for_target(target: float, source: float) -> float:
    target = _clamp(target, 0.001, 0.999)
    source = _clamp(source, 0.001, 0.999)
    denominator = target + source - (2 * target * source)
    if abs(denominator) < 1e-12:
        return 0.5
    return _clamp((source * (1 - target)) / denominator, 0.001, 0.999)


def _midtone_transfer(midtone: float, value: float) -> float:
    midtone = _clamp(midtone, 0.001, 0.999)
    value = _clamp(value, 0.0, 1.0)
    if value <= 0:
        return 0.0
    if value >= 1:
        return 1.0
    denominator = (((2 * midtone) - 1) * value) - midtone
    if abs(denominator) < 1e-12:
        return 0.0
    return _clamp(((midtone - 1) * value) / denominator, 0.0, 1.0)


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), lower), upper)


def _finite_number(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


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
    preview_generated_at = payload.get("preview_generated_at")
    payload["age_seconds"] = _age_seconds(generated_at)
    if generated_at:
        payload["image_url"] = _image_url(device, str(preview_generated_at or generated_at))
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


def _restretch_cached_preview_if_needed(
    metadata: dict[str, Any],
    raw_path: Path,
    png_path: Path,
    meta_path: Path,
) -> dict[str, Any]:
    if _metadata_uses_current_stretch(metadata):
        return metadata
    if not raw_path.is_file():
        return metadata

    image = metadata.get("image")
    if not isinstance(image, dict):
        return metadata
    try:
        width = int(image.get("width") or 0)
        height = int(image.get("height") or 0)
    except (TypeError, ValueError):
        return metadata
    if width <= 0 or height <= 0:
        return metadata

    try:
        raw_data = raw_path.read_bytes()
        png_bytes, stretch = raw16_to_png(raw_data, width, height)
        png_path.write_bytes(png_bytes)
    except (OSError, ValueError):
        return metadata

    updated = dict(metadata)
    updated_image = dict(image)
    updated_image["png_bytes"] = len(png_bytes)
    updated_image["source_byte_order"] = stretch.get("byte_order")
    updated_image["stretch"] = stretch
    updated["image"] = updated_image
    updated["preview_generated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        meta_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return metadata
    return updated


def _metadata_uses_current_stretch(metadata: dict[str, Any]) -> bool:
    image = metadata.get("image")
    if not isinstance(image, dict):
        return False
    stretch = image.get("stretch")
    if not isinstance(stretch, dict):
        return False
    if not isinstance(metadata.get("preview_generated_at"), str):
        return False
    return (
        stretch.get("algorithm") == STF_STRETCH_ALGORITHM
        and stretch.get("histogram_bins") == STF_HISTOGRAM_BINS
    )


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
