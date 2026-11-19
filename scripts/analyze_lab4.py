"""Аналіз первинних даних лабораторної роботи №4."""
from __future__ import annotations

import json
import gzip
import bisect
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "results" / "lab4_final"
RUNS = DATA / "runs"
FIG = DATA / "figures"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        with gzip.open(path.with_suffix(path.suffix + ".gz"), "rt", encoding="utf-8") as stream:
            text = stream.read()
    return [json.loads(line) for line in text.splitlines() if line]


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[max(0, min(len(values) - 1, math.ceil(q * len(values)) - 1))]


def log_events(name: str) -> list[dict[str, Any]]:
    result = []
    for line in (RUNS / name / "worker.log").read_text(encoding="utf-8").splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            result.append(json.loads(line[start:]))
        except json.JSONDecodeError:
            pass
    return result


def duplicate_result(name: str) -> dict[str, Any]:
    tasks = load_jsonl(RUNS / name / "tasks.jsonl")
    results = load_jsonl(RUNS / name / "task_results.jsonl")
    result_counts = Counter(row["task_id"] for row in results)
    log_counts = Counter(event["task_id"] for event in log_events(name)
                         if event.get("event") == "task_done")
    prom = load_json(RUNS / name / "prometheus.json")
    metric_snapshot = load_json(RUNS / name / "worker_metrics_snapshot.json")

    def last_value(key: str) -> float | None:
        values = [float(item[1]) for row in prom.get(key, [])
                  for item in row.get("values", [])
                  if item[1] not in {"NaN", "+Inf", "-Inf"}]
        return values[-1] if values else None

    histogram_p99 = last_value("claim_p99_seconds")
    return {
        "n": len(tasks),
        "duplicate_by_results": sum(value > 1 for value in result_counts.values()),
        "duplicate_by_logs": sum(value > 1 for value in log_counts.values()),
        "D_results": sum(value > 1 for value in result_counts.values()) / len(tasks),
        "D_logs": sum(value > 1 for value in log_counts.values()) / len(tasks),
        "p99_claim_ms": percentile([event["duration_ms"] for event in log_events(name)
                                     if event.get("event") == "claim"], 0.99),
        "p99_claim_histogram_ms": (
            histogram_p99 * 1000 if histogram_p99 is not None else None
        ),
        "claims_task_total": metric_snapshot["totals"]["task"],
        "claims_empty_total": metric_snapshot["totals"]["empty"],
        "max_lock_waiters": max((row.get("lock_waiters", 0) for row in
                                 load_json(RUNS / name / "samples.json") if "lock_waiters" in row),
                                default=0),
    }


def capacity(name: str, workers: int) -> dict[str, Any]:
    tasks = [row for row in load_jsonl(RUNS / name / "tasks.jsonl") if row["status"] == "done"]
    finished = sorted(dt(row["finished_at"]).timestamp() for row in tasks)
    started = min(dt(row["started_at"]).timestamp() for row in tasks)
    end = max(finished)
    window_start = max(started, end - 30)
    selected = [value for value in finished if window_start <= value <= end]
    duration = end - window_start
    bucket_count = max(1, math.ceil(duration))
    seconds = [0] * bucket_count
    for value in selected:
        seconds[min(bucket_count - 1, int(value - window_start))] += 1
    events = log_events(name)
    prom = load_json(RUNS / name / "prometheus.json")
    def series_mean(key: str) -> float | None:
        values = [float(item[1]) for row in prom.get(key, []) for item in row.get("values", [])
                  if item[1] not in {"NaN", "+Inf", "-Inf"}]
        return statistics.fmean(values) if values else None
    return {
        "workers": workers, "done": len(tasks), "window_seconds": duration,
        "full_30_second_window": duration >= 29.999,
        "x": len(selected) / duration,
        "stddev_per_second": statistics.stdev(seconds) if len(seconds) > 1 else 0.0,
        "per_second": seconds,
        "p99_claim_ms": percentile([event["duration_ms"] for event in events
                                     if event.get("event") == "claim"], 0.99),
        "db_transactions_per_second": series_mean("db_transactions"),
        "db_cpu_cores": series_mean("db_cpu"),
    }


