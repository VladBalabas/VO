"""Rebuild Lab 3 tables, elasticity metrics and figures from immutable raw."""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "results" / "lab3_final"
FIG = DATA / "figures"
RUNS = ("noop", "n1", "n2", "n3", "n5", "fixed_profile", "hpa30_profile",
        "d5_hpa30", "d5_hpa300")


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=32)
def summary(name: str) -> dict[str, Any]:
    return read(DATA / "raw" / f"{name}.summary.json")


def raw_records(name: str) -> Iterable[dict[str, Any]]:
    with (DATA / "raw" / f"{name}.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def is_success(row: dict[str, Any]) -> bool:
    return row["status"] is not None and 200 <= row["status"] < 300


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))]


def fit_usl(rows: list[tuple[int, float]], x1: float) -> dict[str, Any] | None:
    design = [(n - 1.0, n * (n - 1.0), n * x1 / value - 1.0)
              for n, value in rows if n > 1]
    if len(design) < 2:
        return None
    matrix = np.array([[u, v] for u, v, _ in design], dtype=float)
    target = np.array([y for _, _, y in design], dtype=float)
    sigma, kappa = np.linalg.lstsq(matrix, target, rcond=None)[0]
    if sigma < 0 and kappa < 0:
        sigma = kappa = 0.0
    elif sigma < 0:
        sigma = 0.0
        kappa = max(0.0, float(np.dot(matrix[:, 1], target) /
                               np.dot(matrix[:, 1], matrix[:, 1])))
    elif kappa < 0:
        kappa = 0.0
        sigma = max(0.0, float(np.dot(matrix[:, 0], target) /
                               np.dot(matrix[:, 0], matrix[:, 0])))
    result = {
        "sigma": float(sigma),
        "kappa": float(kappa),
        "N_star": math.sqrt((1 - sigma) / kappa) if kappa > 0 and sigma < 1 else None,
        "no_finite_maximum": bool(kappa == 0),
    }
    result["predicted_x"] = {
        str(n): x1 * n / (1 + sigma * (n - 1) + kappa * n * (n - 1))
        for n in range(1, 6)
    }
    return result


def metric_rows(name: str, key: str) -> list[tuple[dict[str, str], float, float]]:
    payload = read(DATA / "metrics" / f"{name}.json")["values"][key]
    result = []
    for series in payload.get("data", {}).get("result", []):
        labels = series["metric"]
        for epoch, value in series.get("values", []):
            if value not in ("NaN", "+Inf", "-Inf"):
                result.append((labels, float(epoch), float(value)))
    return result


