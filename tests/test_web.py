from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from asiairbridge.config import load_config
from asiairbridge.web import _current_image_capture_context


class WebCurrentImageContextTests(unittest.TestCase):
    def test_current_image_context_uses_configured_camera_metadata_when_rpc_lacks_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="asiair-web-context-test-") as tmp:
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
                        "devices": [
                            {
                                "name": "sqa70",
                                "ip": "192.168.8.10",
                                "camera": {"is_color": True, "debayer_pattern": "RG"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            server = SimpleNamespace(config=load_config(config_path))

            with patch(
                "asiairbridge.web.rpc_monitor_response",
                return_value={"ok": True, "highlights": {}, "categories": []},
            ):
                context = _current_image_capture_context(server, "sqa70")

            self.assertFalse(context["enabled"])
            self.assertTrue(context["camera_is_color"])
            self.assertEqual(context["debayer_pattern"], "RG")


if __name__ == "__main__":
    unittest.main()
