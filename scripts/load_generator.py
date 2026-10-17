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
_connections: dict[int, http.client.HTTPConnection] = {}
_connections_lock = threading.Lock()


def close_connections() -> None:
    with _connections_lock:
        connections = list(_connections.values())
        _connections.clear()
    for connection in connections:
        connection.close()


class RunCounters:
    def __init__(self, shared, slots: int):
        self.shared = shared
        self.slots = slots

    def schedule(self, slot: int) -> tuple[int, float]:
        with self.shared.get_lock():
            request_index = sum(self.shared[index * 5] for index in range(self.slots))
            scheduled_at = time.perf_counter()
            self.shared[slot * 5] += 1
            return request_index, scheduled_at

    def dispatch(self, slot: int) -> None:
        with self.shared.get_lock():
            offset = slot * 5
            self.shared[offset + 1] += 1
            active = self.shared[offset + 1] - self.shared[offset + 2]
            self.shared[offset + 4] = max(self.shared[offset + 4], active)

    def complete(self, slot: int, successful: bool) -> None:
        with self.shared.get_lock():
            self.shared[slot * 5 + 2] += 1
            self.shared[slot * 5 + 3] += int(successful)

    def snapshot(self) -> tuple[int, int, int, int]:
        with self.shared.get_lock():
            return tuple(
                sum(self.shared[index * 5 + field] for index in range(self.slots))
                for field in range(4)
            )

    def peaks(self) -> list[int]:
        with self.shared.get_lock():
            return [self.shared[index * 5 + 4] for index in range(self.slots)]


class InFlightSampler:
    def __init__(
        self,
        counters: RunCounters,
        started_at: float,
        duration: float,
        interval: float,
        rate: float | None = None,
        total_requests: int | None = None,
    ):
        self.counters = counters
        self.started_at = started_at
        self.duration = duration
        self.interval = interval
        self.rate = rate
        self.total_requests = total_requests
        self.samples: list[dict[str, Any]] = []
        self.stop_event = threading.Event()
        self.generation_done = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def run(self) -> None:
        if self.stop_event.wait(max(0.0, self.started_at - time.perf_counter())):
            return
        self.samples.append(
            {
                "sampled_at": self.started_at,
                "scheduled": 0,
                "dispatched": 0,
                "completed": 0,
                "completed_successful": 0,
            }
        )
        next_sample = self.started_at + self.interval
        while not self.stop_event.wait(max(0.0, next_sample - time.perf_counter())):
            with self.counters.shared.get_lock():
                sampled_at = time.perf_counter()
                scheduled, dispatched, completed, completed_successful = self.counters.snapshot()
            if self.rate is not None and self.total_requests is not None:
                scheduled = min(
                    self.total_requests,
                    max(0, math.floor((sampled_at - self.started_at) * self.rate) + 1),
                )
            self.samples.append(
                {
                    "sampled_at": sampled_at,
                    "scheduled": scheduled,
                    "dispatched": dispatched,
                    "completed": completed,
                    "completed_successful": completed_successful,
                }
            )
            if (
                sampled_at >= self.started_at + self.duration
                and completed >= scheduled
                and (self.rate is not None or self.generation_done.is_set())
            ):
                break
            next_sample = max(next_sample + self.interval, sampled_at + self.interval)

    def finish(self) -> list[dict[str, Any]]:
        self.generation_done.set()
        self.thread.join()
        return self.samples

    def abort(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)


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
    with _connections_lock:
        _connections[threading.get_ident()] = connection
    return connection


def reset_connection() -> None:
    connection = getattr(_thread_state, "connection", None)
    if connection is not None:
        connection.close()
    _thread_state.connection = None
    _thread_state.connection_key = None
    with _connections_lock:
        _connections.pop(threading.get_ident(), None)


