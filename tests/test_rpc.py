from __future__ import annotations

import unittest
from unittest.mock import patch

from asiairbridge.config import Device, DeviceEndpoint
from asiairbridge.rpc import asiair_device_rpc


class RpcEndpointTests(unittest.TestCase):
    def test_device_rpc_falls_back_to_second_endpoint(self) -> None:
        device = Device(
            name="pier-a",
            ip="192.168.8.10",
            endpoints=(
                DeviceEndpoint("wired", "192.168.8.10", priority=0),
                DeviceEndpoint("wifi-bridge", "192.168.8.20", priority=10),
            ),
        )
        calls: list[str] = []

        def fake_rpc(ip: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(ip)
            if ip == "192.168.8.10":
                raise TimeoutError("offline")
            return {"id": kwargs.get("request_id", 1), "method": args[0], "code": 0, "result": "ok"}

        with patch("asiairbridge.rpc.asiair_rpc", side_effect=fake_rpc):
            response = asiair_device_rpc(device, "test_connection", request_id=7)

        self.assertEqual(calls, ["192.168.8.10", "192.168.8.20"])
        self.assertEqual(response["code"], 0)
        self.assertEqual(response["_endpoint"]["label"], "wifi-bridge")


if __name__ == "__main__":
    unittest.main()
