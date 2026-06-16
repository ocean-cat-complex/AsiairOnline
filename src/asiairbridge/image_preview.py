from __future__ import annotations

import json
import socket
import struct
import threading
import time
import warnings
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
IMAGE_PORTS = (4800, 4801)
IMAGE_METHOD = "get_current_img"
MAX_PACKET_BYTES = 192 * 1024 * 1024
IMAGE_FIRST_BYTE_TIMEOUT_SECONDS = 330.0
IMAGE_IDLE_TIMEOUT_SECONDS = 3.0
CACHE_MAX_AGE_SECONDS = 30.0
PREVIEW_MAX_EDGE = 2400
STF_HISTOGRAM_BINS = 4096
STF_BLACK_CLIP_SIGMA = -2.8
STF_TARGET_BACKGROUND = 0.25
STF_WHITE_PERCENTILE = 0.9995
STF_MIN_RANGE = 1.0 / 65535.0
STF_STRETCH_ALGORITHM = "STF-style median/MAD + midtones transfer"
DEBAYER_ALGORITHM = "colour-demosaicing bilinear Bayer demosaic after STF stretch"
DEFAULT_BAYER_PATTERN = "RGGB"

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
    storage_fallback: bool = False,
    storage_prefer: bool = False,
    fallback_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Compatibility only: older display clients passed these while storage fallback existed.
    del storage_fallback, storage_prefer
    capture_context = fallback_context
    device = _select_device(config, device_name)
    cache_dir = _cache_dir(config, device)
    png_path = cache_dir / "current.png"
    raw_path = cache_dir / "current.raw16be"
    meta_path = cache_dir / "current.json"
    cached = _read_metadata(meta_path)
    apply_capture_context = _should_apply_capture_context(capture_context)

    if not force and cached and png_path.is_file() and _cached_preview_usable(cached):
        cached = _metadata_with_capture_context(cached, meta_path, apply_capture_context, capture_context)
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
        if not force and cached and png_path.is_file() and _cached_preview_usable(cached):
            cached = _metadata_with_capture_context(cached, meta_path, apply_capture_context, capture_context)
            cached = _restretch_cached_preview_if_needed(cached, raw_path, png_path, meta_path)
            return _metadata_response(device, cached, refreshed=False)

        debayer_pattern = _refresh_debayer_pattern(capture_context, cached)
        frame = fetch_current_image(device)
        preview = build_preview_frame(
            frame,
            max_edge=PREVIEW_MAX_EDGE,
            preserve_bayer=bool(debayer_pattern),
        )
        normalized_raw = normalize_raw16be(preview.raw_data, preview.bytes_per_pixel)
        png_bytes, stretch = raw16_to_png(
            normalized_raw,
            preview.width,
            preview.height,
            debayer_pattern=debayer_pattern,
        )
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
                "is_color": bool(debayer_pattern),
                "debayer_pattern": debayer_pattern,
                "bayer_sample_preserved": bool(debayer_pattern),
                "stretch": stretch,
            },
            "image_url": _image_url(device, generated_at),
            "source": {
                "port": (frame.endpoint or {}).get("port", IMAGE_PORT),
                "ports": list(IMAGE_PORTS),
                "method": IMAGE_METHOD,
                "format": (
                    "asiair-header + zip(raw_data) + 16-bit Bayer"
                    if debayer_pattern
                    else "asiair-header + zip(raw_data) + 16-bit mono"
                ),
            },
        }
        if apply_capture_context:
            _apply_capture_context_metadata(metadata["image"], capture_context)
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
        for port in IMAGE_PORTS:
            try:
                with rpc_priority_session(
                    endpoint.ip,
                    port=port,
                    priority="image",
                    queue_timeout_seconds=8.0,
                ):
                    packet = _read_image_packet(endpoint.ip, port, payload)
                endpoint_payload = {**endpoint.as_dict(), "port": port}
                return _image_frame_from_packet(packet, endpoint_payload)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{endpoint.label} {endpoint.ip}:{port}: {exc}")
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


def build_preview_frame(
    frame: ImageFrame,
    max_edge: int = PREVIEW_MAX_EDGE,
    preserve_bayer: bool = False,
) -> PreviewFrame:
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
    if preserve_bayer:
        return _build_bayer_preview_frame(frame, step)

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


