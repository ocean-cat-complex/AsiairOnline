from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from asiairbridge.state import RunLock, read_latest_state, write_run_state


class StateTests(unittest.TestCase):
    def test_read_latest_state_tolerates_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "latest.json").write_text("{", encoding="utf-8")

            self.assertIsNone(read_latest_state(state_dir))

    def test_run_lock_reclaims_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "backup.lock"
            lock_path.write_text(json.dumps({"pid": 123456}), encoding="utf-8")

            with patch("asiairbridge.state._pid_is_running", return_value=False):
                with RunLock(lock_path):
                    self.assertTrue(lock_path.exists())

            self.assertFalse(lock_path.exists())

    def test_force_lock_refuses_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "backup.lock"
            lock_path.write_text(json.dumps({"pid": 123456}), encoding="utf-8")

            with patch("asiairbridge.state._pid_is_running", return_value=True):
                with self.assertRaises(RuntimeError):
                    with RunLock(lock_path, force=True):
                        pass

            self.assertTrue(lock_path.exists())

    def test_write_run_state_uses_latest_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            run_path = write_run_state(state_dir, "run-1", {"ok": True})

            self.assertTrue(run_path.exists())
            self.assertEqual(read_latest_state(state_dir), {"ok": True})
            self.assertFalse((state_dir / "latest.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
