"""Independent Lab 3 verification without importing the main analyzer."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "results" / "lab3_final"
RUNS = ("noop", "n1", "n2", "n3", "n5", "fixed_profile", "hpa30_profile",
        "d5_hpa30", "d5_hpa300")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def success(row: dict[str, Any]) -> bool:
    return row["status"] is not None and 200 <= row["status"] < 300


def raw_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def verify_run(name: str) -> dict[str, Any]:
    summary = load(DATA / "raw" / f"{name}.summary.json")
    raw = DATA / "raw" / f"{name}.jsonl"
    steps = summary.get("steps", [])
    step_counts = [[0] * 20 for _ in steps]
    step_latencies: list[list[float]] = [[] for _ in steps]
    step_pods: list[Counter[str]] = [Counter() for _ in steps]
    profile_duration = math.ceil(float(summary["nominal_duration_seconds"]))
    per_second = [{"sent": 0, "success": 0, "http_non_2xx": 0,
                   "timeouts": 0, "connection_errors": 0}
                  for _ in range(profile_duration)]
    connection_sequences: dict[tuple[int, int], list[int]] = defaultdict(list)
    status_counts = Counter()
    record_count = 0
    for line in raw.open(encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        record_count += 1
        connection_sequences[(row["worker"], row["session_generation"])].append(
            row["request_on_session"]
        )
        sent_second = int(row["sent_offset_ms"] // 1000)
        if 0 <= sent_second < profile_duration:
            per_second[sent_second]["sent"] += 1
        completed_second = int(row["completed_offset_ms"] // 1000)
        if 0 <= completed_second < profile_duration:
            target = per_second[completed_second]
            if success(row):
                target["success"] += 1
                status_counts["success"] += 1
            elif row["status"] is not None:
                target["http_non_2xx"] += 1
                status_counts["non_2xx"] += 1
            elif row["error_type"] == "TimeoutError":
                target["timeouts"] += 1
                status_counts["timeouts"] += 1
            else:
                target["connection_errors"] += 1
                status_counts["connection_errors"] += 1
        for index, step in enumerate(steps):
            start = float(step["measurement_start_offset_ms"])
            offset = row["completed_offset_ms"] - start
            if 0 <= offset < 20_000:
                if success(row):
                    step_counts[index][int(offset // 1000)] += 1
                    if row.get("pod"):
                        step_pods[index][row["pod"]] += 1
                step_latencies[index].append(row["latency_ms"])
                break

    assert record_count == summary["raw_records"] == summary["counts"]["scheduled"]
    assert summary["counts"]["scheduled"] == summary["counts"]["sent"] == summary["counts"]["completed"]
    digest = raw_sha256(raw)
    assert digest == summary["raw_sha256"]
    assert len(summary["connection_evidence"]) == 40
    assert {worker for worker, _ in connection_sequences} == set(range(40))
    for (worker, generation), sequence in connection_sequences.items():
        assert sequence == list(range(1, len(sequence) + 1)), (name, worker, generation)
        assert len(sequence) <= 100
        later_generation_exists = (worker, generation + 1) in connection_sequences
        if later_generation_exists:
            assert len(sequence) == 100
    verified_steps = []
    for index, step in enumerate(steps):
        counts = step_counts[index]
        x = statistics.fmean(counts)
        sd = statistics.stdev(counts)
        assert math.isclose(x, step["x"], abs_tol=1e-9)
        assert math.isclose(sd, step["stddev"], abs_tol=1e-9)
        assert counts == step["counts_last_20s"]
        assert dict(sorted(step_pods[index].items())) == step["pod_success_counts"]
        verified_steps.append({"rate": step["rate"], "x": x, "stddev": sd})

    stored_seconds = load(DATA / "per_second" / f"{name}.json")
    assert len(stored_seconds) == len(per_second)
    for actual, expected in zip(per_second, stored_seconds):
        assert actual["sent"] == expected["sent"]
        assert actual["success"] == expected["completed_2xx"]
        assert actual["http_non_2xx"] == expected["http_non_2xx"]
        assert actual["timeouts"] == expected["timeouts"]
        assert actual["connection_errors"] == expected["connection_errors"]
    return {"run": name, "raw_records": record_count, "sha256": digest,
            "observed_sessions": len(connection_sequences), "steps": verified_steps,
            "statuses_within_nominal_profile": dict(status_counts)}


def independent_fit(rows: list[tuple[int, float]], x1: float) -> dict[str, float]:
    design = [(n - 1.0, n * (n - 1.0), n * x1 / value - 1.0)
              for n, value in rows if n > 1]
    suu = sum(u * u for u, _, _ in design)
    svv = sum(v * v for _, v, _ in design)
    suv = sum(u * v for u, v, _ in design)
    suy = sum(u * y for u, _, y in design)
    svy = sum(v * y for _, v, y in design)
    determinant = suu * svv - suv * suv
    sigma = (suy * svv - svy * suv) / determinant
    kappa = (svy * suu - suy * suv) / determinant
    if sigma < 0 and kappa < 0:
        sigma = kappa = 0.0
    elif sigma < 0:
        sigma = 0.0
        kappa = max(0.0, svy / svv)
    elif kappa < 0:
        kappa = 0.0
        sigma = max(0.0, suy / suu)
    return {"sigma": sigma, "kappa": kappa}


def verify_elasticity(analysis: dict[str, Any]) -> dict[str, Any]:
    results = {}
    for name, stored in analysis["elasticity"].items():
        timeline = stored["timeline"]
        demand = timeline["demand"]
        ready = timeline["ready"]
        duration = len(ready)
        recalculated = {
            "theta_U": sum(s < d for s, d in zip(ready, demand)) / duration,
            "theta_O": sum(s > d for s, d in zip(ready, demand)) / duration,
            "a_U": sum(max(d - s, 0) for s, d in zip(ready, demand)) / duration,
            "a_O": sum(max(s - d, 0) for s, d in zip(ready, demand)) / duration,
            "W_replica_seconds": sum(ready),
        }
        for key, value in recalculated.items():
            assert math.isclose(value, stored[key], abs_tol=1e-12), (name, key)
        results[name] = recalculated
    return results


def main() -> None:
    runs = [verify_run(name) for name in RUNS]
    analysis = load(DATA / "analysis.json")
    eligible = [(row["n"], row["x"]) for row in analysis["fixed_table"]
                if row["eligible_for_usl"]]
    fit = independent_fit(eligible, analysis["fixed_table"][0]["x"])
    assert math.isclose(fit["sigma"], analysis["usl"]["sigma"], abs_tol=1e-12)
    assert math.isclose(fit["kappa"], analysis["usl"]["kappa"], abs_tol=1e-12)
    elasticity = verify_elasticity(analysis)
    result = {
        "verification_passed": True,
        "definitions": {
            "X": "2xx completions in each actual second of the last 20 seconds",
            "raw": "newline-delimited request records hashed before verification",
            "connections": "40 workers; ordered session counters 1..100; every non-final session has 100 attempts",
        },
        "runs": runs,
        "independent_usl": fit,
        "independent_elasticity": elasticity,
        "checks": [
            "all raw SHA-256 values match summaries",
            "raw record counts equal scheduled/sent/completed counts",
            "X and sample SD recomputed from completion timestamps",
            "per-pod counts recomputed from X-Pod",
            "40 connection workers and rotation after 100 attempts verified",
            "per-second success/error counts recomputed from raw",
            "USL refit without NumPy or analyzer import",
            "elasticity formulas recomputed independently",
        ],
    }
    (DATA / "independent_verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"verification_passed": True, "runs": len(runs),
                      "usl": fit, "raw_records": sum(row["raw_records"] for row in runs)},
                     indent=2))


if __name__ == "__main__":
    main()
