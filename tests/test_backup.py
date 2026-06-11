from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from asiairbridge.backup import build_jobs, run_job
from asiairbridge.config import load_config
from asiairbridge.probe import ProbeResult


class BackupTests(unittest.TestCase):
    def test_python_backend_dry_run_does_not_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "image.fit").write_text("fits", encoding="utf-8")
            config_path = root / "config" / "devices.json"
            config_path.parent.mkdir()
            config_path.write_text(
                json.dumps(
                    {
                        "project": {
                            "destination_root": "backups",
                            "logs_dir": "logs",
                            "state_dir": "state",
                            "lock_file": "state/backup.lock",
                        },
                        "backup": {
                            "source_roots": [
                                {
                                    "label": "Mounted",
                                    "path_template": str(source),
                                }
                            ]
                        },
                        "devices": [{"name": "pier-a", "ip": "127.0.0.1"}],
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            job = build_jobs(config, "test")[0]

            with (
                patch("asiairbridge.backup.tcp_open", return_value=ProbeResult(True, "tcp/445 open")),
                patch("asiairbridge.backup.shutil.which", return_value=None),
            ):
                result = run_job(config, job, dry_run=True)

            self.assertTrue(result.ok)
            self.assertEqual(result.copy_backend, "python")
            self.assertFalse((config.project.destination_root / "pier-a" / "Mounted" / "image.fit").exists())
            self.assertIn("would copy 1 file", result.detail)


if __name__ == "__main__":
    unittest.main()