def _build_bayer_preview_frame(frame: ImageFrame, step: int) -> PreviewFrame:
    out_width = max(1, (frame.width + step - 1) // step)
    out_height = max(1, (frame.height + step - 1) // step)
    row_bytes = frame.width * frame.bytes_per_pixel
    reduced = bytearray(out_width * out_height * frame.bytes_per_pixel)
    x_positions = [
        _mapped_bayer_index(index, out_width, frame.width, index & 1)
        for index in range(out_width)
    ]
    y_positions = [
        _mapped_bayer_index(index, out_height, frame.height, index & 1)
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


def _mapped_bayer_index(index: int, out_count: int, source_count: int, parity: int) -> int:
    if source_count <= 1:
        return 0
    if out_count <= 1:
        candidate = 0
    else:
        candidate = round(index * (source_count - 1) / (out_count - 1))
    candidate = min(source_count - 1, max(0, candidate))
    if (candidate & 1) == parity:
        return candidate
    for delta in (-1, 1, -2, 2):
        adjusted = candidate + delta
        if 0 <= adjusted < source_count and (adjusted & 1) == parity:
            return adjusted
    return candidate


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


def raw16_to_png(
    raw_data: bytes,
    width: int,
    height: int,
    debayer_pattern: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    high_byte_offset = _detect_high_byte_offset(raw_data)
    total_pixels = width * height
    if len(raw_data) < total_pixels * 2:
        raise ValueError("raw_data is shorter than the requested image dimensions")

    normalized_pattern = _normalize_bayer_pattern(debayer_pattern)
    histogram = _raw16_histogram(raw_data, total_pixels, high_byte_offset, STF_HISTOGRAM_BINS)
    stretch = _stf_stretch_from_histogram(histogram, total_pixels, STF_HISTOGRAM_BINS)
    lut = _stf_lut(stretch)
    pixels = _apply_raw16_lut(raw_data, total_pixels, high_byte_offset, lut)
    stretch_metadata = {
        "source": "16-bit Bayer" if normalized_pattern else "16-bit mono",
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
    if normalized_pattern:
        rgb_pixels = _debayer_with_colour_demosaicing(pixels, width, height, normalized_pattern)
        stretch_metadata["debayer"] = {
            "pattern": normalized_pattern,
            "algorithm": DEBAYER_ALGORITHM,
        }
        return _png_rgb(width, height, rgb_pixels), stretch_metadata

    return _png_grayscale(width, height, pixels), stretch_metadata


def _apply_capture_context_metadata(image: dict[str, Any], capture_context: dict[str, Any] | None) -> None:
    if not capture_context:
        return
    if capture_context.get("exposure_seconds") is not None:
        try:
            exposure_seconds = float(capture_context["exposure_seconds"])
        except (TypeError, ValueError):
            exposure_seconds = 0.0
        if exposure_seconds > 0:
            image["exposure_seconds"] = exposure_seconds
            image["exposure_ms"] = int(round(exposure_seconds * 1000))
            image["exposure_source"] = "capture_context"
    if capture_context.get("bin") is not None:
        image["bin"] = capture_context["bin"]
    if capture_context.get("camera_is_color") is not None:
        image["is_color"] = bool(capture_context.get("camera_is_color"))
        if not image["is_color"]:
            image.pop("debayer_pattern", None)
    debayer_pattern = _capture_context_debayer_pattern(capture_context)
    if debayer_pattern:
        image["debayer_pattern"] = debayer_pattern


def _metadata_with_capture_context(
    metadata: dict[str, Any],
    meta_path: Path,
    apply_context: bool,
    capture_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if not apply_context:
        return metadata
    image = metadata.get("image")
    if not isinstance(image, dict):
        return metadata
    updated = dict(metadata)
    updated_image = dict(image)
    before = json.dumps(updated_image, sort_keys=True, default=str)
    _apply_capture_context_metadata(updated_image, capture_context)
    if json.dumps(updated_image, sort_keys=True, default=str) == before:
        return metadata
    updated["image"] = updated_image
    try:
        meta_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return updated
    return updated


def _should_apply_capture_context(capture_context: dict[str, Any] | None) -> bool:
    return bool(
        capture_context
        and (
            capture_context.get("enabled")
            or capture_context.get("camera_is_color") is not None
            or capture_context.get("debayer_pattern")
            or capture_context.get("bayer_pattern")
        )
    )


def _capture_context_debayer_pattern(capture_context: dict[str, Any] | None) -> str | None:
    if not capture_context or not bool(capture_context.get("camera_is_color")):
        return None
    return _normalize_bayer_pattern(
        capture_context.get("debayer_pattern")
        or capture_context.get("bayer_pattern")
        or DEFAULT_BAYER_PATTERN
    )


def _refresh_debayer_pattern(
    capture_context: dict[str, Any] | None,
    cached_metadata: dict[str, Any] | None,
) -> str | None:
    debayer_pattern = _capture_context_debayer_pattern(capture_context)
    if debayer_pattern:
        return debayer_pattern
    if capture_context and capture_context.get("camera_is_color") is False:
        return None
    if isinstance(cached_metadata, dict):
        return _metadata_debayer_pattern(cached_metadata)
    return None


def _cached_preview_usable(metadata: dict[str, Any]) -> bool:
    source = metadata.get("source")
    if not isinstance(source, dict):
        return True
    return source.get("type") != "storage-fallback"


def _preview_sample_step(width: int, height: int) -> int:
    return max(1, (max(int(width), int(height)) + PREVIEW_MAX_EDGE - 1) // PREVIEW_MAX_EDGE)


def _read_png_as_grayscale(data: bytes) -> tuple[int, int, bytes]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG signature missing")
    offset = 8
    width = height = bit_depth = color_type = compression = filter_method = interlace = None
    idat = bytearray()
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("PNG chunk is truncated")
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        chunk = data[offset + 8 : offset + 8 + size]
        offset += 12 + size
        if kind == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB",
                chunk[:13],
            )
        elif kind == b"IDAT":
            idat.extend(chunk)
        elif kind == b"IEND":
            break
    if width is None or height is None or bit_depth is None or color_type is None:
        raise ValueError("PNG IHDR missing")
    if bit_depth not in {8, 16}:
        raise ValueError(f"Unsupported PNG bit depth: {bit_depth}")
    if compression != 0 or filter_method != 0 or interlace != 0:
        raise ValueError("Unsupported PNG encoding")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise ValueError(f"Unsupported PNG color type: {color_type}")

    sample_bytes = 2 if bit_depth == 16 else 1
    bytes_per_pixel = channels * sample_bytes
    stride = width * bytes_per_pixel
    rows = zlib.decompress(bytes(idat))
    expected = (stride + 1) * height
    if len(rows) < expected:
        raise ValueError("PNG image data is shorter than expected")

    previous = bytearray(stride)
    grayscale = bytearray(width * height)
    output_offset = 0
    row_offset = 0
    for _row in range(height):
        filter_type = rows[row_offset]
        encoded = bytearray(rows[row_offset + 1 : row_offset + 1 + stride])
        row_offset += stride + 1
        decoded = _decode_png_filter(filter_type, encoded, previous, bytes_per_pixel)
        for x in range(width):
            pixel_offset = x * bytes_per_pixel
            grayscale[output_offset] = _png_pixel_to_luma(decoded, pixel_offset, channels, sample_bytes)
            output_offset += 1
        previous = decoded
    return width, height, bytes(grayscale)


def _decode_png_filter(filter_type: int, row: bytearray, previous: bytearray, bytes_per_pixel: int) -> bytearray:
    if filter_type == 0:
        return row
    decoded = bytearray(row)
    for index, value in enumerate(row):
        left = decoded[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
        up = previous[index] if index < len(previous) else 0
        up_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel and index < len(previous) else 0
        if filter_type == 1:
            prediction = left
        elif filter_type == 2:
            prediction = up
        elif filter_type == 3:
            prediction = (left + up) // 2
        elif filter_type == 4:
            prediction = _paeth_predictor(left, up, up_left)
        else:
            raise ValueError(f"Unsupported PNG filter: {filter_type}")
        decoded[index] = (value + prediction) & 0xFF
    return decoded


def _png_pixel_to_luma(row: bytearray, offset: int, channels: int, sample_bytes: int) -> int:
    def sample(index: int) -> int:
        position = offset + index * sample_bytes
        return row[position] if sample_bytes == 1 else row[position]

    if channels == 1:
        return sample(0)
    if channels == 2:
        return sample(0)
    red = sample(0)
    green = sample(1)
    blue = sample(2)
    return min(255, max(0, round((0.299 * red) + (0.587 * green) + (0.114 * blue))))


def _paeth_predictor(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    up_left_distance = abs(estimate - up_left)
    if left_distance <= up_distance and left_distance <= up_left_distance:
        return left
    if up_distance <= up_left_distance:
        return up
    return up_left


def _downsample_grayscale_pixels(pixels: bytes, width: int, height: int, step: int) -> tuple[bytes, int, int]:
    out_width = max(1, (width + step - 1) // step)
    out_height = max(1, (height + step - 1) // step)
    output = bytearray(out_width * out_height)
    out_index = 0
    for y in range(0, height, step):
        row_start = y * width
        for x in range(0, width, step):
            output[out_index] = pixels[row_start + x]
            out_index += 1
    return bytes(output), out_width, out_height


def _read_image_packet(ip: str, port: int, payload: bytes) -> bytes:
    started = time.monotonic()
    packet = b""
    saw_data_at: float | None = None
    with socket.create_connection((ip, port), timeout=4.0) as sock:
        sock.settimeout(1.0)
        sock.sendall(payload)
        while len(packet) < MAX_PACKET_BYTES:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                if saw_data_at is not None and time.monotonic() - saw_data_at >= IMAGE_IDLE_TIMEOUT_SECONDS:
                    break
                if saw_data_at is None and time.monotonic() - started >= IMAGE_FIRST_BYTE_TIMEOUT_SECONDS:
                    break
                continue
            if not chunk:
                break
            packet += chunk
            saw_data_at = time.monotonic()

    if not packet:
        raise TimeoutError(f"No image data returned from {ip}:{port}")
    return packet


def _png_grayscale(width: int, height: int, pixels: bytes) -> bytes:
    if len(pixels) != width * height:
        raise ValueError("PNG pixel buffer size does not match dimensions")
    return _png_bytes(width, height, pixels, channels=1)


def _png_rgb(width: int, height: int, pixels: bytes) -> bytes:
    if len(pixels) != width * height * 3:
        raise ValueError("RGB PNG pixel buffer size does not match dimensions")
    return _png_bytes(width, height, pixels, channels=3)


def _png_bytes(width: int, height: int, pixels: bytes, channels: int) -> bytes:
    if channels not in {1, 3}:
        raise ValueError(f"Unsupported PNG channel count: {channels}")

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = bytearray()
    stride = width * channels
    for y in range(height):
        start = y * stride
        rows.append(0)
        rows.extend(pixels[start : start + stride])

    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2 if channels == 3 else 0, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(bytes(rows), level=6)),
            chunk(b"IEND", b""),
        ]
    )


def _debayer_with_colour_demosaicing(pixels: bytes, width: int, height: int, pattern: str) -> bytes:
    if len(pixels) != width * height:
        raise ValueError("Bayer pixel buffer size does not match dimensions")
    try:
        import numpy as np

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message='.*"Matplotlib" related API features.*')
            warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"colour_demosaicing.*")
            from colour_demosaicing import demosaicing_CFA_Bayer_bilinear
    except ImportError as exc:
        raise ValueError("colour-demosaicing is required for Bayer debayer") from exc

    normalized = _normalize_bayer_pattern(pattern)
    if not normalized:
        raise ValueError(f"Unsupported Bayer pattern: {pattern}")
    cfa = np.frombuffer(pixels, dtype=np.uint8).reshape((height, width)).astype(np.float32) / 255.0
    rgb = demosaicing_CFA_Bayer_bilinear(cfa, pattern=normalized)
    rgb8 = np.clip(np.rint(rgb * 255.0), 0, 255).astype(np.uint8)
    return rgb8.tobytes()


def _normalize_bayer_pattern(pattern: Any) -> str | None:
    text = "".join(ch for ch in str(pattern or "").upper() if ch in {"R", "G", "B"})
    if len(text) >= 4:
        text = text[:4]
        if sorted(text) == ["B", "G", "G", "R"]:
            return text
    if len(text) >= 2:
        pair = text[:2]
        if pair == "RG":
            return "RGGB"
        if pair == "BG":
            return "BGGR"
        if pair == "GR":
            return "GRBG"
        if pair == "GB":
            return "GBRG"
    return None


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
        image = payload.get("image")
        if isinstance(image, dict) and image.get("raw_bytes"):
            payload["raw_url"] = _raw_url(device, str(generated_at))
        else:
            payload.pop("raw_url", None)
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
    if bool(image.get("is_color")) and image.get("bayer_sample_preserved") is not True:
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
        png_bytes, stretch = raw16_to_png(
            raw_data,
            width,
            height,
            debayer_pattern=_metadata_debayer_pattern(metadata),
        )
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
    uses_current_stretch = (
        stretch.get("algorithm") == STF_STRETCH_ALGORITHM
        and stretch.get("histogram_bins") == STF_HISTOGRAM_BINS
    )
    if not uses_current_stretch:
        return False
    if bool(image.get("is_color")):
        if image.get("bayer_sample_preserved") is not True:
            return False
        debayer = stretch.get("debayer")
        if not isinstance(debayer, dict):
            return False
        return (
            debayer.get("algorithm") == DEBAYER_ALGORITHM
            and debayer.get("pattern") == _metadata_debayer_pattern(metadata)
        )
    if isinstance(stretch.get("debayer"), dict):
        return False
    return True


def _metadata_debayer_pattern(metadata: dict[str, Any]) -> str | None:
    image = metadata.get("image")
    if not isinstance(image, dict) or not bool(image.get("is_color")):
        return None
    return _normalize_bayer_pattern(image.get("debayer_pattern") or DEFAULT_BAYER_PATTERN)


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
