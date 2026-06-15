from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from asiairbridge.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_platform_specific_paths_are_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "devices.json"
            config_path.parent.mkdir()
            config_path.write_text(
                json.dumps(
                    {
                        "project": {
                            "destination_root": {
                                "darwin": "state/backups",
                                "default": "state/backups",
                            },
                            "private_path_prefixes": [{"darwin": "state/backups"}],
                        },
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": "/Volumes/{name}/EMMC Images",
                                    "path_templates": {
                                        "darwin": "/Volumes/{name}/EMMC Images",
                                        "linux": "/mnt/{name}/EMMC Images",
                                        "default": "/Volumes/{name}/EMMC Images",
                                    },
                                }
                            ]
                        },
                        "devices": [
                            {
                                "name": "pier-a",
                                "ip": "192.168.8.10",
                                "endpoints": [
                                    {"label": "wired", "ip": "192.168.8.10", "priority": 0},
                                    {"label": "wifi-bridge", "ip": "192.168.8.20", "priority": 10},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("asiairbridge.config._platform_keys", return_value=("darwin", "posix", "default")):
                config = load_config(config_path)

            device = config.default_device()
            source = config.source_roots_for(device)[0]
            self.assertEqual(config.project.destination_root, (root / "state" / "backups").resolve())
            self.assertEqual(str(source.render(device)), "/Volumes/pier-a/EMMC Images")
            self.assertEqual(config.display_path(root / "state" / "backups" / "pier-a"), ".../pier-a")
            self.assertEqual(device.endpoint_ips(), ("192.168.8.10", "192.168.8.20"))
            self.assertEqual(config.backup.retry_count, 2)
            self.assertEqual(config.backup.retry_wait_seconds, 5)
            self.assertEqual(config.backup.job_timeout_hours, 6)

    def test_root_level_config_uses_its_own_directory_as_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
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
                                    "path_template": "/Volumes/{name}/EMMC Images",
                                }
                            ]
                        },
                        "devices": [{"name": "pier-a", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.root, root.resolve())
            self.assertEqual(config.project.destination_root, (root / "state" / "backups").resolve())

    def test_invalid_smb_port_reports_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "devices.json"
            config_path.parent.mkdir()
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "smb_port": "bad",
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": "/Volumes/{name}/EMMC Images",
                                }
                            ],
                        },
                        "devices": [{"name": "pier-a", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_invalid_path_template_reports_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "devices.json"
            config_path.parent.mkdir()
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": "/Volumes/{unknown}/EMMC Images",
                                }
                            ]
                        },
                        "devices": [{"name": "pier-a", "ip": "192.168.8.10"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_invalid_endpoint_priority_reports_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config" / "devices.json"
            config_path.parent.mkdir()
            config_path.write_text(
                json.dumps(
                    {
                        "project": {"destination_root": "state/backups"},
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "EMMC Images",
                                    "path_template": "/Volumes/{name}/EMMC Images",
                                }
                            ]
                        },
                        "devices": [
                            {
                                "name": "pier-a",
                                "ip": "192.168.8.10",
                                "endpoints": [{"ip": "192.168.8.10", "priority": "bad"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
