import math
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from backend.ui.api.schemas import TaskCreate
from scripts.analyze_lab4 import usl
from worker.processing import process_slice, quantile_level
from worker.settings import load_settings


ROOT = Path(__file__).resolve().parents[1]


class Lab4Tests(unittest.TestCase):
    def test_quantile_uses_first_level_reaching_ceiling_rank(self):
        histogram = [1, 2, 1, 0]
        self.assertEqual(quantile_level(histogram, 0.25, 4), 0)
        self.assertEqual(quantile_level(histogram, 0.50, 4), 1)
        self.assertEqual(quantile_level(histogram, 0.95, 4), 2)

    def test_slice_is_deterministic_and_accounts_for_every_pixel(self):
        first = process_slice(12345, 16, 0.05, 0.95)
        second = process_slice(12345, 16, 0.05, 0.95)
        self.assertEqual(first, second)
        self.assertEqual(first["removed_pixels"] + first["kept_pixels"], 256)
        self.assertLess(first["p_lo"], first["p_hi"])
        self.assertTrue(math.isfinite(first["mean"]))
        self.assertTrue(math.isfinite(first["stddev"]))

    def test_seed_zero_is_the_documented_poison_task(self):
        with self.assertRaisesRegex(ValueError, "seed=0"):
            process_slice(0, 256, 0.05, 0.95)

    def test_task_payload_boundaries_are_validated_before_database(self):
        self.assertEqual(TaskCreate(priority=1, seed=0).priority, 1)
        self.assertEqual(TaskCreate(priority=3, seed=2**31 - 1).seed, 2**31 - 1)
        for payload in ({"priority": 0}, {"priority": 4}, {"seed": -1}, {"seed": 2**31}):
            with self.assertRaises(ValidationError):
                TaskCreate(**payload)

    def test_variant_two_worker_defaults(self):
        environment = {
            "POSTGRES_HOST": "db", "POSTGRES_DB": "db", "POSTGRES_USER": "u",
            "POSTGRES_PASSWORD": "p", "WEB_URL": "http://web", "WORKER_ID": "w",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = load_settings()
        self.assertEqual(settings.lease_seconds, 5)
        self.assertEqual(settings.poll_seconds, 0.1)
        self.assertEqual(settings.q_lo, 0.05)
        self.assertEqual(settings.q_hi, 0.95)
        self.assertEqual(settings.claim_batch_size, 1)

    def test_usl_recovers_known_coefficients(self):
        sigma, kappa, x1 = 0.08, 0.01, 20.0
        rows = []
        for workers in (1, 2, 5):
            x = workers * x1 / (1 + sigma * (workers - 1) + kappa * workers * (workers - 1))
            rows.append({"workers": workers, "x": x})
        result = usl(rows)
        self.assertAlmostEqual(result["sigma"], sigma)
        self.assertAlmostEqual(result["kappa"], kappa)
        self.assertEqual(result["projection"], "none")

    def test_final_schema_and_worker_manifest_keep_required_guards(self):
        schema = (ROOT / "db" / "init.sql").read_text(encoding="utf-8")
        worker = (ROOT / "k8s" / "lab4" / "worker.yaml").read_text(encoding="utf-8")
        self.assertIn("task_id BIGINT PRIMARY KEY", schema)
        self.assertIn("priority BETWEEN 1 AND 3", schema)
        self.assertIn("idx_tasks_claim", schema)
        self.assertNotIn("replicas:", worker.split("kind: Deployment", 1)[1].split("---", 1)[0])
        self.assertIn("terminationGracePeriodSeconds: 30", worker)
        self.assertIn("fieldPath: metadata.name", worker)

    def test_raw_hashes_are_computed_from_written_bytes(self):
        orchestrator = (ROOT / "scripts" / "run_lab4.py").read_text(encoding="utf-8")
        self.assertIn('open("w", encoding="utf-8", newline="\\n")', orchestrator)
        self.assertIn('(run_dir / "tasks.jsonl").read_bytes()', orchestrator)
        self.assertIn('(run_dir / "task_results.jsonl").read_bytes()', orchestrator)

    def test_controlled_drain_does_not_change_the_measurement_window(self):
        orchestrator = (ROOT / "scripts" / "run_lab4.py").read_text(encoding="utf-8")
        measurement = orchestrator.index('write_json(run_dir / "measurement_end.json"')
        drain = orchestrator.index('configure_workers(name + "_controlled_drain"')
        self.assertLess(measurement, drain)
        self.assertIn("lambda i: 3 if poison_start <= i < poison_start + 10", orchestrator)

    def test_required_lab4_evidence_is_persisted(self):
        orchestrator = (ROOT / "scripts" / "run_lab4.py").read_text(encoding="utf-8")
        analyzer = (ROOT / "scripts" / "analyze_lab4.py").read_text(encoding="utf-8")
        self.assertIn('"claims_empty_total"', orchestrator)
        self.assertIn("write_database_snapshot(run_dir, \"at_measurement_end\")", orchestrator)
        self.assertIn("phase_capacity30", orchestrator)
        self.assertIn("queue_axis.axvline(150", analyzer)


if __name__ == "__main__":
    unittest.main()