def usl(rows: list[dict[str, Any]]) -> dict[str, Any]:
    x1 = rows[0]["x"]
    design = []
    for row in rows[1:]:
        n = row["workers"]; c = row["x"] / x1
        design.append({"W": n, "C": c, "y": n / c - 1,
                       "u": n - 1, "v": n * (n - 1)})
    suu = sum(row["u"] ** 2 for row in design)
    svv = sum(row["v"] ** 2 for row in design)
    suv = sum(row["u"] * row["v"] for row in design)
    suy = sum(row["u"] * row["y"] for row in design)
    svy = sum(row["v"] * row["y"] for row in design)
    determinant = suu * svv - suv**2
    sigma0 = (suy * svv - svy * suv) / determinant
    kappa0 = (svy * suu - suy * suv) / determinant
    sigma, kappa = sigma0, kappa0
    projection = "none"
    if sigma < 0:
        sigma = 0.0; kappa = max(0.0, svy / svv); projection = "sigma=0, refit kappa"
    if kappa < 0:
        kappa = 0.0; sigma = max(0.0, suy / suu); projection = "kappa=0, refit sigma"
    predicted = {str(n): n * x1 / (1 + sigma * (n - 1) + kappa * n * (n - 1))
                 for n in range(1, 11)}
    optimum = math.sqrt((1 - sigma) / kappa) if kappa > 0 and sigma < 1 else None
    return {"design": design, "sums": {"sum_u2": suu, "sum_v2": svv,
            "sum_uv": suv, "sum_uy": suy, "sum_vy": svy, "determinant": determinant},
            "sigma_unconstrained": sigma0, "kappa_unconstrained": kappa0,
            "projection": projection, "sigma": sigma, "kappa": kappa,
            "W_star": optimum, "predicted_x": predicted}


def weighted_sample(samples: list[dict[str, Any]], start: float, end: float,
                    value: Any) -> float:
    points = sorted((dt(row["db_at"]).timestamp(), value(row)) for row in samples
                    if "db_at" in row and start <= dt(row["db_at"]).timestamp() <= end)
    if not points:
        return math.nan
    area = 0.0
    previous_t, previous_q = points[0]
    for current_t, current_q in points[1:]:
        area += previous_q * (current_t - previous_t)
        previous_t, previous_q = current_t, current_q
    covered = points[-1][0] - points[0][0]
    return area / covered if covered else float(points[0][1])


def little(name: str, steady: bool) -> dict[str, Any]:
    tasks = load_jsonl(RUNS / name / "tasks.jsonl")
    samples = load_json(RUNS / name / "samples.json")
    created = sorted(dt(row["created_at"]).timestamp() for row in tasks)
    end = max(created); start = end - (120 if steady else end - min(created))
    cohort = [row for row in tasks if start <= dt(row["created_at"]).timestamp() <= end]
    waits = [(dt(row["started_at"]) - dt(row["created_at"])).total_seconds()
             for row in cohort if row["started_at"]]
    sojourns = [(dt(row["finished_at"]) - dt(row["created_at"])).total_seconds()
                for row in cohort if row["finished_at"]]
    result = {"window_start": start, "window_end": end, "created": len(cohort),
              "lambda": len(cohort) / (end - start), "W_q": statistics.fmean(waits),
              "L_q": weighted_sample(samples, start, end, lambda row: row["queued"]),
              "W_system": statistics.fmean(sojourns),
              "L_unfinished": weighted_sample(
                  samples, start, end, lambda row: row["queued"] + row["running"])}
    result["lambda_W_q"] = result["lambda"] * result["W_q"]
    result["lambda_W_system"] = result["lambda"] * result["W_system"]
    result["difference_percent"] = (abs(result["L_q"] - result["lambda_W_q"])
                                    / result["L_q"] * 100 if result["L_q"] else None)
    result["system_difference_percent"] = (
        abs(result["L_unfinished"] - result["lambda_W_system"])
        / result["L_unfinished"] * 100 if result["L_unfinished"] else None)
    if not steady:
        ordered = sorted((dt(row["created_at"]), row) for row in tasks)
        first = [row for _, row in ordered[:100]]; last = [row for _, row in ordered[-100:]]
        result["W_q_first_100"] = statistics.fmean(
            (dt(row["started_at"]) - dt(row["created_at"])).total_seconds() for row in first)
        result["W_q_last_100"] = statistics.fmean(
            (dt(row["started_at"]) - dt(row["created_at"])).total_seconds() for row in last)
        measured = [row for row in samples if "db_at" in row and
                    dt(row["db_at"]).timestamp() <= end]
        result["queue_at_generation_end"] = measured[-1].get("queued") if measured else None
    return result


