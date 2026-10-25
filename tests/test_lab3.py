import tempfile
import unittest
from collections import deque
from pathlib import Path

from scripts.analyze_lab3 import hpa_events
from scripts.lab3_generator import classification, parser, step_summary


class Lab3GeneratorTests(unittest.TestCase):
    def test_required_connection_defaults(self):
        args = parser().parse_args(["--output", "unused"])
        self.assertEqual(args.connections, 40)
        self.assertEqual(args.requests_per_connection, 100)
        self.assertEqual(args.step_duration, 30)

    def test_timeout_is_non_2xx(self):
        self.assertEqual(classification({"status": None, "error_type": "TimeoutError"}),
                         "non_2xx")
        self.assertEqual(classification({"status": None, "error_type": "ClientError"}),
                         "connection_error")

    def test_step_uses_last_twenty_seconds_by_completion_time(self):
        rows = deque()
        for second in range(30):
            rows.append({
                "status": 200,
                "error_type": None,
                "pod": "web-a",
                "completed_offset_ms": second * 1000 + 500,
                "sent_offset_ms": second * 1000 + 400,
                "latency_ms": 100,
                "schedule_lag_ms": 10,
            })
        rows.append({
            "status": None,
            "error_type": "TimeoutError",
            "pod": None,
            "completed_offset_ms": 29_900,
            "sent_offset_ms": 25_000,
            "latency_ms": 5_000,
            "schedule_lag_ms": 20,
        })
        result = step_summary(rows, index=0, rate=2, start_ms=0,
                              duration_ms=30_000, start_epoch=100)
        self.assertEqual(result["counts_last_20s"], [1] * 20)
        self.assertEqual(result["x"], 1)
        self.assertEqual(result["non_2xx"], 1)
        self.assertEqual(result["pod_success_counts"], {"web-a": 20})
        self.assertEqual(result["measurement_start_offset_ms"], 10_000)

    def test_scale_down_is_first_real_decrease_after_maximum(self):
        ready = [1] * 65 + [2] * 15 + [3] * 30 + [5] * 55 + [3] * 15
        timeline = {
            "desired": [1] * 60 + [2] * 15 + [3] * 30 + [5] * 60 + [3] * 15,
            "ready": ready,
            "p99_ms": [100.0] * 180,
        }
        result = hpa_events("test", {"timeline": timeline}, 100.0)
        self.assertIsNone(result["maximum_ready_after_jump_seconds"])
        self.assertEqual(result["maximum_ready_observed_at_profile_second"], 110)
        self.assertEqual(result["first_ready_decrease_after_jump_end_seconds"], 75)


if __name__ == "__main__":
    unittest.main()
