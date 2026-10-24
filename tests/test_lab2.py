import ast
import math
import unittest
from pathlib import Path

from scripts.analyze_lab2 import fit_usl, server_bucket, validate_continuous_profile
from scripts.lab2_async_generator import classify, step_summary


class Lab2AnalysisTests(unittest.TestCase):
    def test_histogram_uses_exact_assignment_buckets(self):
        source = Path("backend/ui/api/factory.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        histogram = next(
            node.value for node in module.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "HTTP_DURATION"
                    for target in node.targets)
        )
        buckets = next(keyword.value for keyword in histogram.keywords
                       if keyword.arg == "buckets")
        self.assertEqual(
            ast.literal_eval(buckets),
            (.005, .010, .025, .050, .100, .250, .500, 1.0),
        )

    def test_recover_usl_coefficients(self):
        sigma, kappa, x1 = .15, .03, 100.0
        points = [(n, x1*n/(1+sigma*(n-1)+kappa*n*(n-1)), x1)
                  for n in (1, 2, 3, 4)]
        fit = fit_usl(points)
        self.assertAlmostEqual(fit["sigma"], sigma)
        self.assertAlmostEqual(fit["kappa"], kappa)
        self.assertAlmostEqual(fit["N_star"], math.sqrt((1-sigma)/kappa))

    def test_negative_parameter_is_fixed_and_other_refit(self):
        x1 = 100.0
        points = [(n, x1*n/(1+.08*n*(n-1)), x1) for n in (1, 2, 3, 4)]
        fit = fit_usl(points)
        self.assertGreaterEqual(fit["sigma"], 0)
        self.assertAlmostEqual(fit["kappa"], .08)

    def test_histogram_bucket_error_bound(self):
        data = {"server_buckets":{"result":[
            {"metric":{"le":"0.1"},"values":[[10,"90"]]},
            {"metric":{"le":"0.25"},"values":[[10,"100"]]},
            {"metric":{"le":"+Inf"},"values":[[10,"100"]]},
        ]}}
        result = server_bucket(data, 9, 11)
        self.assertEqual(result["label"], "(0.1, 0.25] s")
        self.assertAlmostEqual(result["max_error_ms"], 150)

    def test_above_last_finite_bucket_is_explicit(self):
        data = {"server_buckets":{"result":[
            {"metric":{"le":"1.0"},"values":[[10,"90"]]},
            {"metric":{"le":"+Inf"},"values":[[10,"100"]]},
        ]}}
        self.assertEqual(server_bucket(data, 9, 11)["label"], ">1 s")

    def test_timeout_is_counted_as_non_2xx(self):
        self.assertEqual(classify({"status": None, "error_type": "TimeoutError"}), "non_2xx")
        self.assertEqual(classify({"status": None, "error_type": "ClientError"}), "connection_error")

    def test_step_counts_completion_from_previous_step(self):
        records = [{"step_index": -1, "status": 200, "error_type": None,
                    "completed_offset_ms": 15500 + 1000*i, "latency_ms": 5}
                   for i in range(15)]
        records.append({"step_index": 0, "status": None, "error_type": "TimeoutError",
                        "completed_offset_ms": 29500, "latency_ms": 5000})
        summary = step_summary(records, [], rate=200, index=0,
                               start_ms=10000, duration_ms=20000, start_epoch=100)
        self.assertEqual(summary["x"], 1)
        self.assertEqual(summary["non_2xx"], 1)
        self.assertEqual(summary["timeouts"], 1)
        self.assertEqual(summary["scheduled"], 4000)

    def test_continuous_profile_rejects_step_gap(self):
        raw = {"mode": "profile", "nominal_duration_seconds": 30,
               "steps": [{"start_offset_ms": 11000, "step_index": 0, "rate": 200}],
               "requests_scheduled": 6000, "requests_sent": 6000,
               "samples": [None] * 6000}
        with self.assertRaisesRegex(AssertionError, "gap"):
            validate_continuous_profile(raw)


if __name__ == "__main__":
    unittest.main()