def build_per_second(name: str) -> list[dict[str, Any]]:
    info = summary(name)
    duration = math.ceil(float(info["nominal_duration_seconds"]))
    rows = [{
        "second": second, "planned": 0.0, "sent": 0, "completed_2xx": 0,
        "http_non_2xx": 0, "timeouts": 0, "connection_errors": 0,
        "latencies": [], "successful_latencies": [],
    } for second in range(duration)]
    if info["mode"] == "profile":
        offset = 0
        for segment in info["profile"]:
            end = offset + int(segment["duration"])
            for second in range(offset, min(end, duration)):
                rows[second]["planned"] = float(segment["rate"])
            offset = end
    else:
        for second in range(min(int(info.get("warmup_duration", 10)), duration)):
            rows[second]["planned"] = 200
        for step in info["steps"]:
            start = int(step["start_offset_ms"] // 1000)
            end = min(duration, start + int(step["duration_seconds"]))
            for second in range(start, end):
                rows[second]["planned"] = float(step["rate"])

    for record in raw_records(name):
        sent_second = int(record["sent_offset_ms"] // 1000)
        if 0 <= sent_second < duration:
            rows[sent_second]["sent"] += 1
        second = int(record["completed_offset_ms"] // 1000)
        if not 0 <= second < duration:
            continue
        row = rows[second]
        row["latencies"].append(record["latency_ms"])
        if is_success(record):
            row["completed_2xx"] += 1
            row["successful_latencies"].append(record["latency_ms"])
        elif record["status"] is not None:
            row["http_non_2xx"] += 1
        elif record["error_type"] == "TimeoutError":
            row["timeouts"] += 1
        else:
            row["connection_errors"] += 1
    for row in rows:
        row["client_p99_ms"] = quantile(row.pop("latencies"), .99)
        row["client_2xx_p99_ms"] = quantile(row.pop("successful_latencies"), .99)
    destination = DATA / "per_second"
    destination.mkdir(exist_ok=True)
    (destination / f"{name}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return rows


def window_raw(name: str, start_ms: float, end_ms: float) -> list[dict[str, Any]]:
    return [row for row in raw_records(name)
            if start_ms <= row["completed_offset_ms"] < end_ms]


def metric_by_pod(name: str, key: str, start: float, end: float) -> dict[str, list[float]]:
    values: dict[str, list[float]] = defaultdict(list)
    for labels, epoch, value in metric_rows(name, key):
        if start <= epoch < end:
            values[labels.get("pod", "?")].append(value)
    return dict(values)


def fixed_table() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    g = max(summary("noop")["steps"], key=lambda row: row["x"])["x"]
    table = []
    details = {}
    for n in (1, 2, 3, 5):
        name = f"n{n}"
        info = summary(name)
        winning = max(info["steps"], key=lambda row: row["x"])
        start_ms = winning["measurement_start_offset_ms"]
        end_ms = start_ms + 20_000
        records = window_raw(name, start_ms, end_ms)
        pod_counts: dict[str, int] = defaultdict(int)
        pod_latencies: dict[str, list[float]] = defaultdict(list)
        for row in records:
            if is_success(row) and row.get("pod"):
                pod_counts[row["pod"]] += 1
                pod_latencies[row["pod"]].append(row["network_latency_ms"])
        pod_rates = {pod: count / 20 for pod, count in pod_counts.items()}
        imbalance = max(pod_rates.values()) / statistics.fmean(pod_rates.values()) \
            if len(pod_rates) > 1 else 1.0
        start_epoch = info["started_at_epoch"] + start_ms / 1000
        end_epoch = info["started_at_epoch"] + end_ms / 1000
        server = metric_by_pod(name, "server_p99", start_epoch, end_epoch)
        server_all = metric_by_pod(name, "server_p99_all", start_epoch, end_epoch)
        # cAdvisor counters are refreshed roughly every 10-15 seconds.  The
        # saved ratio query can therefore be NaN at an exact 5-second query
        # timestamp.  Include the immediately adjacent samples whose 20-second
        # range still overlaps the measurement window, and aggregate per pod.
        throttle = metric_by_pod(name, "cfs_throttling", start_epoch - 1, end_epoch + 11)
        cpu = metric_by_pod(name, "web_cpu_ratio", start_epoch - 1, end_epoch + 11)
        pod_throttling = {pod: statistics.fmean(values)
                          for pod, values in throttle.items() if values}
        pod_cpu = {pod: statistics.fmean(values) for pod, values in cpu.items() if values}
        high = max(pod_rates, key=pod_rates.get) if pod_rates else None
        low = min(pod_rates, key=pod_rates.get) if pod_rates else None
        last = info["steps"][-1]
        generator_limited = bool(winning["x"] >= .9 * g)
        saturation_not_reached = bool(last["x"] >= .9 * last["rate"])
        row = {
            "n": n, "rate": winning["rate"], "x": winning["x"],
            "stddev": winning["stddev"], "client_p99_ms": winning["client_p99_ms"],
            "client_2xx_p99_ms": winning["client_2xx_p99_ms"],
            "server_p99_ms": statistics.fmean(
                value for values in server_all.values() for value in values
            ) * 1000 if server_all else None,
            "non_2xx": winning["non_2xx"], "timeouts": winning["timeouts"],
            "connection_errors": winning["connection_errors"],
            "schedule_lag_p95_ms": winning["schedule_lag_p95_ms"],
            "pod_rates": pod_rates, "imbalance": imbalance,
            "mean_throttling": statistics.fmean(pod_throttling.values())
                if pod_throttling else None,
            "max_pod_throttling": max(pod_throttling.values(), default=None),
            "mean_cpu_ratio": statistics.fmean(pod_cpu.values()) if pod_cpu else None,
            "generator_limited": generator_limited,
            "standalone_ceiling_limited": generator_limited,
            "connection_model_noticeable_lag": winning["schedule_lag_p95_ms"] >= 1_500,
            "saturation_not_reached": saturation_not_reached,
            "eligible_for_usl": not generator_limited and not saturation_not_reached,
        }
        table.append(row)
        details[str(n)] = {
            "most_loaded_pod": high,
            "least_loaded_pod": low,
            "server_p99_ms_most": statistics.fmean(server.get(high, [])) * 1000
                if high and server.get(high) else None,
            "server_p99_ms_least": statistics.fmean(server.get(low, [])) * 1000
                if low and server.get(low) else None,
            "throttling_most": pod_throttling.get(high) if high else None,
            "throttling_least": pod_throttling.get(low) if low else None,
            "pod_network_p99_ms": {pod: quantile(values, .99)
                                    for pod, values in pod_latencies.items()},
        }
    x1 = table[0]["x"]
    eligible = [(row["n"], row["x"]) for row in table if row["eligible_for_usl"]]
    model = fit_usl(eligible, x1) if table[0]["eligible_for_usl"] else None
    return table, {"generator_G": g, "pods": details, "usl": model}


def piecewise(name: str, key: str, duration: int, aggregate: str = "first") -> list[float | None]:
    start = summary(name)["started_at_epoch"]
    points: dict[float, list[float]] = defaultdict(list)
    for _labels, epoch, value in metric_rows(name, key):
        points[epoch - start].append(value)
    compact = sorted((offset, statistics.fmean(values) if aggregate == "mean" else values[0])
                     for offset, values in points.items())
    if not compact:
        return [None] * duration
    result = []
    index = 0
    current = compact[0][1]
    for second in range(duration):
        while index + 1 < len(compact) and compact[index + 1][0] <= second + .999:
            index += 1
            current = compact[index][1]
        result.append(current)
    return result


def profile_latency(name: str, start_second: int, end_second: int) -> float | None:
    values = [row["latency_ms"] for row in raw_records(name)
              if start_second * 1000 <= row["completed_offset_ms"] < end_second * 1000]
    return quantile(values, .99)


def demand_for_rate(rate: float, capacities: dict[int, float]) -> int:
    for n in range(1, 6):
        if capacities[n] >= rate:
            return n
    return 5


def elasticity(name: str, capacities: dict[int, float], threshold: float) -> dict[str, Any]:
    seconds = read(DATA / "per_second" / f"{name}.json")
    duration = len(seconds)
    ready = piecewise(name, "ready_replicas", duration)
    desired = piecewise(name, "desired_replicas", duration)
    cpu = piecewise(name, "web_cpu_ratio", duration, aggregate="mean")
    demand = [demand_for_rate(row["planned"], capacities) for row in seconds]
    actual = [int(round(value)) if value is not None else 0 for value in ready]
    under = [s < d for s, d in zip(actual, demand)]
    over = [s > d for s, d in zip(actual, demand)]
    result = {
        "T": duration,
        "theta_U": sum(under) / duration,
        "theta_O": sum(over) / duration,
        "a_U": sum(max(d - s, 0) for s, d in zip(actual, demand)) / duration,
        "a_O": sum(max(s - d, 0) for s, d in zip(actual, demand)) / duration,
        "W_replica_seconds": sum(actual),
        "threshold_p99_ms": threshold,
        "threshold_violation_seconds": sum(
            row["client_p99_ms"] is not None and row["client_p99_ms"] > threshold
            for row in seconds
        ),
        "http_non_2xx": sum(row["http_non_2xx"] for row in seconds),
        "timeouts": sum(row["timeouts"] for row in seconds),
        "connection_errors": sum(row["connection_errors"] for row in seconds),
        "timeline": {
            "planned": [row["planned"] for row in seconds],
            "achieved": [row["completed_2xx"] for row in seconds],
            "p99_ms": [row["client_p99_ms"] for row in seconds],
            "demand": demand, "ready": actual, "desired": desired, "cpu_ratio": cpu,
        },
    }
    return result


def hpa_events(name: str, elasticity_result: dict[str, Any], fixed_spike_p99: float | None) -> dict[str, Any]:
    timeline = elasticity_result["timeline"]
    desired = timeline["desired"]
    ready = timeline["ready"]
    p99 = timeline["p99_ms"]
    jump_start, jump_end = 30, 90

    first_desired = next((s - jump_start for s in range(jump_start, min(jump_end, len(desired)))
                          if desired[s] is not None and desired[s] > 1), None)
    maximum_ready = max(ready)
    maximum_ready_second = next((s for s in range(jump_start, len(ready))
                                 if ready[s] == maximum_ready), None)
    max_ready_time = (
        maximum_ready_second - jump_start
        if maximum_ready_second is not None and maximum_ready_second < jump_end
        else None
    )
    recovery = None
    if fixed_spike_p99 is not None:
        limit = 1.2 * fixed_spike_p99
        for second in range(jump_start, min(jump_end - 9, len(p99) - 9)):
            values = p99[second:second + 10]
            if all(value is not None and value < limit for value in values):
                recovery = second - jump_start
                break
    decrease_search_start = max(jump_end, maximum_ready_second or jump_end)
    scale_down = next((s - jump_end for s in range(decrease_search_start + 1, len(ready))
                       if ready[s] < ready[s - 1]), None)
    return {
        "desired_change_after_jump_seconds": first_desired,
        "maximum_ready_replicas": maximum_ready,
        "maximum_ready_after_jump_seconds": max_ready_time,
        "maximum_ready_observed_at_profile_second": maximum_ready_second,
        "p99_recovery_after_jump_seconds": recovery,
        "first_ready_decrease_after_jump_end_seconds": scale_down,
        "not_reached_representation": None,
    }


def plot_usl(table: list[dict[str, Any]], model: dict[str, Any] | None) -> None:
    lab2 = read(ROOT / "results" / "lab2_final" / "analysis.json")
    compose = lab2["table"]
    compose_model = lab2["usl"]
    fig, ax = plt.subplots(figsize=(8, 5))
    for label, rows, marker in (("Compose", compose, "o"), ("Kubernetes", table, "s")):
        x1 = rows[0]["x"]
        ax.errorbar([row["n"] for row in rows], [row["x"] / x1 for row in rows],
                    yerr=[row["stddev"] / x1 for row in rows], fmt=marker, capsize=4,
                    label=f"{label} measured ± SD")
    xx = np.linspace(1, 5.2, 250)
    if compose_model:
        ax.plot(xx, xx / (1 + compose_model["sigma"] * (xx - 1) +
                          compose_model["kappa"] * xx * (xx - 1)), label="Compose USL")
    if model:
        ax.plot(xx, xx / (1 + model["sigma"] * (xx - 1) +
                          model["kappa"] * xx * (xx - 1)), label="Kubernetes USL")
    ax.plot(xx, xx, "--", color="gray", label="Ideal")
    ax.set(xlabel="Replicas N", ylabel="C(N)=X(N)/X(1)", title="Compose vs Kubernetes scalability")
    ax.set_xticks([1, 2, 3, 4, 5]); ax.grid(alpha=.2); ax.legend(); fig.tight_layout()
    fig.savefig(FIG / "usl_compose_kubernetes.png", dpi=170); plt.close(fig)


def plot_pod_distribution() -> None:
    info = summary("n5")
    winning = max(info["steps"], key=lambda row: row["x"])
    start = winning["measurement_start_offset_ms"]
    end = start + 20_000
    counts: dict[str, list[int]] = defaultdict(lambda: [0] * 20)
    for row in raw_records("n5"):
        if start <= row["completed_offset_ms"] < end and is_success(row) and row.get("pod"):
            second = int((row["completed_offset_ms"] - start) // 1000)
            counts[row["pod"]][second] += 1
    fig, ax = plt.subplots(figsize=(10, 5))
    for pod, values in sorted(counts.items()):
        ax.plot(range(1, 21), values, marker=".", label=pod)
    ax.set(xlabel="Second of X(5) measurement window", ylabel="2xx completions/s",
           title="Per-pod request distribution at X(5)")
    ax.grid(alpha=.2); ax.legend(fontsize=7); fig.tight_layout()
    fig.savefig(FIG / "n5_per_pod_requests.png", dpi=170); plt.close(fig)


def plot_profile_pair(names: tuple[str, str], results: dict[str, dict[str, Any]], prefix: str) -> None:
    max_rate = max(
        max(max(results[name]["timeline"]["planned"]),
            max(results[name]["timeline"]["achieved"]))
        for name in names
    )
    max_p99 = max((value or 0) for name in names for value in results[name]["timeline"]["p99_ms"])
    max_replica = 5.5
    for name in names:
        timeline = results[name]["timeline"]
        t = range(len(timeline["planned"]))
        fig, axes = plt.subplots(4, 1, figsize=(11, 12), sharex=True)
        axes[0].step(t, timeline["planned"], where="post", label="planned")
        axes[0].plot(t, timeline["achieved"], label="2xx completed/s", linewidth=.9)
        axes[0].set_ylim(0, max_rate * 1.15); axes[0].legend()
        axes[1].plot(t, timeline["p99_ms"], linewidth=.9)
        axes[1].set_ylim(0, max(1, max_p99 * 1.05)); axes[1].set_ylabel("p99, ms")
        axes[2].step(t, timeline["demand"], where="post", label="d(t)")
        axes[2].step(t, timeline["ready"], where="post", label="s(t)")
        if any(value is not None for value in timeline["desired"]):
            axes[2].step(t, timeline["desired"], where="post", label="HPA desired", alpha=.7)
        axes[2].set_ylim(0, max_replica); axes[2].legend()
        axes[3].plot(t, timeline["cpu_ratio"], linewidth=.9)
        axes[3].axhline(.8, color="red", linestyle="--", label="HPA target 80%")
        axes[3].set_ylim(0, 2.2); axes[3].set_ylabel("CPU / requests.cpu")
        axes[3].set_xlabel("Seconds from profile start"); axes[3].legend()
        axes[0].set_title(name)
        for axis in axes:
            axis.axvline(30, color="gray", alpha=.4)
            axis.axvline(90, color="gray", alpha=.4)
            axis.grid(alpha=.15)
        fig.tight_layout(); fig.savefig(FIG / f"{prefix}_{name}.png", dpi=170); plt.close(fig)


def write_raw_manifest() -> None:
    """Describe the intentionally untracked primary files without modifying them."""
    rows = []
    for name in RUNS:
        raw = DATA / "raw" / f"{name}.jsonl"
        run_summary = summary(name)
        rows.append({
            "run": name,
            "file": f"raw/{name}.jsonl",
            "bytes": raw.stat().st_size,
            "records": run_summary["raw_records"],
            "sha256": run_summary["raw_sha256"],
            "summary": f"raw/{name}.summary.json",
        })
    manifest = {
        "purpose": "immutable per-request primary data omitted from Git because of size",
        "generated_from": "raw files and their generator summaries",
        "files": rows,
        "reproduce": [
            ".\\.venv\\Scripts\\python.exe scripts\\run_lab3.py --phase stairs",
            ".\\.venv\\Scripts\\python.exe scripts\\run_lab3.py --phase profiles",
            ".\\.venv\\Scripts\\python.exe scripts\\analyze_lab3.py",
            ".\\.venv\\Scripts\\python.exe scripts\\verify_lab3_results.py",
        ],
    }
    (DATA / "raw_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# Первинні raw-дані лабораторної №3",
        "",
        "Файли `*.jsonl` є незмінюваними записами кожної HTTP-спроби й локально виключені",
        "з Git через розмір. Їхні компактні summaries та цей маніфест версіонуються.",
        "",
        "| Прогін | Записів | Розмір, байт | SHA-256 |",
        "| --- | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| `{row['run']}` | {row['records']} | {row['bytes']} | `{row['sha256']}` |"
        )
    lines.extend([
        "",
        "## Відтворення і перевірка",
        "",
        "```powershell",
        *manifest["reproduce"],
        "```",
        "",
        "Незалежна перевірка повторно читає кожен JSONL, звіряє SHA-256 і кількість записів,",
        "перераховує X, SD, pod-розподіл, USL та метрики еластичності.",
        "",
    ])
    (DATA / "raw" / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    for name in RUNS:
        build_per_second(name)
    table, detail = fixed_table()
    model = detail["usl"]
    if model:
        x1 = table[0]["x"]
        model["rmse_x"] = math.sqrt(statistics.fmean(
            (row["x"] - model["predicted_x"][str(row["n"])]) ** 2
            for row in table if row["eligible_for_usl"] and row["n"] > 1
        ))
    capacities = {row["n"]: row["x"] for row in table}
    if model is None:
        raise RuntimeError("USL fit is unavailable; cannot infer X(4) for elasticity")
    capacities[4] = model["predicted_x"]["4"]

    p99_0 = profile_latency("fixed_profile", 0, 30)
    threshold = 2 * p99_0 if p99_0 is not None else math.inf
    elasticity_results = {
        name: elasticity(name, capacities, threshold)
        for name in ("fixed_profile", "hpa30_profile", "d5_hpa30", "d5_hpa300")
    }
    fixed_spike_p99 = profile_latency("fixed_profile", 30, 90)
    events = {
        name: hpa_events(name, elasticity_results[name], fixed_spike_p99)
        for name in ("hpa30_profile", "d5_hpa30", "d5_hpa300")
    }
    analysis = {
        "definitions": {
            "X": "mean 2xx completions/s in the last 20 seconds of a 30-second step",
            "client_latency": "scheduled timestamp through response/error",
            "generator_limited": "X(N) >= 0.9 of standalone noop ceiling G",
            "connection_model_noticeable_lag": "schedule-lag p95 >= 5% of a 30-second step; disclosed as a method limitation but not an assignment rule for excluding N from USL",
            "throttling": "rate(throttled_periods)/rate(periods) over matching 20-second window",
            "cadvisor_sampling": "per-pod means from range-query samples in [window start-1s, window end+11s] because cAdvisor counter refresh can yield NaN at an exact 5s timestamp",
            "imbalance": "max per-pod successful completions/s divided by pod mean",
        },
        "generator_G": detail["generator_G"],
        "fixed_table": table,
        "pod_details": detail["pods"],
        "usl": model,
        "capacities_for_elasticity": capacities,
        "p99_0_ms": p99_0,
        "fixed_spike_p99_ms": fixed_spike_p99,
        "elasticity": elasticity_results,
        "hpa_events": events,
    }
    (DATA / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    plot_usl(table, model)
    plot_pod_distribution()
    plot_profile_pair(("fixed_profile", "hpa30_profile"), elasticity_results, "short")
    plot_profile_pair(("d5_hpa30", "d5_hpa300"), elasticity_results, "d5")
    write_raw_manifest()
    print(json.dumps({"generator_G": detail["generator_G"], "table": table,
                      "usl": model, "elasticity": {k: {x: v for x, v in value.items()
                      if x != "timeline"} for k, value in elasticity_results.items()},
                      "events": events}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
