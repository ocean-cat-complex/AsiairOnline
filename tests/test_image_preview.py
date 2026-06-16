from __future__ import annotations

import json
import struct
import tempfile
import unittest
import zlib
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from asiairbridge.config import load_config
from asiairbridge.image_preview import (
    ImageFrame,
    _debayer_with_colour_demosaicing,
    _png_grayscale,
    _restretch_cached_preview_if_needed,
    build_preview_frame,
    cached_image_path,
    current_image_response,
    fetch_current_image,
    raw16_to_png,
)


class ImagePreviewStretchTests(unittest.TestCase):
    def test_raw16_to_png_uses_stf_midtone_stretch(self) -> None:
        width = 64
        height = 32
        values: list[int] = []
        for y in range(height):
            for x in range(width):
                value = 980 + ((x * 7 + y * 11) % 80)
                if (x, y) in {(12, 8), (31, 12), (50, 25)}:
                    value = 28000
                values.append(value)

        raw = b"".join(value.to_bytes(2, "little") for value in values)
        png, stretch = raw16_to_png(raw, width, height)
        pixels = _decode_grayscale_png(png, width, height)

        self.assertEqual(stretch["byte_order"], "little")
        self.assertEqual(stretch["algorithm"], "STF-style median/MAD + midtones transfer")
        self.assertLess(stretch["black"], stretch["median"])
        self.assertGreater(stretch["white"], stretch["median"])
        self.assertGreater(max(pixels), 240)
        background = sorted(pixels)[len(pixels) // 2]
        self.assertGreater(background, 35)
        self.assertLess(background, 120)

    def test_colour_demosaicing_interpolates_color_bayer_data(self) -> None:
        width = 6
        height = 6
        pixels = bytearray()
        for y in range(height):
            for x in range(width):
                if y % 2 == 0 and x % 2 == 0:
                    pixels.append(240)
                elif y % 2 == 1 and x % 2 == 1:
                    pixels.append(20)
                else:
                    pixels.append(120)

        rgb = _debayer_with_colour_demosaicing(bytes(pixels), width, height, "RG")

        red_values = rgb[0::3]
        green_values = rgb[1::3]
        blue_values = rgb[2::3]
        self.assertGreater(sum(red_values) / len(red_values), sum(green_values) / len(green_values))
        self.assertGreater(sum(green_values) / len(green_values), sum(blue_values) / len(blue_values))

    def test_bayer_preview_downsample_preserves_cfa_parity(self) -> None:
        width = 8
        height = 8
        raw_data = bytearray()
        for y in range(height):
            for x in range(width):
                if y % 2 == 0 and x % 2 == 0:
                    raw_data.append(240)
                elif y % 2 == 1 and x % 2 == 1:
                    raw_data.append(20)
                else:
                    raw_data.append(120)
        frame = ImageFrame(
            width=width,
            height=height,
            image_id=1,
            bin_value=1,
            exposure_ms=1000,
            bytes_per_pixel=1,
            packet_bytes=len(raw_data),
            zip_bytes=len(raw_data),
            raw_bytes=len(raw_data),
            raw_data=bytes(raw_data),
            endpoint={"label": "primary", "ip": "192.168.8.10", "port": 4800},
        )

        preview = build_preview_frame(frame, max_edge=3, preserve_bayer=True)

        self.assertEqual(preview.sample_step, 3)
        for y in range(preview.height):
            for x in range(preview.width):
                value = preview.raw_data[(y * preview.width) + x]
                if y % 2 == 0 and x % 2 == 0:
                    self.assertEqual(value, 240)
                elif y % 2 == 1 and x % 2 == 1:
                    self.assertEqual(value, 20)
                else:
                    self.assertEqual(value, 120)

    def test_raw16_to_png_debayers_color_bayer_data(self) -> None:
        width = 6
        height = 6
        values: list[int] = []
        for y in range(height):
            for x in range(width):
                values.append(1000 + ((x * 113 + y * 251) % 9000))
        raw = b"".join(value.to_bytes(2, "little") for value in values)

        png, stretch = raw16_to_png(raw, width, height, debayer_pattern="RG")
        pixels = _decode_rgb_png(png, width, height)

        self.assertEqual(stretch["source"], "16-bit Bayer")
        self.assertEqual(stretch["debayer"]["pattern"], "RGGB")
        self.assertEqual(stretch["debayer"]["algorithm"], "colour-demosaicing bilinear Bayer demosaic after STF stretch")
        self.assertEqual(len(pixels), width * height * 3)

    def test_cached_preview_is_restretched_when_metadata_uses_old_algorithm(self) -> None:
        width = 12
        height = 8
        values = [900 + ((index * 17) % 70) for index in range(width * height)]
        values[-1] = 24000
        raw = b"".join(value.to_bytes(2, "little") for value in values)

        with tempfile.TemporaryDirectory(prefix="asiair-preview-test-") as tmp:
            root = Path(tmp)
            raw_path = root / "current.raw16be"
            png_path = root / "current.png"
            meta_path = root / "current.json"
            raw_path.write_bytes(raw)
            png_path.write_bytes(b"old")
            metadata = {
                "ok": True,
                "generated_at": "2026-06-14T01:00:00",
                "image": {
                    "width": width,
                    "height": height,
                    "png_bytes": 3,
                    "stretch": {"source": "16-bit mono high byte", "percentiles": "1%-99.5%"},
                },
            }

            updated = _restretch_cached_preview_if_needed(metadata, raw_path, png_path, meta_path)

            self.assertEqual(updated["image"]["stretch"]["algorithm"], "STF-style median/MAD + midtones transfer")
            self.assertIn("preview_generated_at", updated)
            self.assertGreater(updated["image"]["png_bytes"], 3)
            pixels = _decode_grayscale_png(png_path.read_bytes(), width, height)
            self.assertGreater(max(pixels), 240)

    def test_color_cache_without_bayer_preserved_marker_is_not_restretched_from_cached_raw(self) -> None:
        width = 6
        height = 6
        raw = b"".join((1000 + index).to_bytes(2, "little") for index in range(width * height))

        with tempfile.TemporaryDirectory(prefix="asiair-color-cache-marker-test-") as tmp:
            root = Path(tmp)
            raw_path = root / "current.raw16be"
            png_path = root / "current.png"
            meta_path = root / "current.json"
            raw_path.write_bytes(raw)
            png_path.write_bytes(b"old-color-preview")
            metadata = {
                "ok": True,
                "generated_at": "2026-06-17T01:00:00",
                "preview_generated_at": "2026-06-17T01:00:00",
                "image": {
                    "width": width,
                    "height": height,
                    "is_color": True,
                    "debayer_pattern": "RGGB",
                    "png_bytes": len(b"old-color-preview"),
                    "stretch": {"source": "16-bit Bayer"},
                },
            }

            updated = _restretch_cached_preview_if_needed(metadata, raw_path, png_path, meta_path)

            self.assertIs(updated, metadata)
            self.assertEqual(png_path.read_bytes(), b"old-color-preview")

    def test_current_image_debayers_color_camera_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asiair-color-preview-test-") as tmp:
            root = Path(tmp)
            config_path = root / "devices.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": str(root / "not-mounted"),
                                }
                            ]
                        },
                        "devices": [{"name": "sqa70", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            width = 6
            height = 6
            raw_values = []
            for y in range(height):
                for x in range(width):
                    if y % 2 == 0 and x % 2 == 0:
                        raw_values.append(50000)
                    elif y % 2 == 1 and x % 2 == 1:
                        raw_values.append(7000)
                    else:
                        raw_values.append(21000)
            frame = ImageFrame(
                width=width,
                height=height,
                image_id=70,
                bin_value=1,
                exposure_ms=1000,
                bytes_per_pixel=2,
                packet_bytes=width * height * 2,
                zip_bytes=width * height * 2,
                raw_bytes=width * height * 2,
                raw_data=b"".join(value.to_bytes(2, "big") for value in raw_values),
                endpoint={"label": "primary", "ip": "192.168.8.10", "port": 4800},
            )

            with patch("asiairbridge.image_preview.fetch_current_image", return_value=frame):
                payload = current_image_response(
                    config,
                    "sqa70",
                    force=True,
                    fallback_context={"enabled": False, "camera_is_color": True, "debayer_pattern": "RG"},
                )

            self.assertTrue(payload["ok"])
            self.assertTrue(payload["image"]["is_color"])
            self.assertEqual(payload["image"]["debayer_pattern"], "RGGB")
            self.assertTrue(payload["image"]["bayer_sample_preserved"])
            self.assertEqual(payload["image"]["stretch"]["debayer"]["pattern"], "RGGB")
            pixels = _decode_rgb_png(cached_image_path(config, "sqa70").read_bytes(), width, height)
            self.assertEqual(len(pixels), width * height * 3)

    def test_current_image_keeps_writing_when_color_context_is_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asiair-mono-preview-test-") as tmp:
            root = Path(tmp)
            config_path = root / "devices.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": str(root / "not-mounted"),
                                }
                            ]
                        },
                        "devices": [{"name": "pier-a", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            frame = ImageFrame(
                width=4,
                height=3,
                image_id=7,
                bin_value=1,
                exposure_ms=1000,
                bytes_per_pixel=1,
                packet_bytes=12,
                zip_bytes=12,
                raw_bytes=12,
                raw_data=bytes([20 + index for index in range(12)]),
                endpoint={"label": "primary", "ip": "192.168.8.10", "port": 4800},
            )

            with patch("asiairbridge.image_preview.fetch_current_image", return_value=frame):
                payload = current_image_response(config, "pier-a", force=True, fallback_context={"enabled": False})

            self.assertTrue(payload["ok"])
            self.assertTrue(payload["refreshed"])
            self.assertFalse(payload["image"]["is_color"])
            self.assertNotIn("debayer", payload["image"]["stretch"])
            pixels = _decode_grayscale_png(cached_image_path(config, "pier-a").read_bytes(), 4, 3)
            self.assertEqual(len(pixels), 12)

    def test_current_image_uses_previous_color_metadata_when_rpc_context_is_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asiair-color-context-gap-test-") as tmp:
            root = Path(tmp)
            config_path = root / "devices.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": str(root / "not-mounted"),
                                }
                            ]
                        },
                        "devices": [{"name": "sqa70", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            width = 6
            height = 6
            raw_values = []
            for y in range(height):
                for x in range(width):
                    if y % 2 == 0 and x % 2 == 0:
                        raw_values.append(50000)
                    elif y % 2 == 1 and x % 2 == 1:
                        raw_values.append(7000)
                    else:
                        raw_values.append(21000)
            frame = ImageFrame(
                width=width,
                height=height,
                image_id=70,
                bin_value=1,
                exposure_ms=1000,
                bytes_per_pixel=2,
                packet_bytes=width * height * 2,
                zip_bytes=width * height * 2,
                raw_bytes=width * height * 2,
                raw_data=b"".join(value.to_bytes(2, "big") for value in raw_values),
                endpoint={"label": "primary", "ip": "192.168.8.10", "port": 4800},
            )

            with patch("asiairbridge.image_preview.fetch_current_image", return_value=frame):
                current_image_response(
                    config,
                    "sqa70",
                    force=True,
                    fallback_context={"enabled": False, "camera_is_color": True, "debayer_pattern": "RG"},
                )
                payload = current_image_response(config, "sqa70", force=True, fallback_context={"enabled": False})

            self.assertTrue(payload["ok"])
            self.assertTrue(payload["refreshed"])
            self.assertTrue(payload["image"]["is_color"])
            self.assertEqual(payload["image"]["debayer_pattern"], "RGGB")
            self.assertTrue(payload["image"]["bayer_sample_preserved"])
            self.assertEqual(payload["image"]["stretch"]["debayer"]["pattern"], "RGGB")
            pixels = _decode_rgb_png(cached_image_path(config, "sqa70").read_bytes(), width, height)
            self.assertEqual(len(pixels), width * height * 3)

    def test_current_image_uses_live_preview_even_when_storage_flags_are_passed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asiair-live-preview-test-") as tmp:
            root = Path(tmp)
            config_path = root / "devices.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": str(root / "not-mounted"),
                                }
                            ]
                        },
                        "devices": [{"name": "pier-a", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            frame = ImageFrame(
                width=4,
                height=3,
                image_id=7,
                bin_value=4,
                exposure_ms=1000,
                bytes_per_pixel=1,
                packet_bytes=12,
                zip_bytes=12,
                raw_bytes=12,
                raw_data=bytes(range(12)),
                endpoint={"label": "primary", "ip": "192.168.8.10", "port": 4800},
            )

            with patch("asiairbridge.image_preview.fetch_current_image", return_value=frame) as fetch:
                payload = current_image_response(
                    config,
                    "pier-a",
                    force=True,
                    storage_fallback=True,
                    storage_prefer=True,
                    fallback_context={"enabled": True, "page": "plan", "exposure_seconds": 600.0, "bin": 1},
                )

            fetch.assert_called_once()
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["refreshed"])
            self.assertNotEqual(payload["source"].get("type"), "storage-fallback")
            self.assertEqual(payload["source"]["method"], "get_current_img")
            self.assertEqual(payload["source"]["port"], 4800)
            self.assertEqual(payload["image"]["width"], 4)
            self.assertEqual(payload["image"]["height"], 3)
            self.assertEqual(payload["image"]["exposure_seconds"], 600.0)
            self.assertEqual(payload["image"]["exposure_ms"], 600000)
            self.assertEqual(payload["image"]["exposure_source"], "capture_context")
            self.assertEqual(payload["image"]["bin"], 1)
            self.assertIn("raw_url", payload)
            cached_pixels = _decode_grayscale_png(cached_image_path(config, "pier-a").read_bytes(), 4, 3)
            self.assertEqual(len(cached_pixels), 12)

    def test_stale_storage_fallback_cache_is_refreshed_from_live_preview(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asiair-storage-cache-migration-test-") as tmp:
            root = Path(tmp)
            config_path = root / "devices.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": str(root / "not-mounted"),
                                }
                            ]
                        },
                        "devices": [{"name": "pier-a", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            cache_path = cached_image_path(config, "pier-a")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(_png_grayscale(4, 3, bytes([99] * 12)))
            (cache_path.parent / "current.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "generated_at": "2026-06-16T00:00:00",
                        "preview_generated_at": "2026-06-16T00:00:00",
                        "image": {"width": 4, "height": 3, "png_bytes": 1},
                        "source": {"type": "storage-fallback", "origin": "asiair-smb"},
                    }
                ),
                encoding="utf-8",
            )
            frame = ImageFrame(
                width=4,
                height=3,
                image_id=8,
                bin_value=1,
                exposure_ms=1000,
                bytes_per_pixel=1,
                packet_bytes=12,
                zip_bytes=12,
                raw_bytes=12,
                raw_data=bytes([20 + index for index in range(12)]),
                endpoint={"label": "primary", "ip": "192.168.8.10", "port": 4800},
            )

            with patch("asiairbridge.image_preview.fetch_current_image", return_value=frame) as fetch:
                payload = current_image_response(config, "pier-a", force=False)

            fetch.assert_called_once()
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["refreshed"])
            self.assertEqual(payload["source"]["method"], "get_current_img")
            self.assertNotEqual(payload["source"].get("type"), "storage-fallback")
            cached_pixels = _decode_grayscale_png(cache_path.read_bytes(), 4, 3)
            self.assertNotEqual(cached_pixels, bytes([99] * 12))

    def test_fetch_current_image_falls_back_to_4801(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asiair-port-fallback-test-") as tmp:
            root = Path(tmp)
            config_path = root / "devices.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": str(root / "not-mounted"),
                                }
                            ]
                        },
                        "devices": [{"name": "pier-a", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )
            device = load_config(config_path).default_device()
            raw_data = b"".join(value.to_bytes(2, "big") for value in range(12))
            packet = _image_packet(4, 3, raw_data, exposure_ms=1000, image_id=11, bin_value=2)
            ports: list[int] = []

            def read_packet(ip: str, port: int, payload: bytes) -> bytes:
                self.assertEqual(ip, "192.168.8.10")
                self.assertIn(b"get_current_img", payload)
                ports.append(port)
                if port == 4800:
                    raise TimeoutError("no preview on primary image port")
                return packet

            with (
                patch("asiairbridge.image_preview.rpc_priority_session", return_value=nullcontext()),
                patch("asiairbridge.image_preview._read_image_packet", side_effect=read_packet),
            ):
                frame = fetch_current_image(device)

            self.assertEqual(ports, [4800, 4801])
            self.assertEqual(frame.width, 4)
            self.assertEqual(frame.height, 3)
            self.assertEqual(frame.bytes_per_pixel, 2)
            self.assertEqual(frame.exposure_ms, 1000)
            self.assertEqual(frame.bin_value, 2)
            self.assertEqual(
                frame.endpoint,
                {"label": "primary", "ip": "192.168.8.10", "kind": None, "priority": 0, "enabled": True, "port": 4801},
            )

    def test_live_fetch_uses_capture_context_exposure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asiair-capture-context-test-") as tmp:
            root = Path(tmp)
            config_path = root / "devices.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": str(root / "not-mounted"),
                                }
                            ]
                        },
                        "devices": [{"name": "pier-a", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            frame = ImageFrame(
                width=4,
                height=3,
                image_id=7,
                bin_value=4,
                exposure_ms=1000,
                bytes_per_pixel=1,
                packet_bytes=12,
                zip_bytes=12,
                raw_bytes=12,
                raw_data=bytes(range(12)),
                endpoint={"label": "primary", "ip": "192.168.8.10"},
            )

            with patch("asiairbridge.image_preview.fetch_current_image", return_value=frame):
                payload = current_image_response(
                    config,
                    "pier-a",
                    force=True,
                    fallback_context={"enabled": True, "page": "plan", "exposure_seconds": 600.0, "bin": 1},
                )

            self.assertEqual(payload["source"]["method"], "get_current_img")
            self.assertEqual(payload["image"]["exposure_ms"], 600000)
            self.assertEqual(payload["image"]["exposure_seconds"], 600.0)
            self.assertEqual(payload["image"]["exposure_source"], "capture_context")
            self.assertEqual(payload["image"]["bin"], 1)

    def test_cached_live_image_uses_capture_context_exposure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asiair-cache-context-test-") as tmp:
            root = Path(tmp)
            config_path = root / "devices.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": str(root / "not-mounted"),
                                }
                            ]
                        },
                        "devices": [{"name": "pier-a", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            frame = ImageFrame(
                width=4,
                height=3,
                image_id=8,
                bin_value=4,
                exposure_ms=1000,
                bytes_per_pixel=1,
                packet_bytes=12,
                zip_bytes=12,
                raw_bytes=12,
                raw_data=bytes(range(12)),
                endpoint={"label": "primary", "ip": "192.168.8.10"},
            )

            with patch("asiairbridge.image_preview.fetch_current_image", return_value=frame):
                current_image_response(config, "pier-a", force=True)
            with patch("asiairbridge.image_preview.fetch_current_image") as fetch:
                payload = current_image_response(
                    config,
                    "pier-a",
                    force=False,
                    fallback_context={"enabled": True, "page": "plan", "exposure_seconds": 600.0, "bin": 1},
                )

            fetch.assert_not_called()
            self.assertFalse(payload["refreshed"])
            self.assertEqual(payload["image"]["exposure_ms"], 600000)
            self.assertEqual(payload["image"]["exposure_seconds"], 600.0)
            self.assertEqual(payload["image"]["bin"], 1)


def _image_packet(
    width: int,
    height: int,
    raw_data: bytes,
    exposure_ms: int,
    image_id: int,
    bin_value: int,
) -> bytes:
    header = bytearray(32)
    header[16:18] = width.to_bytes(2, "big")
    header[18:20] = height.to_bytes(2, "big")
    header[24:26] = exposure_ms.to_bytes(2, "big")
    header[28:30] = image_id.to_bytes(2, "big")
    header[30:32] = bin_value.to_bytes(2, "big")
    archive_bytes = BytesIO()
    with ZipFile(archive_bytes, "w") as archive:
        archive.writestr("raw_data", raw_data)
    return bytes(header) + archive_bytes.getvalue()


def _decode_grayscale_png(png: bytes, width: int, height: int) -> bytes:
    self_assert = unittest.TestCase()
    self_assert.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
    offset = 8
    compressed = bytearray()
    while offset < len(png):
        size = struct.unpack(">I", png[offset : offset + 4])[0]
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + size]
        offset += 12 + size
        if kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            break
    rows = zlib.decompress(bytes(compressed))
    out = bytearray()
    stride = width + 1
    for y in range(height):
        row = rows[y * stride : (y + 1) * stride]
        self_assert.assertEqual(row[0], 0)
        out.extend(row[1:])
    self_assert.assertEqual(len(out), width * height)
    return bytes(out)


def _decode_rgb_png(png: bytes, width: int, height: int) -> bytes:
    self_assert = unittest.TestCase()
    self_assert.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
    offset = 8
    compressed = bytearray()
    color_type = None
    while offset < len(png):
        size = struct.unpack(">I", png[offset : offset + 4])[0]
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + size]
        offset += 12 + size
        if kind == b"IHDR":
            parsed_width, parsed_height, bit_depth, color_type, *_ = struct.unpack(">IIBBBBB", data[:13])
            self_assert.assertEqual(parsed_width, width)
            self_assert.assertEqual(parsed_height, height)
            self_assert.assertEqual(bit_depth, 8)
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            break
    self_assert.assertEqual(color_type, 2)
    rows = zlib.decompress(bytes(compressed))
    out = bytearray()
    stride = (width * 3) + 1
    for y in range(height):
        row = rows[y * stride : (y + 1) * stride]
        self_assert.assertEqual(row[0], 0)
        out.extend(row[1:])
    self_assert.assertEqual(len(out), width * height * 3)
    return bytes(out)


if __name__ == "__main__":
    unittest.main()
