from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


OPEN_FILE_PATTERN = re.compile(
    r"open-r(?P<instances>\d+)-rate(?P<rate>\d+)-rep(?P<repeat>\d+)\.json"
)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rounded_median(values: list[float]) -> float:
    return round(statistics.median(values), 6)


def successful_throughput(run: dict[str, Any]) -> float:
    if "successful_rate_rps" in run:
        return float(run["successful_rate_rps"])
    return float(run["responses_2xx"]) / float(run["observation_seconds"])


def attempted_throughput(run: dict[str, Any]) -> float:
    if "attempted_completion_rate_rps" in run:
        return float(run["attempted_completion_rate_rps"])
    if int(run.get("schema_version", 1)) == 1 and "achieved_rate_rps" in run:
        return float(run["achieved_rate_rps"])
    return float(run["requests_scheduled"]) / float(run["observation_seconds"])


def latency_origin(run: dict[str, Any]) -> str:
    return str(run.get("latency_origin", "sent_at"))


def open_runs(results_directory: Path) -> dict[tuple[int, int], list[dict[str, Any]]]:
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for path in sorted((results_directory / "raw").glob("open-r*-rate*-rep*.json")):
        match = OPEN_FILE_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        data = read_json(path)
        data["source_file"] = path.name
        key = (int(match.group("instances")), int(match.group("rate")))
        grouped[key].append(data)
    return grouped


def summarize_open_runs(results_directory: Path) -> list[dict[str, Any]]:
    grouped = open_runs(results_directory)

    if not grouped:
        raise RuntimeError("Не знайдено сирих результатів відкритого контуру")

    fields = (
        "latency_mean_ms",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "error_rate",
        "average_in_flight",
        "schedule_lag_p95_ms",
    )
    rows = []
    for (instances, requested_rate), runs in sorted(grouped.items()):
        origins = {latency_origin(run) for run in runs}
        if len(origins) != 1:
            raise ValueError("Не можна змішувати різні методики затримки в одній медіані")
        successful_rate = rounded_median([successful_throughput(run) for run in runs])
        measurements = {run.get("in_flight_measurement", "derived_from_latency_sum") for run in runs}
        if len(measurements) != 1:
            raise ValueError("Не можна змішувати різні методики вимірювання L в одній медіані")
        row: dict[str, Any] = {
            "instances": instances,
            "requested_rate_rps": requested_rate,
            "repeat_count": len(runs),
            "achieved_rate_rps": successful_rate,
            "successful_rate_rps": successful_rate,
            "attempted_completion_rate_rps": rounded_median(
                [attempted_throughput(run) for run in runs]
            ),
            "latency_origin": origins.pop(),
            "in_flight_measurement": measurements.pop(),
            "source_files": [run["source_file"] for run in runs],
        }
        for field in fields:
            row[field] = rounded_median([float(run[field]) for run in runs])
        rows.append(row)
    return rows


def find_row(
    rows: list[dict[str, Any]],
    instances: int,
    rate: int,
) -> dict[str, Any]:
    return next(
        row
        for row in rows
        if row["instances"] == instances and row["requested_rate_rps"] == rate
    )


def scaling_calculations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rates = sorted(
        set(row["requested_rate_rps"] for row in rows if row["instances"] == 1)
    )
    calculations = []
    for rate in rates:
        one = find_row(rows, 1, rate)
        two = find_row(rows, 2, rate)
        speedup = two["achieved_rate_rps"] / one["achieved_rate_rps"]
        karp_flatt = (1 / speedup - 0.5) / 0.5
        calculations.append(
            {
                "requested_rate_rps": rate,
                "throughput_one_rps": round(one["achieved_rate_rps"], 6),
                "throughput_two_rps": round(two["achieved_rate_rps"], 6),
                "speedup": round(speedup, 6),
                "karp_flatt": round(karp_flatt, 6),
            }
        )
    return calculations


