from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from asiairbridge.camera_ops import camera_status_response
from asiairbridge.config import AppConfig, BackupSettings, Device, ProjectSettings, SourceRoot
from asiairbridge.rpc import GUIDER_PORT, IMAGER_PORT


class CameraStatusMountTests(unittest.TestCase):
    def test_camera_status_reads_mount_on_guider_port(self) -> None:
        config = _test_config()
        calls: list[tuple[str, int]] = []

        def fake_rpc(device, method, *args, **kwargs):  # type: ignore[no-untyped-def]
            port = int(kwargs.get("port", IMAGER_PORT))
            calls.append((method, port))
            result = {
                "get_app_state": {"page": "preview", "capture": {"state": "idle", "is_working": False}},
                "get_camera_state": {"name": "ASI6200MM", "state": "idle", "path": "/"},
                "get_camera_info": {"chip_size": [9576, 6388], "bins": [1, 2, 4]},
                "get_camera_exp_and_bin": {"exposure": 1_000_000, "bin": 1},
                "get_controls": [],
                "get_camera_16bit": True,
                "get_subframe": {"width": 0, "height": 0, "x": 0, "y": 0},
                "can_liveview": True,
                "can_abort_expose": False,
                "get_control_value": {"name": (kwargs.get("params") or [""])[0], "value": 0},
                "scope_get_ra_dec": [11.955556, 89.0, 21.897556],
                "scope_get_track_state": False,
                "scope_get_track_mode": {"list": ["Sidereal", "Solar", "Lunar"], "index": 0},
                "scope_get_slew_rate": {"list": ["1X", "MAX"], "index": 1},
                "scope_is_moving": "none",
                "scope_get_pierside": "pier_east",
                "scope_get_location": [40.0043, 116.32],
            }[method]
            return {"code": 0, "result": result, "_endpoint": {"label": "primary", "ip": device.ip}}

        with (
            patch("asiairbridge.camera_ops.asiair_device_rpc", side_effect=fake_rpc),
            patch("asiairbridge.camera_ops.current_image_response", return_value={"ok": False}),
            patch("asiairbridge.camera_ops.control_state", return_value={"role": "monitor"}),
        ):
            payload = camera_status_response(
                config,
                "pier-a",
                rpc_timeout_seconds=0.2,
                queue_timeout_seconds=0.2,
                status_budget_seconds=8.0,
            )

        mount = payload["mount"]
        self.assertTrue(mount["available"])
        self.assertEqual(mount["port"], GUIDER_PORT)
        self.assertEqual(mount["ra_text"], "11h 57m 20.0s")
        self.assertEqual(mount["dec_text"], "+89° 00' 00.0\"")
        self.assertEqual(mount["track_enabled"], False)
        self.assertEqual(mount["track_mode"]["value"], "Sidereal")
        self.assertEqual(mount["slew_rate"]["value"], "MAX")
        for method, port in calls:
            if method.startswith("scope_"):
                self.assertEqual(port, GUIDER_PORT)


def _test_config() -> AppConfig:
    root = Path(tempfile.mkdtemp(prefix="asiairbridge-test-"))
    source_root = SourceRoot(label="Images", path_template=str(root / "{name}"))
    return AppConfig(
        path=root / "devices.json",
        root=root,
        project=ProjectSettings(
            timezone="Asia/Shanghai",
            destination_root=root / "dest",
            logs_dir=root / "logs",
            state_dir=root / "state",
            lock_file=root / "state" / "backup.lock",
            default_device="pier-a",
        ),
        backup=BackupSettings(
            dry_run_default=True,
            copy_empty_dirs=True,
            retry_count=2,
            retry_wait_seconds=5,
            job_timeout_hours=6,
            smb_port=445,
            exclude_dirs=(),
            exclude_files=(),
            source_roots=(source_root,),
        ),
        devices=(Device(name="pier-a", ip="127.0.0.1"),),
    )


if __name__ == "__main__":
    unittest.main()