def measured_get(
    base_url: str,
    path: str,
    timeout: float,
    scheduled_at: float,
    request_index: int,
    counters: RunCounters,
    slot: int,
) -> dict[str, Any]:
    sent_at = time.perf_counter()
    counters.dispatch(slot)
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
    counters.complete(slot, status is not None and 200 <= status < 300)
    return {
        "request_index": request_index,
        "scheduled_at": scheduled_at,
        "sent_at": sent_at,
        "completed_at": completed_at,
        "latency_ms": (completed_at - scheduled_at) * 1000,
        "network_latency_ms": (completed_at - sent_at) * 1000,
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
    start_clock,
    start_event,
    ready_queue,
    process_index: int,
    process_count: int,
    threads_per_process: int,
    result_queue,
    counters: RunCounters,
) -> None:
    total_requests = max(1, math.floor(rate * duration))
    futures = []

    try:
        with ThreadPoolExecutor(max_workers=threads_per_process) as executor:
            ready_queue.put(process_index)
            start_event.wait()
            start_at = start_clock.value
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
                        request_index,
                        counters,
                        process_index,
                    )
                )

            records = [future.result() for future in futures]
        result_queue.put({"records": records})
    except Exception as exception:
        result_queue.put(
            {"error": f"Процес {process_index}: {type(exception).__name__}: {exception}"}
        )
    finally:
        close_connections()


