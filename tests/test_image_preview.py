from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from asiairbridge.image_preview import _restretch_cached_preview_if_needed, raw16_to_png


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


if __name__ == "__main__":
    unittest.main()
