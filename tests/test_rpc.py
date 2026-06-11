from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from asiairbridge.config import Device, DeviceEndpoint
from asiairbridge.rpc import asiair_device_rpc, asiair_rpc


class FakeSocket:
    def __init__(self, chunks: list[bytes] | None = None, exc: Exception | None = None) -> None:
        self.chunks = chunks or []
        self.exc = exc
        self.timeouts: list[float] = []
        self.sent: list[bytes] = []

    def __enter__(self) -> "FakeSocket":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv(self, _size: int) -> bytes:
        if self.exc is not None:
            raise self.exc
        if self.chunks:
            return self.chunks.pop(0)
        return b""


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

    def test_rpc_caps_unmatched_response_bytes(self) -> None:
        fake = FakeSocket([b"xxxxxxxxxxx"])
        with (
            patch("asiairbridge.rpc.socket.create_connection", return_value=fake),
            patch("asiairbridge.rpc.MAX_RPC_RESPONSE_BYTES", 10),
        ):
            with self.assertRaises(ValueError):
                asiair_rpc("192.168.8.10", "test_connection", timeout_seconds=1.0)

    def test_rpc_socket_timeout_becomes_timeout_error(self) -> None:
        fake = FakeSocket(exc=socket.timeout())
        with patch("asiairbridge.rpc.socket.create_connection", return_value=fake):
            with self.assertRaises(TimeoutError):
                asiair_rpc("192.168.8.10", "test_connection", timeout_seconds=1.0)

        self.assertTrue(fake.timeouts)


if __name__ == "__main__":
    unittest.main()
