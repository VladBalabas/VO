import json
import tempfile
import unittest
from pathlib import Path

from scripts.analyze_results import (
    analyze,
    little_calculation,
    scaling_calculations,
    summarize_open_runs,
)


class AnalyzeResultsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.results = Path(self.temporary.name)
        (self.results / "raw").mkdir()

    def write_run(self, name, **values):
        run = {
            "requests_scheduled": 100,
            "responses_2xx": 80,
            "observation_seconds": 10,
            "achieved_rate_rps": 10,
            "successful_rate_rps": 8,
            "latency_mean_ms": 100,
            "latency_p50_ms": 80,
            "latency_p95_ms": 150,
            "latency_p99_ms": 200,
            "error_rate": 0.2,
            "average_in_flight": 1,
            "schedule_lag_p95_ms": 0,
        }
        run.update(values)
        (self.results / "raw" / name).write_text(json.dumps(run), encoding="utf-8")

    def test_table_and_scaling_use_successful_responses(self):
        for repeat, success in enumerate((6, 8, 9), start=1):
            self.write_run(f"open-r1-rate500-rep{repeat}.json", successful_rate_rps=success)
            self.write_run(f"open-r2-rate500-rep{repeat}.json", successful_rate_rps=success * 1.8)
        rows = summarize_open_runs(self.results)
        self.assertEqual(rows[0]["achieved_rate_rps"], 8)
        self.assertEqual(rows[0]["attempted_completion_rate_rps"], 10)
        self.assertEqual(scaling_calculations(rows)[0]["speedup"], 1.8)
        self.assertAlmostEqual(scaling_calculations(rows)[0]["karp_flatt"], 0.111111)

    def test_legacy_little_is_not_an_independent_verification(self):
        self.write_run("open-r1-rate100-rep1.json")
        little = little_calculation(self.results)
        self.assertFalse(little["independent_verification"])
        self.assertIsNone(little["measured_average_in_flight"])
        self.assertIsNone(little["discrepancy_percent"])

    def test_little_compares_same_run_and_successful_cohort(self):
        for repeat, throughput, latency, measured in ((1, 5, 100, 0.4), (2, 8, 300, 2), (3, 2, 200, 0.5)):
            self.write_run(
                f"open-r1-rate100-rep{repeat}.json",
                schema_version=2,
                latency_origin="scheduled_at",
                in_flight_measurement="independent_periodic_counter_samples",
                successful_rate_rps=throughput,
                successful_latency_mean_ms=latency,
                average_successful_in_flight=measured,
            )
        little = little_calculation(self.results)
        self.assertTrue(little["independent_verification"])
        self.assertEqual(little["source_file"], "open-r1-rate100-rep3.json")
        self.assertEqual(little["lambda_times_w"], 0.4)
        self.assertEqual(little["measured_average_in_flight"], 0.5)
        self.assertEqual(little["discrepancy_percent"], 20)

    def test_mixed_latency_methods_are_rejected(self):
        self.write_run("open-r1-rate100-rep1.json")
        self.write_run("open-r1-rate100-rep2.json", schema_version=2, latency_origin="scheduled_at")
        with self.assertRaises(ValueError):
            summarize_open_runs(self.results)

    def test_closed_comparison_and_noop_use_successful_throughput(self):
        for instances in (1, 2):
            self.write_run(f"open-r{instances}-rate100-rep1.json")
        self.write_run("closed-loop.json", concurrency=1, successful_rate_rps=9)
        self.write_run("generator-noop.json", successful_rate_rps=9.5, samples=[{}])
        summary = analyze(self.results)
        self.assertEqual(summary["closed_comparison"]["closed_achieved_rate_rps"], 9)
        self.assertEqual(summary["closed_comparison"]["closed_minus_open_successful_rate_rps"], 1)
        self.assertEqual(summary["generator_noop"]["achieved_rate_rps"], 9.5)
        self.assertNotIn("samples", summary["generator_noop"])


if __name__ == "__main__":
    unittest.main()
