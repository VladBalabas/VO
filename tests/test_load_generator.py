from __future__ import annotations

import argparse
import multiprocessing
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import load_generator as generator


class TestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = 0
    lock = threading.Lock()

    def do_GET(self):
        with self.lock:
            TestHandler.seen += 1
            index = TestHandler.seen
        time.sleep(0.08 if self.path == "/slow" else 0.015)
        if self.path == "/mixed" and index % 3 == 0:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        body = b'{"ok":true}'
        self.send_response(503 if self.path == "/mixed" and index % 3 == 1 else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Pod", "test-pod")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class LoadGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def arguments(self, **overrides):
        values = dict(
            base_url=self.base_url,
            path="/ok",
            seed_file=None,
            processes=2,
            threads_per_process=0,
            rate=40,
            duration=0.8,
            timeout=1,
            sample_interval_ms=2,
            concurrency=3,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_open_success_and_independent_little_sampling(self):
        data = generator.run_open(self.arguments())
        self.assertEqual(data["requests_scheduled"], 32)
        self.assertEqual(data["responses_2xx"], 32)
        self.assertEqual(data["achieved_rate_rps"], data["successful_rate_rps"])
        self.assertGreater(data["in_flight_sample_count"], 20)
        self.assertEqual(data["in_flight_samples"][-1]["in_flight"], 0)
        predicted = data["successful_rate_rps"] * data["successful_latency_mean_ms"] / 1000
        self.assertLess(abs(predicted - data["average_successful_in_flight"]) / predicted, 0.5)
        self.assertGreaterEqual(data["latency_mean_ms"], data["network_latency_mean_ms"])

    def test_generator_queue_is_in_latency_and_drains(self):
        data = generator.run_open(
            self.arguments(path="/slow", processes=1, threads_per_process=1, rate=40, duration=0.5)
        )
        self.assertEqual(data["requests_scheduled"], 20)
        self.assertTrue(data["worker_capacity_saturated"])
        self.assertGreater(data["peak_generator_queued"], 10)
        self.assertGreater(data["drain_seconds"], 1)
        self.assertGreater(data["latency_p95_ms"], data["network_latency_p95_ms"] * 3)
        self.assertGreater(data["average_in_flight"], 1)
        self.assertEqual(data["in_flight_samples"][-1]["in_flight"], 0)

    def test_http_and_transport_failures_are_not_throughput(self):
        data = generator.run_open(
            self.arguments(path="/mixed", processes=1, rate=30, duration=0.5)
        )
        self.assertGreater(data["responses_non_2xx"], 0)
        self.assertGreater(data["request_exceptions"], 0)
        self.assertEqual(
            data["requests_completed"],
            data["responses_2xx"] + data["responses_non_2xx"] + data["request_exceptions"],
        )
        self.assertLess(data["achieved_rate_rps"], data["attempted_completion_rate_rps"])
        self.assertEqual(data["responses_2xx"], sum(data["pod_counts"].values()))

    def test_closed_sampler_drains(self):
        data = generator.run_closed(self.arguments(duration=0.25))
        self.assertGreater(data["requests_completed"], 3)
        self.assertEqual(data["requests_failed"], 0)
        self.assertEqual(data["in_flight_samples"][-1]["in_flight"], 0)
        self.assertLessEqual(data["peak_in_flight"], 3)

    def test_closed_multiprocess_preserves_fixed_total_concurrency(self):
        data = generator.run_closed(
            self.arguments(path="/slow", concurrency=5, processes=2, duration=0.3)
        )
        self.assertEqual(data["processes"], 2)
        self.assertEqual(data["concurrency_by_process"], [3, 2])
        self.assertEqual(sum(data["concurrency_by_process"]), data["concurrency"])
        self.assertEqual(data["worker_capacity"], 5)
        self.assertEqual(len(data["worker_peak_active_by_process"]), 2)
        self.assertTrue(all(peak > 0 for peak in data["worker_peak_active_by_process"]))
        self.assertTrue(
            all(
                peak <= limit
                for peak, limit in zip(
                    data["worker_peak_active_by_process"], data["concurrency_by_process"]
                )
            )
        )
        self.assertLessEqual(data["peak_in_flight"], 5)
        self.assertEqual(data["requests_failed"], 0)
        self.assertEqual(data["in_flight_samples"][-1]["in_flight"], 0)
        self.assertEqual(
            sorted(sample["request_index"] for sample in data["samples"]),
            list(range(data["requests_completed"])),
        )

    def test_observation_excludes_slow_result_collection(self):
        records = [
            {
                "request_index": 0,
                "scheduled_at": 100.1,
                "sent_at": 100.2,
                "completed_at": 100.4,
                "latency_ms": 300,
                "network_latency_ms": 200,
                "schedule_lag_ms": 100,
                "status": 200,
                "pod": "test-pod",
                "error": None,
            }
        ]
        samples = [
            dict(sampled_at=100.0, scheduled=0, dispatched=0, completed=0, completed_successful=0),
            dict(sampled_at=100.2, scheduled=1, dispatched=1, completed=0, completed_successful=0),
            dict(sampled_at=100.5, scheduled=1, dispatched=1, completed=1, completed_successful=1),
            dict(sampled_at=104.0, scheduled=1, dispatched=1, completed=1, completed_successful=1),
        ]
        data = generator.summarize_records(
            records, "closed", 100.0, 1.0, None, {"sample_interval_ms": 1}, samples
        )
        self.assertEqual(data["observation_seconds"], 1.0)
        self.assertEqual(data["successful_rate_rps"], 1.0)
        self.assertEqual(data["in_flight_samples"][-1]["offset_ms"], 1000.0)
        self.assertEqual(data["average_successful_in_flight"], 0.25)
        self.assertNotEqual(data["average_successful_in_flight"], 0.3)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()
