from __future__ import annotations

import argparse
import http.client
import json
import math
import multiprocessing
import os
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from queue import Empty
from typing import Any
from urllib.parse import urlsplit


_thread_state = threading.local()


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def connection_for(base_url: str, timeout: float) -> http.client.HTTPConnection:
    parsed = urlsplit(base_url)
    key = (parsed.scheme, parsed.hostname, parsed.port, timeout)
    cached_key = getattr(_thread_state, "connection_key", None)
    connection = getattr(_thread_state, "connection", None)

    if connection is not None and cached_key == key:
        return connection

    if connection is not None:
        connection.close()

    host = parsed.hostname or "localhost"
    if parsed.scheme == "https":
        connection = http.client.HTTPSConnection(
            host,
            parsed.port or 443,
            timeout=timeout,
        )
    else:
        connection = http.client.HTTPConnection(
            host,
            parsed.port or 80,
            timeout=timeout,
        )

    _thread_state.connection_key = key
    _thread_state.connection = connection
    return connection


def reset_connection() -> None:
    connection = getattr(_thread_state, "connection", None)
    if connection is not None:
        connection.close()
    _thread_state.connection = None
    _thread_state.connection_key = None


def measured_get(
    base_url: str,
    path: str,
    timeout: float,
    scheduled_at: float,
) -> dict[str, Any]:
    sent_at = time.perf_counter()
    status: int | None = None
    pod: str | None = None
    error: str | None = None

    try:
        connection = connection_for(base_url, timeout)
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        response.read()
        status = response.status
        pod = response.getheader("X-Pod")
    except Exception as exception:
        error = f"{type(exception).__name__}: {exception}"
        reset_connection()

    completed_at = time.perf_counter()
    return {
        "scheduled_at": scheduled_at,
        "sent_at": sent_at,
        "completed_at": completed_at,
        "latency_ms": (completed_at - sent_at) * 1000,
        "schedule_lag_ms": max(0.0, (sent_at - scheduled_at) * 1000),
        "status": status,
        "pod": pod,
        "error": error,
    }


def open_process(
    base_url: str,
    path_template: str,
    ids: list[int],
    rate: float,
    duration: float,
    timeout: float,
    start_at: float,
    process_index: int,
    process_count: int,
    threads_per_process: int,
    result_queue,
) -> None:
    total_requests = max(1, math.floor(rate * duration))
    futures = []

    try:
        with ThreadPoolExecutor(max_workers=threads_per_process) as executor:
            for request_index in range(process_index, total_requests, process_count):
                scheduled_at = start_at + request_index / rate
                delay = scheduled_at - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)

                path = path_template
                if ids:
                    path = path_template.format(id=ids[request_index % len(ids)])
                futures.append(
                    executor.submit(
                        measured_get,
                        base_url,
                        path,
                        timeout,
                        scheduled_at,
                    )
                )

            records = [future.result() for future in futures]
        result_queue.put({"records": records})
    except Exception as exception:
        result_queue.put(
            {"error": f"Процес {process_index}: {type(exception).__name__}: {exception}"}
        )


