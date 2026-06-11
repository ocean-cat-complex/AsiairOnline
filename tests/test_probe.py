from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from asiairbridge.probe import net_view, ping_host


class ProbeTests(unittest.TestCase):
    def test_ping_uses_posix_arguments_on_macos(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ping"],
            returncode=0,
            stdout="64 bytes from 192.168.8.10: icmp_seq=0 ttl=64 time=12.3 ms\n",
            stderr="",
        )
        with (
            patch("asiairbridge.probe.sys.platform", "darwin"),
            patch("asiairbridge.probe.subprocess.run", return_value=completed) as run,
        ):
            result = ping_host("192.168.8.10", timeout_ms=750)

        self.assertTrue(result.ok)
        self.assertEqual(run.call_args.args[0], ["ping", "-c", "1", "-W", "750", "192.168.8.10"])

    def test_net_view_uses_smbutil_on_posix(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["smbutil"],
            returncode=0,
            stdout="Share        Type\nEMMC Images  Disk\n",
            stderr="",
        )
        with (
            patch("asiairbridge.probe.shutil.which", return_value="/usr/bin/smbutil"),
            patch("asiairbridge.probe.subprocess.run", return_value=completed) as run,
        ):
            result = net_view("192.168.8.10")

        self.assertTrue(result.ok)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/smbutil", "view", "-N", "//192.168.8.10"])


if __name__ == "__main__":
    unittest.main()
