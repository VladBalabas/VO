"""Open-loop HTTP generator with a genuinely continuous stair-step profile."""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

def quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))]


def classify(record):
    status = record["status"]
    if status is not None:
        return "success" if 200 <= status < 300 else "non_2xx"
    return "non_2xx" if record["error_type"] == "TimeoutError" else "connection_error"


def step_summary(records, dispatches, *, rate, index, start_ms, duration_ms, start_epoch):
    end_ms = start_ms + duration_ms
    window = [r for r in records if start_ms + 5000 <= r["completed_offset_ms"] < end_ms]
    own_dispatches = [d for d in dispatches if d["step_index"] == index]
    counts = [0] * 15
    for record in window:
        if classify(record) == "success":
            counts[int((record["completed_offset_ms"] - start_ms - 5000) // 1000)] += 1
    success = [r["latency_ms"] for r in window if classify(r) == "success"]
    return {
        "rate": rate,
        "step_index": index,
        "start_offset_ms": start_ms,
        "start_epoch": start_epoch + start_ms / 1000,
        "x": statistics.fmean(counts),
        "stddev": statistics.stdev(counts),
        "counts_last_15s": counts,
        "completed_window": len(window),
        "non_2xx": sum(classify(r) == "non_2xx" for r in window),
        "timeouts": sum(r["error_type"] == "TimeoutError" for r in window),
        "connection_errors": sum(classify(r) == "connection_error" for r in window),
        "sent_during_step": sum(start_ms <= d["sent_offset_ms"] < end_ms for d in dispatches),
        "scheduled": rate * duration_ms // 1000,
        "schedule_lag_p95_ms": quantile([d["schedule_lag_ms"] for d in own_dispatches], .95),
        "client_p99_ms": quantile(success, .99),
    }


async def run(args):
    import aiohttp

    ids = json.loads(Path(args.seed_file).read_text(encoding="utf-8"))["ids"] if args.seed_file else []
    start_epoch = args.start_epoch if args.start_epoch is not None else time.time() + 1
    start_perf = time.perf_counter() + (start_epoch - time.time())
    counts = {"scheduled": 0, "dispatched": 0, "completed": 0,
              "successful": 0, "peak_active": 0}
    samples = []
    records = []
    dispatches = []
    pending = set()
    steps = []
    scheduling_done = False
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=0, limit_per_host=0, force_close=False)

    async def sample():
        while not scheduling_done or counts["completed"] < counts["scheduled"]:
            now = time.perf_counter()
            samples.append({"offset_ms": (now - start_perf) * 1000, **counts,
                            "in_flight": counts["scheduled"] - counts["completed"]})
            await asyncio.sleep(args.sample_interval_ms / 1000)
        samples.append({"offset_ms": (time.perf_counter() - start_perf) * 1000,
                        **counts, "in_flight": 0})

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def get(i, scheduled_at, step_index):
            sent = time.perf_counter()
            sent_offset_ms = (sent - start_perf) * 1000
            schedule_lag_ms = max(0, (sent - scheduled_at) * 1000)
            dispatches.append({"step_index": step_index, "sent_offset_ms": sent_offset_ms,
                               "schedule_lag_ms": schedule_lag_ms})
            counts["dispatched"] += 1
            counts["peak_active"] = max(counts["peak_active"],
                                        counts["dispatched"] - counts["completed"])
            path = args.path.format(id=ids[i % len(ids)]) if ids else args.path
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
            except Exception as exc:
                error_type = type(exc).__name__
                error = f"{error_type}: {exc}"
            completed = time.perf_counter()
            counts["completed"] += 1
            counts["successful"] += int(status is not None and 200 <= status < 300)
            records.append({
                "request_index": i, "step_index": step_index,
                "scheduled_offset_ms": (scheduled_at - start_perf) * 1000,
                "sent_offset_ms": sent_offset_ms,
                "completed_offset_ms": (completed - start_perf) * 1000,
                "latency_ms": (completed - scheduled_at) * 1000,
                "network_latency_ms": (completed - sent) * 1000,
                "schedule_lag_ms": schedule_lag_ms,
                "status": status, "pod": pod, "error": error, "error_type": error_type,
            })

        async def dispatch_step(rate, duration, start_offset, step_index):
            total = math.floor(rate * duration)
            first_index = counts["scheduled"]
            step_start = start_perf + start_offset
            for i in range(total):
                scheduled_at = step_start + i / rate
                delay = scheduled_at - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                counts["scheduled"] += 1
                task = asyncio.create_task(get(first_index + i, scheduled_at, step_index))
                pending.add(task)
                task.add_done_callback(pending.discard)
            delay = step_start + duration - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)

        sampler = asyncio.create_task(sample())
        if args.mode == "profile":
            await dispatch_step(200, 10, 0, -1)
            misses = 0
            for index, rate in enumerate(itertools.count(200, 200)):
                offset = 10 + index * 20
                await dispatch_step(rate, 20, offset, index)
                info = step_summary(records, dispatches, rate=rate, index=index,
                                    start_ms=offset * 1000, duration_ms=20000,
                                    start_epoch=start_epoch)
                steps.append(info)
                print(json.dumps({"step": rate, "x": info["x"],
                                  "sent": info["sent_during_step"],
                                  "non_2xx": info["non_2xx"]}), flush=True)
                misses = misses + 1 if info["x"] < .9 * rate else 0
                if misses >= 2 or (args.g_ceiling is not None and rate >= args.g_ceiling):
                    break
            nominal_duration = 10 + 20 * len(steps)
        else:
            await dispatch_step(args.rate, args.duration, 0, 0)
            nominal_duration = args.duration
        scheduling_done = True
        await asyncio.gather(*list(pending))
        await sampler

    records.sort(key=lambda r: r["request_index"])
    successes = [r for r in records if classify(r) == "success"]
    finish = max((r["completed_offset_ms"] for r in records), default=0)
    result = {
        "schema_version": 4,
        "generator_implementation": "aiohttp_continuous_open_loop",
        "latency_origin": "scheduled_at",
        "generated_at": datetime.now(UTC).isoformat(),
        "started_at_epoch": start_epoch,
        "requested_rate_rps": args.rate if args.mode == "open" else None,
        "nominal_duration_seconds": nominal_duration,
        "timeout_seconds": args.timeout,
        "path_template": args.path,
        "mode": args.mode,
        "g_ceiling": args.g_ceiling,
        "stop_two_misses": len(steps) >= 2 and all(
            s["x"] < .9 * s["rate"] for s in steps[-2:]),
        "requests_scheduled": counts["scheduled"],
        "requests_sent": counts["dispatched"],
        "responses_2xx": len(successes),
        "responses_non_2xx": sum(classify(r) == "non_2xx" for r in records),
        "request_timeouts": sum(r["error_type"] == "TimeoutError" for r in records),
        "connection_errors": sum(classify(r) == "connection_error" for r in records),
        "request_exceptions": sum(r["status"] is None for r in records),
        "worker_capacity": None,
        "worker_peak_active_by_process": [counts["peak_active"]],
        "schedule_lag_p95_ms": quantile([r["schedule_lag_ms"] for r in records], .95),
        "latency_p50_ms": quantile([r["latency_ms"] for r in successes], .5),
        "latency_p95_ms": quantile([r["latency_ms"] for r in successes], .95),
        "latency_p99_ms": quantile([r["latency_ms"] for r in successes], .99),
        "observation_seconds": max(nominal_duration, finish / 1000),
        "status_counts": dict(Counter(str(r["status"]) for r in records)),
        "steps": steps,
        "samples": records,
        "in_flight_samples": samples,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("samples", "in_flight_samples", "steps")},
                     ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["open", "profile"])
    parser.add_argument("--base-url", default="http://proxy")
    parser.add_argument("--path", required=True)
    parser.add_argument("--seed-file")
    parser.add_argument("--rate", type=int)
    parser.add_argument("--duration", type=int)
    parser.add_argument("--g-ceiling", type=float)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--sample-interval-ms", type=float, default=100)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start-epoch", type=float)
    args = parser.parse_args()
    if args.mode == "open" and (not args.rate or not args.duration):
        parser.error("open mode requires --rate and --duration")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