def run_throughput(name: str) -> float:
    summary = load_json(RUNS / name / "summary.json")
    duration = summary["config"]["duration"]
    measurement = RUNS / name / "measurement_end.json"
    if measurement.exists():
        return load_json(measurement)["counts"]["done"] / duration
    tasks = load_jsonl(RUNS / name / "tasks.jsonl")
    return sum(row["status"] == "done" for row in tasks) / duration


def priority_result() -> dict[str, Any]:
    tasks = load_jsonl(RUNS / "priority_d1" / "tasks.jsonl")
    waits: dict[int, list[float]] = defaultdict(list)
    for row in tasks:
        if row["started_at"]:
            waits[int(row["priority"])].append(
                (dt(row["started_at"]) - dt(row["created_at"])).total_seconds())
    by_priority = {str(priority): {"count": len(values),
                                   "mean_W_q": statistics.fmean(values),
                                   "p50_W_q": percentile(values, 0.50),
                                   "p99_W_q": percentile(values, 0.99)}
                   for priority, values in sorted(waits.items())}
    ordered = sorted((dt(row["started_at"]).timestamp(), row) for row in tasks
                     if row["started_at"])
    higher_index: dict[int, tuple[list[float], list[float]]] = {}
    for priority in (1, 2, 3):
        candidates = sorted((dt(row["created_at"]).timestamp(),
                             dt(row["started_at"]).timestamp())
                            for row in tasks if int(row["priority"]) == priority
                            and row["started_at"])
        created_values: list[float] = []
        prefix_max_started: list[float] = []
        maximum = -math.inf
        for created_at, started_at in candidates:
            created_values.append(created_at)
            maximum = max(maximum, started_at)
            prefix_max_started.append(maximum)
        higher_index[priority] = (created_values, prefix_max_started)
    violations = 0
    for started, row in ordered:
        violated = False
        for higher in range(int(row["priority"]) + 1, 4):
            created_values, prefix_max_started = higher_index[higher]
            index = bisect.bisect_right(created_values, started) - 1
            if index >= 0 and prefix_max_started[index] > started:
                violated = True
                break
        if violated:
            violations += 1
    return {"by_priority": by_priority,
            "claims_with_waiting_higher_priority": violations,
            "claim_count": len(ordered)}


def pod_restart_count(name: str) -> int:
    path = RUNS / name / "pods_at_measurement_end.json"
    if not path.exists():
        return 0
    payload = load_json(path)
    return sum(status.get("restartCount", 0)
               for item in payload.get("items", [])
               for status in item.get("status", {}).get("containerStatuses", []))


def failure_result(name: str) -> dict[str, Any]:
    tasks = load_jsonl(RUNS / name / "tasks.jsonl")
    summary = load_json(RUNS / name / "summary.json")
    measurement = load_json(RUNS / name / "measurement_end.json")
    measurement_tasks = load_jsonl(RUNS / name / "tasks_at_measurement_end.jsonl")
    attempts = [int(row["attempts"]) for row in tasks]
    measurement_captured = [row for row in measurement_tasks if int(row["attempts"]) > 0]
    measurement_poison = [row for row in measurement_tasks if row.get("seed") == 0]
    duration = float(summary["config"]["duration"])
    captured_count = len(measurement_captured)
    attempts_gt_1_count = sum(int(row["attempts"]) > 1 for row in measurement_captured)
    failed_count = sum(row["status"] == "failed" for row in measurement_captured)
    result = {
        "measurement_end": measurement,
        "measurement_x_done_per_second": measurement["counts"]["done"] / duration,
        "pod_restart_count_at_measurement_end": pod_restart_count(name),
        "final_tasks": len(tasks),
        "final_done": sum(row["status"] == "done" for row in tasks),
        "final_failed": sum(row["status"] == "failed" for row in tasks),
        "observed_mean_attempts_after_drain": statistics.fmean(attempts),
        "measurement_attempts_distribution": measurement["database"]["attempts_distribution"],
        "measurement_attempts_gt_1": measurement["database"]["attempts_gt_1"],
        "measurement_failed": measurement["database"]["failed"],
        "measurement_uncaptured_tasks": len(measurement_tasks) - captured_count,
        "measurement_captured_tasks": captured_count,
        "measurement_attempts_gt_1_captured": attempts_gt_1_count,
        "measurement_attempts_gt_1_fraction_of_captured": (
            attempts_gt_1_count / captured_count if captured_count else None
        ),
        "measurement_failed_captured": failed_count,
        "measurement_failed_fraction_of_captured": (
            failed_count / captured_count if captured_count else None
        ),
    }
    measurement_normal_waits = [
        (dt(row["started_at"]) - dt(row["created_at"])).total_seconds()
        for row in measurement_tasks if row.get("seed") != 0 and row.get("started_at")
    ]
    result["normal_W_q_p99_at_measurement_end"] = percentile(
        measurement_normal_waits, .99
    )
    if measurement_poison:
        poison_times = [
            (dt(row["finished_at"]) - dt(row["created_at"])).total_seconds()
            for row in measurement_poison if row["finished_at"]
        ]
        result["poison_at_measurement_end"] = {
            "count": len(measurement_poison),
            "failed": sum(row["status"] == "failed" for row in measurement_poison),
            "attempts": Counter(int(row["attempts"]) for row in measurement_poison),
            "time_to_failed_p50_seconds": percentile(poison_times, .50),
            "time_to_failed_p99_seconds": percentile(poison_times, .99),
        }
    return result


