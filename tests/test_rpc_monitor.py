from __future__ import annotations

import unittest

from asiairbridge.rpc_monitor import _highlights


class RpcMonitorHighlightTests(unittest.TestCase):
    def test_highlights_prefer_plan_sequence_exposure_for_active_target(self) -> None:
        items = [
            {
                "method": "get_app_state",
                "params": None,
                "result": {
                    "page": "plan",
                    "capture": {
                        "state": "expose",
                        "is_working": True,
                        "progress": {
                            "cur_target": {"target_name": "NGC 6960", "cur": 0, "total": 20},
                            "cur_seq": {"frame_type": "light", "cur": 0, "total": 20},
                        },
                    },
                },
            },
            {
                "method": "get_camera_exp_and_bin",
                "params": None,
                "result": {"exp_ms": 5000, "bin": 2},
            },
            {
                "method": "get_plan",
                "params": None,
                "result": [
                    {
                        "plan_name": "春季星系",
                        "is_plan_started": True,
                        "targets": [
                            {
                                "target_name": "NGC 6960",
                                "enable": True,
                                "seqs": [
                                    {
                                        "type": "light",
                                        "exp": 600.0,
                                        "bin": 1,
                                        "enable": True,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        ]

        highlights = _highlights(items)

        self.assertEqual(highlights["exposure_seconds"], 600.0)
        self.assertEqual(highlights["exposure_source"], "plan_sequence")
        self.assertEqual(highlights["bin"], 1)


if __name__ == "__main__":
    unittest.main()