def summarize_records(
    records: list[dict[str, Any]],
    mode: str,
    started_at: float,
    nominal_duration: float,
    requested_rate: float | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    if not records:
        raise RuntimeError("Генератор не виконав жодного запиту")

    completed_at = max(record["completed_at"] for record in records)
    sent_at = max(record["sent_at"] for record in records)
    observation_seconds = max(nominal_duration, completed_at - started_at)
    dispatch_seconds = max(nominal_duration, sent_at - started_at)
    latencies = [record["latency_ms"] for record in records]
    schedule_lags = [record["schedule_lag_ms"] for record in records]
    bad_responses = sum(
        1
        for record in records
        if record["status"] is None or not 200 <= record["status"] < 300
    )
    successful = len(records) - bad_responses
    status_counts = Counter(
        "exception" if record["status"] is None else str(record["status"])
        for record in records
    )
    pod_counts = Counter(record["pod"] for record in records if record["pod"])
    total_response_seconds = sum(latencies) / 1000

    samples = [
        {
            "sent_offset_ms": round((record["sent_at"] - started_at) * 1000, 3),
            "latency_ms": round(record["latency_ms"], 3),
            "schedule_lag_ms": round(record["schedule_lag_ms"], 3),
            "status": record["status"],
            "pod": record["pod"],
            "error": record["error"],
        }
        for record in records
    ]

    result: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "requested_rate_rps": requested_rate,
        "nominal_duration_seconds": nominal_duration,
        "observation_seconds": round(observation_seconds, 6),
        "requests_scheduled": len(records),
        "responses_2xx": successful,
        "responses_non_2xx": bad_responses,
        "error_rate": round(bad_responses / len(records), 8),
        "achieved_rate_rps": round(len(records) / observation_seconds, 6),
        "successful_rate_rps": round(successful / observation_seconds, 6),
        "dispatch_rate_rps": round(len(records) / dispatch_seconds, 6),
        "latency_mean_ms": round(statistics.fmean(latencies), 6),
        "latency_p50_ms": round(percentile(latencies, 0.50), 6),
        "latency_p95_ms": round(percentile(latencies, 0.95), 6),
        "latency_p99_ms": round(percentile(latencies, 0.99), 6),
        "schedule_lag_p95_ms": round(percentile(schedule_lags, 0.95), 6),
        "average_in_flight": round(total_response_seconds / observation_seconds, 6),
        "status_counts": dict(sorted(status_counts.items())),
        "pod_counts": dict(sorted(pod_counts.items())),
        "samples": samples,
    }
    result.update(extra)
    return result


def run_open(args: argparse.Namespace) -> dict[str, Any]:
    ids = load_ids(args.seed_file) if args.seed_file else []
    cpu_count = os.cpu_count() or 1
    process_count = args.processes or min(cpu_count, max(1, math.ceil(args.rate)))
    process_count = max(1, process_count)
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    start_at = time.perf_counter() + 2.0
    processes = []

    for process_index in range(process_count):
        process = context.Process(
            target=open_process,
            args=(
                args.base_url,
                args.path,
                ids,
                args.rate,
                args.duration,
                args.timeout,
                start_at,
                process_index,
                process_count,
                args.threads_per_process,
                result_queue,
            ),
        )
        process.start()
        processes.append(process)

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    queue_timeout = args.duration + args.timeout + 60
    for _ in processes:
        try:
            process_result = result_queue.get(timeout=queue_timeout)
        except Empty as exception:
            raise RuntimeError("Один із процесів генератора не повернув результат") from exception
        if "error" in process_result:
            errors.append(process_result["error"])
        else:
            records.extend(process_result["records"])

    for process in processes:
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join()

    if errors:
        raise RuntimeError("; ".join(errors))

    return summarize_records(
        records=records,
        mode="open",
        started_at=start_at,
        nominal_duration=args.duration,
        requested_rate=args.rate,
        extra={
            "processes": process_count,
            "threads_per_process": args.threads_per_process,
            "path_template": args.path,
        },
    )


def closed_worker(
    base_url: str,
    path_template: str,
    ids: list[int],
    timeout: float,
    start_at: float,
    end_at: float,
    worker_index: int,
) -> list[dict[str, Any]]:
    records = []
    request_index = worker_index
    while time.perf_counter() < start_at:
        time.sleep(min(0.01, start_at - time.perf_counter()))

    while time.perf_counter() < end_at:
        path = path_template
        if ids:
            path = path_template.format(id=ids[request_index % len(ids)])
        scheduled_at = time.perf_counter()
        records.append(measured_get(base_url, path, timeout, scheduled_at))
        request_index += 1
    return records


def run_closed(args: argparse.Namespace) -> dict[str, Any]:
    ids = load_ids(args.seed_file) if args.seed_file else []
    start_at = time.perf_counter() + 1.0
    end_at = start_at + args.duration
    records: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                closed_worker,
                args.base_url,
                args.path,
                ids,
                args.timeout,
                start_at,
                end_at,
                worker_index,
            )
            for worker_index in range(args.concurrency)
        ]
        for future in futures:
            records.extend(future.result())

    return summarize_records(
        records=records,
        mode="closed",
        started_at=start_at,
        nominal_duration=args.duration,
        requested_rate=None,
        extra={
            "concurrency": args.concurrency,
            "path_template": args.path,
        },
    )