def step_times() -> dict[str, float]:
    events = log_events("worker20")
    durations: dict[str, list[float]] = defaultdict(list)
    for event in events:
        if event.get("event") == "step": durations[event["step"]].append(event["duration_ms"] / 1000)
    means = {step: statistics.fmean(values) for step, values in durations.items()}
    means["t_io"] = means.get("fetch", 0) + means.get("store", 0)
    means["t_cpu"] = means.get("process", 0)
    means["predicted_x1"] = 1 / (means["t_io"] + means["t_cpu"] + 0.1 / 2)
    return means


def plot_capacity(rows: list[dict[str, Any]], model: dict[str, Any]) -> None:
    import matplotlib.pyplot as plt

    FIG.mkdir(parents=True, exist_ok=True)
    ns = [row["workers"] for row in rows]; xs = [row["x"] for row in rows]
    plt.figure(figsize=(7, 4)); plt.errorbar(ns, xs, yerr=[row["stddev_per_second"] for row in rows],
                                             fmt="o", capsize=4, label="Вимірювання")
    curve_n = list(range(1, 11)); plt.plot(curve_n, [model["predicted_x"][str(n)] for n in curve_n],
                                           label="USL")
    plt.xlabel("Кількість воркерів W"); plt.ylabel("Завершені задачі/с")
    plt.grid(alpha=.3); plt.legend(); plt.tight_layout(); plt.savefig(FIG / "capacity_usl.png", dpi=160); plt.close()


def plot_timeline(name: str, destination: str) -> None:
    import matplotlib.pyplot as plt

    source = RUNS / name / "samples.json"
    if not source.exists():
        return
    samples = [row for row in load_json(source) if "db_at" in row]
    if not samples: return
    origin = dt(samples[0]["db_at"])
    t = [(dt(row["db_at"]) - origin).total_seconds() for row in samples]
    plt.figure(figsize=(8, 4));
    for field, label in (("queued", "queued"), ("running", "running"), ("done", "done")):
        plt.plot(t, [row[field] for row in samples], label=label)
    plt.xlabel("Час, с"); plt.ylabel("Кількість задач"); plt.grid(alpha=.3); plt.legend()
    plt.tight_layout(); plt.savefig(FIG / destination, dpi=160); plt.close()


def plot_failure_timeline() -> None:
    import matplotlib.pyplot as plt

    run_name = "f3_poison"
    samples = [row for row in load_json(RUNS / run_name / "samples.json")
               if "db_at" in row]
    if len(samples) < 2:
        raise RuntimeError(f"Недостатньо вибірок {run_name} для часової діаграми")
    poison_at = load_json(RUNS / run_name / "summary.json")["config"]["poison_at"]
    origin = dt(samples[0]["db_at"])
    samples = [row for row in samples
               if (dt(row["db_at"]) - origin).total_seconds() <= 300]
    t = [(dt(row["db_at"]) - origin).total_seconds() for row in samples]
    done_rate = [0.0]
    for previous, current in zip(samples, samples[1:]):
        elapsed = (dt(current["db_at"]) - dt(previous["db_at"])).total_seconds()
        done_rate.append(
            (current["done"] - previous["done"]) / elapsed if elapsed > 0 else 0.0
        )
    figure, queue_axis = plt.subplots(figsize=(9, 4.5))
    queue_axis.plot(t, [row["queued"] for row in samples], label="queued")
    queue_axis.plot(t, [row["running"] for row in samples], label="running")
    queue_axis.set_xlabel("Час, с")
    queue_axis.set_ylabel("Кількість задач")
    rate_axis = queue_axis.twinx()
    rate_axis.plot(t, done_rate, color="tab:green", alpha=.75, label="done/с")
    rate_axis.set_ylabel("Завершено задач/с")
    queue_axis.axvline(poison_at, color="tab:red", linestyle="--",
                       label="10 задач seed=0")
    lines = queue_axis.get_lines() + rate_axis.get_lines()
    queue_axis.legend(lines, [line.get_label() for line in lines], loc="upper left")
    queue_axis.grid(alpha=.3)
    figure.tight_layout()
    figure.savefig(FIG / "fail_poison.png", dpi=160)
    plt.close(figure)


