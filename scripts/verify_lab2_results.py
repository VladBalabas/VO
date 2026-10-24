"""Independent consistency checks for the saved Lab 2 raw results.

This script deliberately does not import the analysis module.  It recomputes
the primary throughput values and USL parameters from JSON inputs and writes a
machine-readable verification record.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "results" / "lab2_final"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def is_success(record: dict) -> bool:
    status = record["status"]
    return status is not None and 200 <= status < 300


def refit_usl(rows: list[tuple[int, float]], x1: float) -> dict[str, float]:
    design = [(n - 1.0, n * (n - 1.0), n * x1 / x - 1.0)
              for n, x in rows if n > 1]
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


def verify_profile(name: str) -> dict:
    path = DATA / "raw" / f"{name}_profile.json"
    raw = load(path)
    expected = 2000 + sum(step["rate"] * 20 for step in raw["steps"])
    assert raw["mode"] == "profile"
    assert raw["requests_scheduled"] == expected == raw["requests_sent"]
    assert len(raw["samples"]) == expected
    assert raw["nominal_duration_seconds"] == 10 + 20 * len(raw["steps"])
    recalculated = []
    for index, step in enumerate(raw["steps"]):
        assert step["step_index"] == index
        assert step["start_offset_ms"] == (10 + 20 * index) * 1000
        start = step["start_offset_ms"]
        window = [r for r in raw["samples"]
                  if start + 5000 <= r["completed_offset_ms"] < start + 20000]
        counts = [0] * 15
        for record in window:
            if is_success(record):
                counts[int((record["completed_offset_ms"] - start - 5000) // 1000)] += 1
        x = statistics.fmean(counts)
        sd = statistics.stdev(counts)
        timeouts = sum(r["error_type"] == "TimeoutError" for r in window)
        connection_errors = sum(
            r["status"] is None and r["error_type"] != "TimeoutError" for r in window
        )
        non_2xx = sum(
            (r["status"] is not None and not 200 <= r["status"] < 300)
            or r["error_type"] == "TimeoutError" for r in window
        )
        assert abs(x - step["x"]) < 1e-9
        assert abs(sd - step["stddev"]) < 1e-9
        assert counts == step["counts_last_15s"]
        assert timeouts == step["timeouts"]
        assert connection_errors == step["connection_errors"]
        assert non_2xx == step["non_2xx"]
        sent_during_step = sum(
            start <= r["sent_offset_ms"] < start + 20000 for r in raw["samples"]
        )
        assert sent_during_step == step["sent_during_step"]
        assert step["scheduled"] == step["rate"] * 20
        recalculated.append({"rate": step["rate"], "x": x, "stddev": sd,
                             "non_2xx": non_2xx, "timeouts": timeouts,
                             "connection_errors": connection_errors,
                             "scheduled": step["scheduled"],
                             "sent_during_step": sent_during_step})
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"profile": name, "sha256": digest, "scheduled": expected,
            "stop_two_misses": raw["stop_two_misses"], "steps": recalculated}


def main() -> None:
    profiles = [verify_profile("noop")]
    profiles.extend(verify_profile(f"n{n}") for n in range(1, 5))
    analysis = load(DATA / "analysis.json")
    all_rows = [(row["n"], row["x"]) for row in analysis["table"]]
    rows = [(row["n"], row["x"]) for row in analysis["table"]
            if not row["generator_limited"]]
    independent_usl = refit_usl(rows, all_rows[0][1])
    recorded_usl = analysis["usl"]
    assert recorded_usl is not None
    assert math.isclose(independent_usl["sigma"], recorded_usl["sigma"], abs_tol=1e-12)
    assert math.isclose(independent_usl["kappa"], recorded_usl["kappa"], abs_tol=1e-12)
    result = {
        "verification_passed": True,
        "definitions": {
            "X": "mean successful completions/s in seconds 6..20 of each step",
            "timeout": "counted in non_2xx, separately reported",
            "connection_error": "status is null and error type is not TimeoutError",
        },
        "profiles": profiles,
        "independent_usl": independent_usl,
        "checks": [
            "continuous offsets and exact scheduled counts",
            "all scheduled requests dispatched",
            "X and sample standard deviation recomputed from completion timestamps",
            "timeouts and connection errors recomputed independently",
            "raw SHA-256 recorded",
            "USL parameters recomputed without the analysis module",
        ],
    }
    destination = DATA / "independent_verification.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verification_passed": True,
                      "usl": independent_usl,
                      "profiles": [p["profile"] for p in profiles]}, indent=2))


if __name__ == "__main__":
    main()