def summarize_records(
    records: list[dict[str, Any]],
    mode: str,
    started_at: float,
    nominal_duration: float,
    requested_rate: float | None,
    extra: dict[str, Any],
    occupancy_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    if not records:
        raise RuntimeError("Генератор не виконав жодного запиту")

    completed_at = max(record["completed_at"] for record in records)
    sent_at = max(record["sent_at"] for record in records)
    observation_end = max(started_at + nominal_duration, completed_at)
    observation_seconds = observation_end - started_at
    dispatch_seconds = max(nominal_duration, sent_at - started_at)
    latencies = [record["latency_ms"] for record in records]
    network_latencies = [record["network_latency_ms"] for record in records]
    schedule_lags = [record["schedule_lag_ms"] for record in records]
    bad_responses = sum(
        1
        for record in records
        if record["status"] is None or not 200 <= record["status"] < 300
    )
    successful = len(records) - bad_responses
    occupancy_samples = [
        sample for sample in occupancy_samples if sample["sampled_at"] < observation_end
    ]
    occupancy_samples.append(
        {
            "sampled_at": observation_end,
            "scheduled": len(records),
            "dispatched": len(records),
            "completed": len(records),
            "completed_successful": successful,
        }
    )
    successful_latencies = [
        record["latency_ms"]
        for record in records
        if record["status"] is not None and 200 <= record["status"] < 300
    ]
    status_counts = Counter(
        "exception" if record["status"] is None else str(record["status"])
        for record in records
    )
    pod_counts = Counter(
        record["pod"]
        for record in records
        if record["pod"] and record["status"] is not None and 200 <= record["status"] < 300
    )
    error_counts = Counter(
        record["error"].split(":", 1)[0] for record in records if record["error"]
    )
    successful_prefix = [0] * (len(records) + 1)
    for record in sorted(records, key=lambda item: item["request_index"]):
        index = record["request_index"]
        successful_prefix[index + 1] = successful_prefix[index] + int(
            record["status"] is not None and 200 <= record["status"] < 300
        )
    in_flight_samples = []
    for sample in occupancy_samples:
        scheduled = min(len(records), sample["scheduled"])
        in_flight_samples.append(
            {
                "offset_ms": (sample["sampled_at"] - started_at) * 1000,
                "scheduled": scheduled,
                "dispatched": sample["dispatched"],
                "completed": sample["completed"],
                "completed_successful": sample["completed_successful"],
                "in_flight": max(0, scheduled - sample["completed"]),
                "successful_in_flight": max(
                    0, successful_prefix[scheduled] - sample["completed_successful"]
                ),
            }
        )
    def sampled_average(field: str) -> float:
        return sum(
            (after["offset_ms"] - before["offset_ms"]) / 1000
            * (before[field] + after[field]) / 2
            for before, after in zip(in_flight_samples, in_flight_samples[1:])
        ) / observation_seconds

    sampling_gaps = [
        after["offset_ms"] - before["offset_ms"]
        for before, after in zip(in_flight_samples, in_flight_samples[1:])
    ]

    samples = [
        {
            "request_index": record["request_index"],
            "scheduled_offset_ms": round((record["scheduled_at"] - started_at) * 1000, 3),
            "sent_offset_ms": round((record["sent_at"] - started_at) * 1000, 3),
            "completed_offset_ms": round((record["completed_at"] - started_at) * 1000, 3),
            "latency_ms": round(record["latency_ms"], 3),
            "network_latency_ms": round(record["network_latency_ms"], 3),
            "schedule_lag_ms": round(record["schedule_lag_ms"], 3),
            "status": record["status"],
            "pod": record["pod"],
            "error": record["error"],
        }
        for record in records
    ]

    result: dict[str, Any] = {
        "schema_version": 2,
        "generator_implementation": "multiprocessing_thread_pools",
        "latency_origin": "scheduled_at",
        "latency_basis": "scheduled_at_to_completed_at_including_generator_queue",
        "network_latency_origin": "sent_at",
        "in_flight_measurement": "independent_periodic_counter_samples",
        "in_flight_sample_interval_ms": extra.pop("sample_interval_ms"),
        "in_flight_sample_gap_max_ms": round(max(sampling_gaps, default=0), 6),
        "in_flight_sample_count": len(in_flight_samples),
        "in_flight_integration": "trapezoidal_over_actual_sample_timestamps",
        "in_flight_terminal_boundary": "zero_pending_after_last_completion; samples_after_observation_end_excluded",
        "successful_in_flight_classification": "sampled_scheduled_success_cohort_minus_sampled_successful_completions",
        "observation_window": "planned_start_through_max(nominal_end,last_completion); excludes_result_collection",
        "finite_window_note": "Початково порожня система, завершальний drain і дискретизація впливають на скінченну вибірку; це не доказ стаціонарності.",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "requested_rate_rps": requested_rate,
        "nominal_duration_seconds": nominal_duration,
        "observation_seconds": round(observation_seconds, 6),
        "completion_span_seconds": round(max(nominal_duration, completed_at - started_at), 6),
        "drain_seconds": round(max(0.0, completed_at - started_at - nominal_duration), 6),
        "requests_scheduled": len(records),
        "requests_sent": len(records),
        "requests_completed": len(records),
        "responses_2xx": successful,
        "responses_non_2xx": sum(1 for record in records if record["status"] is not None and not 200 <= record["status"] < 300),
        "request_exceptions": sum(1 for record in records if record["status"] is None),
        "requests_failed": bad_responses,
        "error_rate": round(bad_responses / len(records), 8),
        "achieved_rate_rps": round(successful / observation_seconds, 6),
        "successful_rate_rps": round(successful / observation_seconds, 6),
        "attempted_completion_rate_rps": round(len(records) / observation_seconds, 6),
        "dispatch_rate_rps": round(len(records) / dispatch_seconds, 6),
        "latency_mean_ms": round(statistics.fmean(latencies), 6),
        "latency_p50_ms": round(percentile(latencies, 0.50), 6),
        "latency_p95_ms": round(percentile(latencies, 0.95), 6),
        "latency_p99_ms": round(percentile(latencies, 0.99), 6),
        "successful_latency_mean_ms": round(statistics.fmean(successful_latencies), 6) if successful_latencies else None,
        "network_latency_mean_ms": round(statistics.fmean(network_latencies), 6),
        "network_latency_p95_ms": round(percentile(network_latencies, 0.95), 6),
        "schedule_lag_p95_ms": round(percentile(schedule_lags, 0.95), 6),
        "average_in_flight": round(sampled_average("in_flight"), 6),
        "average_successful_in_flight": round(sampled_average("successful_in_flight"), 6),
        "peak_in_flight": max(sample["in_flight"] for sample in in_flight_samples),
        "peak_generator_queued": max(sample["scheduled"] - sample["dispatched"] for sample in in_flight_samples),
        "error_counts": dict(sorted(error_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "pod_counts": dict(sorted(pod_counts.items())),
        "samples": samples,
        "in_flight_samples": in_flight_samples,
    }
    result.update(extra)
    return result


def run_open(args: argparse.Namespace) -> dict[str, Any]:
    ids = load_ids(args.seed_file) if args.seed_file else []
    cpu_count = os.cpu_count() or 1
    process_count = args.processes or min(cpu_count, max(1, math.ceil(args.rate)))
    process_count = max(1, process_count)
    threads_per_process = args.threads_per_process or max(
        16, math.ceil(args.rate * args.timeout * 1.25 / process_count)
    )
    context = multiprocessing.get_context(
        "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    )
    result_queue = context.Queue()
    ready_queue = context.Queue()
    start_event = context.Event()
    start_clock = context.Value("d", 0.0)
    counters = RunCounters(context.Array("q", process_count * 5), process_count)
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
                start_clock,
                start_event,
                ready_queue,
                process_index,
                process_count,
                threads_per_process,
                result_queue,
                counters,
            ),
        )
        process.start()
        processes.append(process)

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    sampler = None
    occupancy_samples = []
    try:
        for _ in processes:
            ready_queue.get(timeout=60)
        start_at = time.perf_counter() + 1.0
        sampler = InFlightSampler(
            counters, start_at, args.duration, args.sample_interval_ms / 1000,
            args.rate, max(1, math.floor(args.rate * args.duration)),
        )
        start_clock.value = start_at
        sampler.start()
        start_event.set()
        queue_timeout = args.duration + args.timeout * 2 + 60
        for _ in processes:
            process_result = result_queue.get(timeout=queue_timeout)
            if "error" in process_result:
                errors.append(process_result["error"])
            else:
                records.extend(process_result["records"])
        if errors:
            raise RuntimeError("; ".join(errors))
        occupancy_samples = sampler.finish()
    except Empty as exception:
        raise RuntimeError("Один із процесів генератора не повернув результат") from exception
    finally:
        if sampler is not None:
            sampler.abort()
        start_event.set()
        for process in processes:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join()

    worker_peaks = counters.peaks()
    worker_capacity = threads_per_process * process_count
    if counters.snapshot()[1] != len(records):
        raise RuntimeError("Кількість фактичних відправлень не збігається з журналом запитів")

    return summarize_records(
        records=records,
        mode="open",
        started_at=start_at,
        nominal_duration=args.duration,
        requested_rate=args.rate,
        extra={
            "processes": process_count,
            "threads_per_process": threads_per_process,
            "thread_pool_sizing": "explicit" if args.threads_per_process else "ceil(rate*timeout*1.25/processes), minimum 16",
            "worker_capacity": worker_capacity,
            "worker_peak_active_by_process": worker_peaks,
            "worker_capacity_saturated": any(peak >= threads_per_process for peak in worker_peaks),
            "worker_capacity_covers_rate_times_timeout": worker_capacity >= args.rate * args.timeout,
            "sample_interval_ms": args.sample_interval_ms,
            "path_template": args.path,
        },
        occupancy_samples=occupancy_samples,
    )


def closed_worker(
    base_url: str,
    path_template: str,
    ids: list[int],
    timeout: float,
    start_at: float,
    end_at: float,
    worker_index: int,
    counters: RunCounters,
    process_index: int,
) -> list[dict[str, Any]]:
    records = []
    request_index = worker_index
    while time.perf_counter() < start_at:
        time.sleep(max(0.0, min(0.01, start_at - time.perf_counter())))

    while time.perf_counter() < end_at:
        path = path_template
        if ids:
            path = path_template.format(id=ids[request_index % len(ids)])
        global_index, scheduled_at = counters.schedule(process_index)
        records.append(
            measured_get(
                base_url, path, timeout, scheduled_at, global_index, counters, process_index
            )
        )
        request_index += 1
    return records


def closed_process(
    base_url: str,
    path_template: str,
    ids: list[int],
    timeout: float,
    duration: float,
    start_clock,
    start_event,
    ready_queue,
    process_index: int,
    concurrency: int,
    worker_offset: int,
    counters: RunCounters,
    result_queue,
) -> None:
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            ready_queue.put(process_index)
            start_event.wait()
            start_at = start_clock.value
            futures = [
                executor.submit(
                    closed_worker,
                    base_url,
                    path_template,
                    ids,
                    timeout,
                    start_at,
                    start_at + duration,
                    worker_offset + worker_index,
                    counters,
                    process_index,
                )
                for worker_index in range(concurrency)
            ]
            records = [record for future in futures for record in future.result()]
        result_queue.put({"records": records})
    except Exception as exception:
        result_queue.put(
            {"error": f"Процес {process_index}: {type(exception).__name__}: {exception}"}
        )
    finally:
        close_connections()


def run_closed(args: argparse.Namespace) -> dict[str, Any]:
    ids = load_ids(args.seed_file) if args.seed_file else []
    process_count = min(args.processes or (os.cpu_count() or 1), args.concurrency)
    process_count = max(1, process_count)
    concurrency_by_process = [
        args.concurrency // process_count + int(index < args.concurrency % process_count)
        for index in range(process_count)
    ]
    context = multiprocessing.get_context(
        "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    )
    result_queue = context.Queue()
    ready_queue = context.Queue()
    start_event = context.Event()
    start_clock = context.Value("d", 0.0)
    counters = RunCounters(context.Array("q", process_count * 5), process_count)
    processes = []
    worker_offset = 0
    for process_index, concurrency in enumerate(concurrency_by_process):
        process = context.Process(
            target=closed_process,
            args=(
                args.base_url,
                args.path,
                ids,
                args.timeout,
                args.duration,
                start_clock,
                start_event,
                ready_queue,
                process_index,
                concurrency,
                worker_offset,
                counters,
                result_queue,
            ),
        )
        process.start()
        processes.append(process)
        worker_offset += concurrency

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    sampler = None
    occupancy_samples = []
    try:
        for _ in processes:
            ready_queue.get(timeout=60)
        start_at = time.perf_counter() + 1.0
        sampler = InFlightSampler(
            counters, start_at, args.duration, args.sample_interval_ms / 1000,
        )
        start_clock.value = start_at
        sampler.start()
        start_event.set()
        queue_timeout = args.duration + args.timeout * 2 + 60
        for _ in processes:
            process_result = result_queue.get(timeout=queue_timeout)
            if "error" in process_result:
                errors.append(process_result["error"])
            else:
                records.extend(process_result["records"])
        if errors:
            raise RuntimeError("; ".join(errors))
        occupancy_samples = sampler.finish()
    except Empty as exception:
        raise RuntimeError("Один із процесів генератора не повернув результат") from exception
    finally:
        if sampler is not None:
            sampler.abort()
        start_event.set()
        for process in processes:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join()

    if counters.snapshot()[1] != len(records):
        raise RuntimeError("Кількість фактичних відправлень не збігається з журналом запитів")
    return summarize_records(
        records=records,
        mode="closed",
        started_at=start_at,
        nominal_duration=args.duration,
        requested_rate=None,
        extra={
            "concurrency": args.concurrency,
            "processes": process_count,
            "concurrency_by_process": concurrency_by_process,
            "closed_loop_execution": "fixed_total_concurrency_split_across_process_thread_pools",
            "path_template": args.path,
            "sample_interval_ms": args.sample_interval_ms,
            "worker_capacity": args.concurrency,
            "worker_peak_active_by_process": counters.peaks(),
        },
        occupancy_samples=occupancy_samples,
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

    compact = {key: value for key, value in result.items() if key not in ("samples", "in_flight_samples")}
    print(json.dumps(compact, ensure_ascii=False, indent=2), flush=True)


def add_request_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default="http://proxy")
    parser.add_argument("--path", default="/referrals/{id}")
    parser.add_argument("--seed-file")
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--output")
    parser.add_argument("--sample-interval-ms", type=float, default=1.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Генератор навантаження для лабораторної роботи 1"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    open_parser = subparsers.add_parser("open", help="Відкритий контур")
    add_request_arguments(open_parser)
    open_parser.add_argument("--rate", type=float, required=True)
    open_parser.add_argument("--processes", type=int, default=0)
    open_parser.add_argument("--threads-per-process", type=int, default=0)

    closed_parser = subparsers.add_parser("closed", help="Закритий контур")
    add_request_arguments(closed_parser)
    closed_parser.add_argument("--concurrency", type=int, required=True)
    closed_parser.add_argument("--processes", type=int, default=0)

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
    for name in ("duration", "timeout", "rate", "sample_interval_ms", "concurrency", "count"):
        if hasattr(args, name) and getattr(args, name) <= 0:
            raise ValueError(f"Параметр {name} має бути більшим за нуль")
    for name in ("processes", "threads_per_process"):
        if hasattr(args, name) and getattr(args, name) < 0:
            raise ValueError(f"Параметр {name} не може бути відʼємним")
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