def main() -> None:
    claim_names = ("naive_100ms", "naive_1ms", "skip_locked_1ms", "locked_1ms")
    duplicates = {name: duplicate_result(name) for name in claim_names}
    capacities = [capacity(f"capacity_w{workers}", workers) for workers in (1, 2, 5)]
    capacities30 = [capacity(f"capacity30_w{workers}", workers) for workers in (1, 2, 5)]
    model = usl(capacities)
    baseline = load_json(RUNS / "fail_baseline" / "summary.json")
    poison = load_json(RUNS / "fail_poison" / "summary.json")
    baseline_measurement = failure_result("fail_baseline")
    poison_measurement = failure_result("fail_poison")
    f3_baseline = failure_result("f3_baseline")
    f3_poison = failure_result("f3_poison")
    analysis = {
        "definitions": {
            "X": "done tasks in the intersection of the final 30 s and the measured non-empty interval / actual interval seconds",
            "D": "distinct task ids with more than one result (or task_done log) / tasks in run",
            "L_q": "time-weighted independent SQL samples of queued count using actual timestamps",
            "lambda": "tasks created in window / actual window seconds",
        },
        "step_times": step_times(), "duplicates": duplicates,
        "capacity": {str(row["workers"]): row for row in capacities},
        "capacity_30s_confirmation": {str(row["workers"]): row for row in capacities30},
        "usl": model,
        "little_steady": little("little_steady", True),
        "little_overload": little("little_overload", False),
        "graceful_restart": load_json(RUNS / "graceful_restart" / "summary.json"),
        "failure": {
            "expected_attempts_unbounded": 1 / 0.85,
            "expected_attempts_max_3": 1 + 0.15 + 0.15**2,
            "expected_failed_fraction": 0.15**3,
            "baseline": baseline, "poison": poison,
            "baseline_x": run_throughput("fail_baseline"),
            "poison_x": run_throughput("fail_poison"),
            "fraction_denominator": "tasks with attempts > 0 at measurement end",
            "baseline_measurement": baseline_measurement,
            "poison_measurement": poison_measurement,
            "high_rate_poison_f3_metrics": {
                "usable": False,
                "reason": (
                    "poison tasks were not terminal at the 300 s measurement boundary; "
                    "post-drain values were produced with FAIL_RATE=0"
                ),
            },
            "f3_measurement_pair": {
                "rate": 0.2,
                "duration_seconds": 300,
                "poison_injected_at_seconds": 150,
                "baseline": f3_baseline,
                "poison": f3_poison,
                "normal_p99_comparison_seconds": {
                    "baseline": f3_baseline["normal_W_q_p99_at_measurement_end"],
                    "poison": f3_poison["normal_W_q_p99_at_measurement_end"],
                },
                "phase": "measurement snapshot before FAIL_RATE=0 controlled drain",
            },
            "limitation": "CrashLoopBackOff backoff dominates the 300 s window; FAIL_RATE was set to zero only after measurement for a controlled drain.",
        },
        "priority_d1": priority_result(),
        "lease_fencing": load_json(RUNS / "lease_fencing" / "lease_case.json"),
    }
    write = DATA / "analysis.json"
    write.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    plot_capacity(capacities, model)
    plot_timeline("little_steady", "little_steady.png")
    plot_timeline("little_overload", "little_overload.png")
    plot_failure_timeline()
    print(json.dumps({"duplicates": duplicates, "capacity": analysis["capacity"],
                      "usl": model, "little": analysis["little_steady"],
                      "priority": analysis["priority_d1"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