def json_request(
    method: str,
    base_url: str,
    path: str,
    timeout: float,
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    parsed = urlsplit(base_url)
    connection_class = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(
        parsed.hostname or "localhost",
        parsed.port or (443 if parsed.scheme == "https" else 80),
        timeout=timeout,
    )
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw_body = response.read()
        parsed_body = json.loads(raw_body) if raw_body else None
        return response.status, parsed_body
    finally:
        connection.close()


def run_seed(args: argparse.Namespace) -> dict[str, Any]:
    ids = []
    for index in range(1, args.count + 1):
        payload = {
            "patient_code": f"BENCH-{index:04d}",
            "modality": ("CT", "MRI", "XR", "US", "PET")[(index - 1) % 5],
            "priority": (index - 1) % 5 + 1,
            "doctor": f"Тестовий лікар {index:03d}",
            "issued_at": date.today().isoformat(),
            "status": "created",
        }
        status, body = json_request(
            "POST",
            args.base_url,
            "/referrals",
            args.timeout,
            payload,
        )
        if status != 201 or not isinstance(body, dict) or not isinstance(body.get("id"), int):
            raise RuntimeError(f"Не вдалося створити тестовий запис {index}: HTTP {status}")
        ids.append(body["id"])

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "count": len(ids),
        "ids": ids,
    }


def load_ids(seed_file: str) -> list[int]:
    data = json.loads(Path(seed_file).read_text(encoding="utf-8"))
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(item, int) for item in ids):
        raise ValueError("Файл тестових записів не містить коректного масиву ids")
    return ids


def run_cleanup(args: argparse.Namespace) -> dict[str, Any]:
    ids = load_ids(args.seed_file)
    removed = 0
    missing = 0
    for referral_id in ids:
        status, _ = json_request(
            "DELETE",
            args.base_url,
            f"/referrals/{referral_id}",
            args.timeout,
        )
        if status == 204:
            removed += 1
        elif status == 404:
            missing += 1
        else:
            raise RuntimeError(f"Видалення запису {referral_id} повернуло HTTP {status}")
    return {"removed": removed, "already_missing": missing}


def emit_result(result: dict[str, Any], output: str | None) -> None:
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    compact = {key: value for key, value in result.items() if key != "samples"}
    print(json.dumps(compact, ensure_ascii=False, indent=2), flush=True)


def add_request_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default="http://proxy")
    parser.add_argument("--path", default="/referrals/{id}")
    parser.add_argument("--seed-file")
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Генератор навантаження для лабораторної роботи 1"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    open_parser = subparsers.add_parser("open", help="Відкритий контур")
    add_request_arguments(open_parser)
    open_parser.add_argument("--rate", type=float, required=True)
    open_parser.add_argument("--processes", type=int, default=0)
    open_parser.add_argument("--threads-per-process", type=int, default=16)

    closed_parser = subparsers.add_parser("closed", help="Закритий контур")
    add_request_arguments(closed_parser)
    closed_parser.add_argument("--concurrency", type=int, required=True)

    seed_parser = subparsers.add_parser("seed", help="Створити тестові записи")
    seed_parser.add_argument("--base-url", default="http://proxy")
    seed_parser.add_argument("--count", type=int, default=100)
    seed_parser.add_argument("--timeout", type=float, default=10.0)
    seed_parser.add_argument("--output", required=True)

    cleanup_parser = subparsers.add_parser("cleanup", help="Видалити тестові записи")
    cleanup_parser.add_argument("--base-url", default="http://proxy")
    cleanup_parser.add_argument("--seed-file", required=True)
    cleanup_parser.add_argument("--timeout", type=float, default=10.0)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "open":
        result = run_open(args)
    elif args.command == "closed":
        result = run_closed(args)
    elif args.command == "seed":
        result = run_seed(args)
    else:
        result = run_cleanup(args)
    emit_result(result, getattr(args, "output", None))


if __name__ == "__main__":
    main()