def little_calculation(results_directory: Path) -> dict[str, Any]:
    runs = open_runs(results_directory)[(1, 100)]
    checks = []
    for run in runs:
        independent = (
            int(run.get("schema_version", 1)) >= 2
            and run.get("in_flight_measurement") == "independent_periodic_counter_samples"
            and latency_origin(run) == "scheduled_at"
            and "average_successful_in_flight" in run
            and "successful_latency_mean_ms" in run
        )
        response_seconds = float(
            run["successful_latency_mean_ms"] if independent else run["latency_mean_ms"]
        ) / 1000
        calculated = successful_throughput(run) * response_seconds
        measured = float(run["average_successful_in_flight"]) if independent else None
        discrepancy = (
            abs(calculated - measured) / measured * 100
            if measured is not None and measured > 0
            else None
        )
        checks.append(
            {
                "source_file": run["source_file"],
                "independent_verification": independent,
                "latency_origin": latency_origin(run),
                "cohort": "responses_2xx",
                "lambda_rps": round(successful_throughput(run), 6),
                "mean_response_seconds": round(response_seconds, 9),
                "lambda_times_w": round(calculated, 6),
                "measured_average_in_flight": round(measured, 6) if measured is not None else None,
                "discrepancy_percent": round(discrepancy, 6) if discrepancy is not None else None,
            }
        )
    representative = sorted(checks, key=lambda check: check["mean_response_seconds"])[len(checks) // 2]
    result = dict(representative)
    result["repeat_checks"] = checks
    if not result["independent_verification"]:
        result["reason"] = (
            "У старих файлах L обчислено з тієї самої суми затримок, що й W. "
            "Це алгебраїчна тотожність, а не незалежна перевірка закону Літтла; "
            "незалежне L і розбіжність не визначено."
        )
    return result


def saturation_choice(rows: list[dict[str, Any]]) -> dict[str, Any]:
    one_instance_rows = sorted(
        (row for row in rows if row["instances"] == 1),
        key=lambda row: row["requested_rate_rps"],
    )
    overloaded = [
        row
        for row in one_instance_rows
        if row["achieved_rate_rps"] < row["requested_rate_rps"] * 0.95
    ]
    selected = overloaded[0] if overloaded else one_instance_rows[-1]
    return {
        "rate_rps": selected["requested_rate_rps"],
        "concurrency": max(1, round(selected["average_in_flight"])),
        "saturation_observed": bool(overloaded),
        "open_achieved_rate_rps": selected["achieved_rate_rps"],
        "open_p95_ms": selected["latency_p95_ms"],
    }


def aggregate_pods(results_directory: Path) -> dict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    for path in sorted((results_directory / "raw").glob("open-r2-rate*-rep*.json")):
        for pod, count in read_json(path).get("pod_counts", {}).items():
            counts[pod] += int(count)
    return dict(sorted(counts.items()))


def optional_json(path: Path) -> dict[str, Any] | None:
    return read_json(path) if path.exists() else None


def optional_compact_json(path: Path) -> dict[str, Any] | None:
    data = optional_json(path)
    if data is None:
        return None
    data.pop("samples", None)
    data.pop("in_flight_samples", None)
    if "responses_2xx" in data:
        data["attempted_completion_rate_rps"] = round(attempted_throughput(data), 6)
        data["achieved_rate_rps"] = round(successful_throughput(data), 6)
        data["latency_origin"] = latency_origin(data)
    return data


def analyze(results_directory: Path) -> dict[str, Any]:
    rows = summarize_open_runs(results_directory)
    selected = saturation_choice(rows)
    closed = optional_json(results_directory / "raw" / "closed-loop.json")
    closed_comparison = None
    if closed is not None:
        closed_comparison = {
            "rate_rps": selected["rate_rps"],
            "concurrency": closed["concurrency"],
            "open_achieved_rate_rps": selected["open_achieved_rate_rps"],
            "open_p95_ms": selected["open_p95_ms"],
            "closed_achieved_rate_rps": round(successful_throughput(closed), 6),
            "closed_p95_ms": closed["latency_p95_ms"],
            "closed_attempted_completion_rate_rps": round(attempted_throughput(closed), 6),
            "closed_latency_origin": latency_origin(closed),
            "open_latency_origin": find_row(rows, 1, selected["rate_rps"])["latency_origin"],
            "closed_minus_open_successful_rate_rps": round(
                successful_throughput(closed) - selected["open_achieved_rate_rps"], 6
            ),
        }

    return {
        "measurement_table": rows,
        "scaling": scaling_calculations(rows),
        "little": little_calculation(results_directory),
        "closed_test_selection": selected,
        "closed_comparison": closed_comparison,
        "pod_counts_two_instances": aggregate_pods(results_directory),
        "startup": optional_json(results_directory / "startup_times.json"),
        "image": optional_json(results_directory / "image_size.json"),
        "generator_noop": optional_compact_json(
            results_directory / "raw" / "generator-noop.json"
        ),
        "network_check": optional_json(results_directory / "network_check.json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Аналіз результатів навантаження")
    parser.add_argument("--results", default="/results")
    args = parser.parse_args()
    results_directory = Path(args.results)
    summary = analyze(results_directory)
    output_path = results_directory / "summary.json"
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
