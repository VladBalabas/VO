import gzip
import tempfile
import unittest
from collections import deque
from pathlib import Path

from scripts.analyze_lab3 import hpa_events, raw_source as analyzer_raw_source
from scripts.lab3_generator import classification, parser, step_summary
from scripts.verify_lab3_results import raw_source as verifier_raw_source


class Lab3GeneratorTests(unittest.TestCase):
    def test_analyzer_and_verifier_accept_gzip_raw_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            archive = raw_dir / "sample.jsonl.gz"
            with gzip.open(archive, "wt", encoding="utf-8") as stream:
                stream.write('{"status": 200}\n')
            self.assertEqual(analyzer_raw_source("sample", raw_dir), archive)
            self.assertEqual(verifier_raw_source("sample", raw_dir), archive)

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

    def test_saved_kubernetes_usl_normal_equation_terms(self):
        values = {1: 326.65, 2: 637.30, 3: 818.25, 5: 1244.80}
        rows = []
        for n in (2, 3, 5):
            c = values[n] / values[1]
            rows.append((n / c - 1, n - 1, n * (n - 1)))
        self.assertEqual(sum(u * u for _, u, _ in rows), 21)
        self.assertEqual(sum(v * v for _, _, v in rows), 440)
        self.assertEqual(sum(u * v for _, u, v in rows), 94)
        self.assertAlmostEqual(sum(y * u for y, u, _ in rows), 1.6685722939187249)
        self.assertAlmostEqual(sum(y * v for y, _, v in rows), 7.477076261804635)


if __name__ == "__main__":
    unittest.main()
