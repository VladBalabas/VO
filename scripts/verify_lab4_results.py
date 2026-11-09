"""Незалежна перевірка ЛР4 без імпорту основного аналізатора."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "results" / "lab4_final"
RUNS = DATA / "runs"
EXPECTED = ("worker20", "naive_100ms", "naive_1ms", "skip_locked_1ms", "locked_1ms",
            "lease_fencing", "capacity_w1", "capacity_w2", "capacity_w5",
            "capacity30_w1", "capacity30_w2", "capacity30_w5", "little_steady",
            "little_overload", "graceful_restart", "fail_baseline", "fail_poison", "priority_d1")


def source(run: str, name: str) -> Path:
    plain = RUNS / run / name
    archive = plain.with_suffix(plain.suffix + ".gz")
    if archive.exists(): return archive
    if plain.exists(): return plain
    raise FileNotFoundError(plain)


def read_bytes(path: Path) -> bytes:
    return gzip.open(path, "rb").read() if path.suffix == ".gz" else path.read_bytes()


def rows(run: str, name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in read_bytes(source(run, name)).decode("utf-8").splitlines() if line]


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def capacity(run: str) -> float:
    tasks = [row for row in rows(run, "tasks.jsonl") if row["status"] == "done"]
    finished = sorted(dt(row["finished_at"]).timestamp() for row in tasks)
    start = min(dt(row["started_at"]).timestamp() for row in tasks)
    end = max(finished); window_start = max(start, end - 30)
    return sum(value >= window_start for value in finished) / (end - window_start)


def weighted_queue(samples: list[dict[str, Any]], start: float, end: float) -> float:
    points = sorted((dt(row["db_at"]).timestamp(), row["queued"]) for row in samples
                    if "db_at" in row and start <= dt(row["db_at"]).timestamp() <= end)
    area = sum(points[index - 1][1] * (points[index][0] - points[index - 1][0])
               for index in range(1, len(points)))
    return area / (points[-1][0] - points[0][0])


def main() -> None:
    analysis = json.loads((DATA / "analysis.json").read_text(encoding="utf-8"))
    checked = []
    for run in EXPECTED:
        summary = json.loads((RUNS / run / "summary.json").read_text(encoding="utf-8"))
        for name, expected_hash in summary["primary_sha256"].items():
            actual = hashlib.sha256(read_bytes(source(run, name))).hexdigest()
            if actual != expected_hash: raise AssertionError((run, name, actual, expected_hash))
        requests = rows(run, "requests.jsonl"); tasks = rows(run, "tasks.jsonl")
        if len(requests) != summary["http"]["scheduled"]: raise AssertionError(run)
        if len(tasks) != summary["database"]["tasks"]: raise AssertionError(run)
        checked.append({"run": run, "requests": len(requests), "tasks": len(tasks)})

    for workers in (1, 2, 5):
        actual = capacity(f"capacity_w{workers}")
        expected = analysis["capacity"][str(workers)]["x"]
        if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12):
            raise AssertionError((workers, actual, expected))
        actual30 = capacity(f"capacity30_w{workers}")
        stored30 = analysis["capacity_30s_confirmation"][str(workers)]
        if not stored30["full_30_second_window"] or not math.isclose(
            actual30, stored30["x"], rel_tol=0, abs_tol=1e-12
        ):
            raise AssertionError(("capacity30", workers, actual30, stored30))

    locked_metrics = json.loads(
        (RUNS / "locked_1ms" / "worker_metrics_snapshot.json").read_text(encoding="utf-8")
    )["totals"]
    if locked_metrics["task"] != 300 or locked_metrics["empty"] <= 0:
        raise AssertionError(("locked metrics", locked_metrics))

    for run in ("fail_baseline", "fail_poison"):
        measurement = json.loads(
            (RUNS / run / "measurement_end.json").read_text(encoding="utf-8")
        )
        measured_tasks = rows(run, "tasks_at_measurement_end.jsonl")
        distribution = Counter(int(row["attempts"]) for row in measured_tasks)
        expected_distribution = {
            int(key): value
            for key, value in measurement["database"]["attempts_distribution"].items()
        }
        if len(measured_tasks) != measurement["counts"]["total"]:
            raise AssertionError((run, "measurement task count"))
        if dict(sorted(distribution.items())) != dict(sorted(expected_distribution.items())):
            raise AssertionError((run, distribution, expected_distribution))

    for run in ("skip_locked_1ms", "locked_1ms", "lease_fencing", "graceful_restart",
                "fail_baseline", "fail_poison", "priority_d1"):
        counts = Counter(row["task_id"] for row in rows(run, "task_results.jsonl"))
        if any(value > 1 for value in counts.values()): raise AssertionError(f"Дубль у {run}")

    tasks = rows("little_steady", "tasks.jsonl")
    created = sorted(dt(row["created_at"]).timestamp() for row in tasks)
    end = max(created); start = end - 120
    cohort = [row for row in tasks if start <= dt(row["created_at"]).timestamp() <= end]
    lam = len(cohort) / (end - start)
    wq = statistics.fmean((dt(row["started_at"]) - dt(row["created_at"])).total_seconds()
                          for row in cohort if row["started_at"])
    lq = weighted_queue(json.loads((RUNS / "little_steady" / "samples.json").read_text()), start, end)
    stored = analysis["little_steady"]
    for actual, expected in ((lam, stored["lambda"]), (wq, stored["W_q"]), (lq, stored["L_q"])):
        if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12):
            raise AssertionError((actual, expected))

    result = {"verification_passed": True, "runs": checked,
              "total_requests": sum(row["requests"] for row in checked),
              "checks": ["SHA-256 усіх первинних JSONL", "кількість запитів і задач",
                         "X(W) з finished_at", "повні 30-секундні capacity-вікна",
                         "точні task/empty claims", "snapshot failure на 300-й секунді",
                         "відсутність дублів у фінальних режимах",
                         "незалежні lambda, W_q і часово-зважене L_q"]}
    (DATA / "independent_verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

