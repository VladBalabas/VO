"""Open-loop Lab 3 generator with forty rotating persistent connections.

Every logical connection owns one aiohttp session/connector and processes its
queue serially.  The session is replaced after exactly 100 attempts.  Request
latency starts at the planned timestamp, so queueing and scheduler lag are not
hidden from the result.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import math
import statistics
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))]


def classification(record: dict[str, Any]) -> str:
    status = record["status"]
    if status is not None:
        return "success" if 200 <= status < 300 else "non_2xx"
    return "non_2xx" if record["error_type"] == "TimeoutError" else "connection_error"


@dataclass(slots=True)
class ConnectionStats:
    worker: int
    sessions_created: int = 0
    attempts: int = 0
    tcp_connections_created: int = 0
    tcp_connections_reused: int = 0
    rotations: list[int] = field(default_factory=list)


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.count = 0

    async def run(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("x", encoding="utf-8", newline="\n") as stream:
            batch: list[str] = []
            while True:
                item = await self.queue.get()
                if item is None:
                    self.queue.task_done()
                    break
                batch.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                self.count += 1
                self.queue.task_done()
                if len(batch) >= 1000 or self.queue.empty():
                    stream.write("\n".join(batch) + "\n")
                    batch.clear()
            if batch:
                stream.write("\n".join(batch) + "\n")
            stream.flush()


def step_summary(
    recent: deque[dict[str, Any]], *, index: int, rate: float,
    start_ms: float, duration_ms: float, start_epoch: float,
) -> dict[str, Any]:
    window_start = start_ms + duration_ms - 20_000
    window_end = start_ms + duration_ms
    rows = [r for r in recent if window_start <= r["completed_offset_ms"] < window_end]
    counts = [0] * 20
    for row in rows:
        if classification(row) == "success":
            second = int((row["completed_offset_ms"] - window_start) // 1000)
            if 0 <= second < 20:
                counts[second] += 1
    latencies = [r["latency_ms"] for r in rows]
    successful_latencies = [r["latency_ms"] for r in rows if classification(r) == "success"]
    pod_counts = Counter(
        r["pod"] for r in rows if classification(r) == "success" and r.get("pod")
    )
    whole_step_rows = [
        r for r in recent if start_ms <= r["completed_offset_ms"] < window_end
    ]
    return {
        "step_index": index,
        "rate": rate,
        "start_offset_ms": start_ms,
        "start_epoch": start_epoch + start_ms / 1000,
        "duration_seconds": duration_ms / 1000,
        "measurement_start_offset_ms": window_start,
        "measurement_duration_seconds": 20,
        "x": statistics.fmean(counts),
        "stddev": statistics.stdev(counts),
        "counts_last_20s": counts,
        "completed_window": len(rows),
        "successful_window": sum(classification(r) == "success" for r in rows),
        "non_2xx": sum(classification(r) == "non_2xx" for r in rows),
        "timeouts": sum(r["error_type"] == "TimeoutError" for r in rows),
        "connection_errors": sum(classification(r) == "connection_error" for r in rows),
        "client_p99_ms": quantile(latencies, .99),
        "client_2xx_p99_ms": quantile(successful_latencies, .99),
        "schedule_lag_p95_ms": quantile([r["schedule_lag_ms"] for r in rows], .95),
        "sent_during_step": sum(
            start_ms <= r["sent_offset_ms"] < window_end for r in whole_step_rows
        ),
        "pod_success_counts": dict(sorted(pod_counts.items())),
        "scheduled": math.floor(rate * duration_ms / 1000),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    import aiohttp

    output = Path(args.output)
    raw_path = output.with_suffix(".jsonl")
    summary_path = output.with_suffix(".summary.json")
    if raw_path.exists() or summary_path.exists():
        raise FileExistsError(f"Refusing to overwrite raw result: {raw_path} / {summary_path}")

    ids = []
    if args.seed_file:
        ids = json.loads(Path(args.seed_file).read_text(encoding="utf-8"))["ids"]
    profile = None
    if args.profile_file:
        profile = json.loads(Path(args.profile_file).read_text(encoding="utf-8"))["segments"]

    start_epoch = args.start_epoch if args.start_epoch is not None else time.time() + 1
    start_perf = time.perf_counter() + (start_epoch - time.time())
    counters = {
        "scheduled": 0, "sent": 0, "completed": 0, "successful": 0,
        "non_2xx": 0, "connection_errors": 0, "active": 0, "peak_active": 0,
    }
    queues = [asyncio.Queue() for _ in range(args.connections)]
    connection_stats = [ConnectionStats(worker=i) for i in range(args.connections)]
    recent: deque[dict[str, Any]] = deque()
    state_samples: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    writer = JsonlWriter(raw_path)
    scheduling_done = False

    async def wait_until(moment: float) -> None:
        while True:
            delay = moment - time.perf_counter()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, .05))

    async def make_session(stats: ConnectionStats):
        trace = aiohttp.TraceConfig()

        async def created(*_unused):
            stats.tcp_connections_created += 1

        async def reused(*_unused):
            stats.tcp_connections_reused += 1

        trace.on_connection_create_end.append(created)
        trace.on_connection_reuseconn.append(reused)
        connector = aiohttp.TCPConnector(limit=1, limit_per_host=1, force_close=False,
                                         keepalive_timeout=300)
        timeout = aiohttp.ClientTimeout(total=args.timeout)
        stats.sessions_created += 1
        return aiohttp.ClientSession(connector=connector, timeout=timeout,
                                     trace_configs=[trace])

    async def worker(worker_id: int) -> None:
        nonlocal recent
        stats = connection_stats[worker_id]
        session = await make_session(stats)
        session_attempts = 0
        try:
            while True:
                item = await queues[worker_id].get()
                if item is None:
                    queues[worker_id].task_done()
                    return
                request_index, scheduled_at, segment_index = item
                sent = time.perf_counter()
                counters["sent"] += 1
                counters["active"] += 1
                counters["peak_active"] = max(counters["peak_active"], counters["active"])
                path = args.path.format(id=ids[request_index % len(ids)]) if ids else args.path
                status = None
                pod = None
                error = None
                error_type = None
                try:
                    async with session.get(args.base_url + path,
                                           headers={"Accept": "application/json"}) as response:
                        await response.read()
                        status = response.status
                        pod = response.headers.get("X-Pod")
                except asyncio.TimeoutError as exc:
                    error_type = "TimeoutError"
                    error = f"{type(exc).__name__}: {exc}"
                except Exception as exc:  # recorded as raw evidence
                    error_type = type(exc).__name__
                    error = f"{error_type}: {exc}"
                completed = time.perf_counter()
                counters["active"] -= 1
                counters["completed"] += 1
                stats.attempts += 1
                session_attempts += 1
                record = {
                    "request_index": request_index,
                    "segment_index": segment_index,
                    "worker": worker_id,
                    "session_generation": stats.sessions_created,
                    "request_on_session": session_attempts,
                    "scheduled_offset_ms": (scheduled_at - start_perf) * 1000,
                    "sent_offset_ms": (sent - start_perf) * 1000,
                    "completed_offset_ms": (completed - start_perf) * 1000,
                    "schedule_lag_ms": max(0.0, (sent - scheduled_at) * 1000),
                    "latency_ms": (completed - scheduled_at) * 1000,
                    "network_latency_ms": (completed - sent) * 1000,
                    "status": status,
                    "pod": pod,
                    "error_type": error_type,
                    "error": error,
                }
                kind = classification(record)
                counter_key = {
                    "success": "successful",
                    "non_2xx": "non_2xx",
                    "connection_error": "connection_errors",
                }[kind]
                counters[counter_key] += 1
                recent.append(record)
                cutoff = (completed - start_perf) * 1000 - 45_000
                while recent and recent[0]["completed_offset_ms"] < cutoff:
                    recent.popleft()
                await writer.queue.put(record)
                queues[worker_id].task_done()
                if session_attempts == args.requests_per_connection:
                    stats.rotations.append(stats.attempts)
                    await session.close()
                    session = await make_session(stats)
                    session_attempts = 0
        finally:
            await session.close()

    async def sampler() -> None:
        while not scheduling_done or counters["completed"] < counters["scheduled"]:
            now = time.perf_counter()
            state_samples.append({
                "offset_ms": (now - start_perf) * 1000,
                **counters,
                "queue_depth": counters["scheduled"] - counters["sent"],
                "outstanding": counters["scheduled"] - counters["completed"],
                "writer_queue": writer.queue.qsize(),
            })
            await asyncio.sleep(args.sample_interval_ms / 1000)

    async def dispatch_segment(rate: float, duration: float, start_offset: float,
                               segment_index: int) -> None:
        total = math.floor(rate * duration)
        first = counters["scheduled"]
        segment_start = start_perf + start_offset
        for local_index in range(total):
            scheduled_at = segment_start + local_index / rate
            await wait_until(scheduled_at)
            request_index = first + local_index
            counters["scheduled"] += 1
            await queues[request_index % args.connections].put(
                (request_index, scheduled_at, segment_index)
            )
        await wait_until(segment_start + duration)

    writer_task = asyncio.create_task(writer.run())
    worker_tasks = [asyncio.create_task(worker(i)) for i in range(args.connections)]
    sampler_task = asyncio.create_task(sampler())
    nominal_duration = 0.0
    stop_two_misses = False
    try:
        if profile is not None:
            offset = 0.0
            for index, segment in enumerate(profile):
                rate = float(segment["rate"])
                duration = float(segment["duration"])
                await dispatch_segment(rate, duration, offset, index)
                offset += duration
            nominal_duration = offset
        else:
            await dispatch_segment(args.warmup_rate, args.warmup_duration, 0.0, -1)
            misses = 0
            for index, rate in enumerate(itertools.count(args.stair_start, args.stair_increment)):
                start_offset = args.warmup_duration + index * args.step_duration
                await dispatch_segment(rate, args.step_duration, start_offset, index)
                info = step_summary(
                    recent, index=index, rate=rate,
                    start_ms=start_offset * 1000,
                    duration_ms=args.step_duration * 1000,
                    start_epoch=start_epoch,
                )
                steps.append(info)
                print(json.dumps({"step": rate, "x": info["x"],
                                  "p99_ms": info["client_p99_ms"],
                                  "non_2xx": info["non_2xx"]}), flush=True)
                misses = misses + 1 if info["x"] < .9 * rate else 0
                if misses >= 2:
                    stop_two_misses = True
                    break
                if args.max_rate is not None and rate >= args.max_rate:
                    break
            nominal_duration = args.warmup_duration + args.step_duration * len(steps)
    finally:
        scheduling_done = True
        await asyncio.gather(*(queue.join() for queue in queues))
        for queue in queues:
            await queue.put(None)
        await asyncio.gather(*worker_tasks)
        await sampler_task
        await writer.queue.put(None)
        await writer.queue.join()
        await writer_task

    finished_epoch = time.time()
    sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    connection_evidence = [
        {
            "worker": s.worker,
            "attempts": s.attempts,
            "sessions_created": s.sessions_created,
            "tcp_connections_created": s.tcp_connections_created,
            "tcp_connections_reused": s.tcp_connections_reused,
            "rotation_attempt_counts": s.rotations,
        }
        for s in connection_stats
    ]
    result = {
        "schema_version": 1,
        "mode": "profile" if profile is not None else "stair",
        "base_url": args.base_url,
        "path": args.path,
        "connections": args.connections,
        "requests_per_connection": args.requests_per_connection,
        "timeout_seconds": args.timeout,
        "started_at": datetime.fromtimestamp(start_epoch, UTC).isoformat(),
        "started_at_epoch": start_epoch,
        "finished_at": datetime.fromtimestamp(finished_epoch, UTC).isoformat(),
        "nominal_duration_seconds": nominal_duration,
        "drain_duration_seconds": max(0.0, finished_epoch - start_epoch - nominal_duration),
        "profile": profile,
        "steps": steps,
        "stop_two_misses": stop_two_misses,
        "counts": counters,
        "state_samples": state_samples,
        "connection_evidence": connection_evidence,
        "raw_file": raw_path.name,
        "raw_records": writer.count,
        "raw_sha256": sha256,
    }
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--base-url", default="http://localhost:8080")
    result.add_argument("--path", default="/referrals/{id}")
    result.add_argument("--seed-file")
    result.add_argument("--output", required=True,
                        help="Output prefix; .jsonl and .summary.json are added")
    result.add_argument("--profile-file")
    result.add_argument("--connections", type=int, default=40)
    result.add_argument("--requests-per-connection", type=int, default=100)
    result.add_argument("--timeout", type=float, default=5.0)
    result.add_argument("--sample-interval-ms", type=int, default=250)
    result.add_argument("--start-epoch", type=float)
    result.add_argument("--warmup-rate", type=int, default=200)
    result.add_argument("--warmup-duration", type=int, default=10)
    result.add_argument("--stair-start", type=int, default=200)
    result.add_argument("--stair-increment", type=int, default=200)
    result.add_argument("--step-duration", type=int, default=30)
    result.add_argument("--max-rate", type=int)
    return result


def main() -> None:
    args = parser().parse_args()
    if args.connections != 40:
        raise SystemExit("Lab 3 variant 2 requires exactly 8*R = 40 connections")
    if args.requests_per_connection != 100:
        raise SystemExit("The assignment requires rotation after exactly 100 requests")
    if args.profile_file is None and args.step_duration != 30:
        raise SystemExit("The Lab 3 stair-step duration must be 30 seconds")
    summary = asyncio.run(run(args))
    print(json.dumps({"counts": summary["counts"],
                      "raw_sha256": summary["raw_sha256"],
                      "stop_two_misses": summary["stop_two_misses"]}, indent=2))


if __name__ == "__main__":
    main()
