from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp
import asyncpg
from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from worker.processing import process_slice
from worker.settings import WorkerSettings, load_settings


TASKS = Counter("worker_tasks_total", "Події виконання задач", ["result"])
CLAIMS = Counter("worker_claims_total", "Результати захоплення", ["result"])
DURATION = Histogram(
    "worker_task_duration_seconds",
    "Тривалість кроків воркера",
    ["step"],
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
IN_FLIGHT = Gauge("worker_tasks_in_flight", "Кількість захоплених задач")


@dataclass(slots=True)
class ClaimedTask:
    id: int
    record_id: int
    seed: int
    priority: int
    attempts: int
    max_attempts: int
    token: int
    created_at: datetime


def log_event(event: str, **values: Any) -> None:
    print(json.dumps({"at": datetime.now(UTC).isoformat(), "event": event, **values},
                     ensure_ascii=False), flush=True)


def task_from_record(record: asyncpg.Record) -> ClaimedTask:
    return ClaimedTask(**{name: record[name] for name in ClaimedTask.__dataclass_fields__})


class LostOwnershipError(RuntimeError):
    pass


class QueueWorker:
    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self.pool: asyncpg.Pool | None = None
        self.session: aiohttp.ClientSession | None = None
        self.stopping = asyncio.Event()
        self.stop_requested_at: float | None = None
        self.owned: dict[int, ClaimedTask] = {}
        self.metrics_runner: web.AppRunner | None = None

    def request_stop(self) -> None:
        if self.stop_requested_at is None:
            self.stop_requested_at = time.monotonic()
            log_event("shutdown_requested",
                      task_timeout_seconds=self.settings.shutdown_task_timeout_seconds)
        self.stopping.set()

    def shutdown_time_remaining(self) -> float:
        if self.stop_requested_at is None:
            return self.settings.shutdown_task_timeout_seconds
        elapsed = time.monotonic() - self.stop_requested_at
        return max(0.0, self.settings.shutdown_task_timeout_seconds - elapsed)

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            host=self.settings.postgres_host,
            port=self.settings.postgres_port,
            database=self.settings.postgres_database,
            user=self.settings.postgres_user,
            password=self.settings.postgres_password,
            min_size=1,
            max_size=3,
            command_timeout=10,
        )
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        app = web.Application()
        app.router.add_get("/metrics", self.metrics)
        self.metrics_runner = web.AppRunner(app, access_log=None)
        await self.metrics_runner.setup()
        await web.TCPSite(self.metrics_runner, "0.0.0.0", self.settings.metrics_port).start()

    async def metrics(self, _request: web.Request) -> web.Response:
        return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
        if self.metrics_runner is not None:
            await self.metrics_runner.cleanup()
        if self.pool is not None:
            await self.pool.close()

    async def claim(self) -> list[ClaimedTask]:
        assert self.pool is not None
        started = time.perf_counter()
        if self.settings.claim_mode == "naive":
            records = await self.pool.fetch(
                """
                SELECT id, record_id, seed, priority, attempts, max_attempts, token, created_at
                FROM tasks WHERE status='queued'
                ORDER BY priority DESC, created_at, id LIMIT $1
                """,
                self.settings.claim_batch_size,
            )
            claimed = []
            for selected in records:
                row = await self.pool.fetchrow(
                    """
                    UPDATE tasks SET status='running', worker_id=$2, token=token+1,
                        attempts=attempts+1, started_at=clock_timestamp(),
                        lease_until=clock_timestamp()+$3::double precision*interval '1 second'
                    WHERE id=$1
                    RETURNING id, record_id, seed, priority, attempts, max_attempts, token, created_at
                    """,
                    selected["id"], self.settings.worker_id, self.settings.lease_seconds,
                )
                if row is not None:
                    claimed.append(task_from_record(row))
        else:
            skip = " SKIP LOCKED" if self.settings.claim_mode == "skip_locked" else ""
            query = f"""
                WITH selected AS (
                    SELECT id FROM tasks WHERE status='queued'
                    ORDER BY priority DESC, created_at, id
                    LIMIT $1 FOR UPDATE{skip}
                )
                UPDATE tasks AS t SET status='running', worker_id=$2, token=t.token+1,
                    attempts=t.attempts+1, started_at=clock_timestamp(),
                    lease_until=clock_timestamp()+$3::double precision*interval '1 second'
                FROM selected WHERE t.id=selected.id
                RETURNING t.id, t.record_id, t.seed, t.priority, t.attempts,
                          t.max_attempts, t.token, t.created_at
            """
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    records = await connection.fetch(
                        query, self.settings.claim_batch_size, self.settings.worker_id,
                        self.settings.lease_seconds,
                    )
            claimed = [task_from_record(row) for row in records]
        elapsed = time.perf_counter() - started
        DURATION.labels("claim").observe(elapsed)
        if claimed:
            CLAIMS.labels("task").inc(len(claimed))
            for task in claimed:
                self.owned[task.id] = task
            IN_FLIGHT.set(len(self.owned))
            log_event("claim", mode=self.settings.claim_mode,
                      task_ids=[task.id for task in claimed], duration_ms=elapsed * 1000)
        else:
            CLAIMS.labels("empty").inc()
        return sorted(claimed, key=lambda task: (-task.priority, task.created_at, task.id))

    async def heartbeat(self) -> None:
        assert self.pool is not None
        while not self.stopping.is_set():
            await asyncio.sleep(self.settings.lease_seconds / 3)
            for task in list(self.owned.values()):
                result = await self.pool.execute(
                    """
                    UPDATE tasks SET lease_until=clock_timestamp()+$1::double precision*interval '1 second'
                    WHERE id=$2 AND token=$3 AND status='running' AND worker_id=$4
                    """,
                    self.settings.lease_seconds, task.id, task.token, self.settings.worker_id,
                )
                if result != "UPDATE 1":
                    log_event("stale_heartbeat", task_id=task.id, token=task.token,
                              updated_rows=0)
                    self.owned.pop(task.id, None)
                    IN_FLIGHT.set(len(self.owned))

    async def fetch_referral(self, task: ClaimedTask) -> dict[str, Any]:
        assert self.session is not None
        started = time.perf_counter()
        async with self.session.get(f"{self.settings.web_url}/referrals/{task.record_id}") as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(f"GET referral повернув {response.status}: {body}")
            result = json.loads(body)
        elapsed = time.perf_counter() - started
        DURATION.labels("fetch").observe(elapsed)
        log_event("step", task_id=task.id, step="fetch", duration_ms=elapsed * 1000)
        return result

    async def store(self, task: ClaimedTask, result: dict[str, Any]) -> None:
        assert self.pool is not None
        started = time.perf_counter()
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                if self.settings.claim_mode == "naive":
                    updated = await connection.fetchval(
                        """
                        UPDATE tasks SET status='done', finished_at=clock_timestamp(),
                            lease_until=NULL WHERE id=$1 RETURNING id
                        """, task.id,
                    )
                else:
                    updated = await connection.fetchval(
                        """
                        UPDATE tasks SET status='done', finished_at=clock_timestamp(),
                            lease_until=NULL WHERE id=$1 AND token=$2 AND status='running'
                        RETURNING id
                        """, task.id, task.token,
                    )
                if updated is None:
                    raise LostOwnershipError(f"Задача {task.id} більше не належить воркеру")
                conflict = " ON CONFLICT (task_id) DO NOTHING" if self.settings.idempotent_results else ""
                await connection.execute(
                    """
                    INSERT INTO task_results(task_id, token, worker_id, result)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """ + conflict,
                    task.id, task.token, self.settings.worker_id,
                    json.dumps(result, ensure_ascii=False),
                )
        elapsed = time.perf_counter() - started
        DURATION.labels("store").observe(elapsed)
        log_event("step", task_id=task.id, step="store", duration_ms=elapsed * 1000)

    async def retry_or_fail(self, task: ClaimedTask, error: Exception) -> None:
        assert self.pool is not None
        final = task.attempts >= task.max_attempts
        status = "failed" if final else "queued"
        command = await self.pool.execute(
            """
            UPDATE tasks SET status=$1::varchar, worker_id=NULL, lease_until=NULL,
                finished_at=CASE WHEN $1::varchar='failed' THEN clock_timestamp() ELSE NULL END
            WHERE id=$2 AND token=$3 AND status='running'
            """,
            status, task.id, task.token,
        )
        if command == "UPDATE 1":
            if final:
                TASKS.labels("failed").inc()
            log_event("process_error", task_id=task.id, token=task.token,
                      attempts=task.attempts, new_status=status,
                      error_type=type(error).__name__, error=str(error))
        else:
            TASKS.labels("lost").inc()
            log_event("stale_error", task_id=task.id, token=task.token, updated_rows=0)

    async def process(self, task: ClaimedTask) -> None:
        try:
            referral = await self.fetch_referral(task)
            started = time.perf_counter()
            result = process_slice(task.seed, self.settings.slice_size,
                                   self.settings.q_lo, self.settings.q_hi)
            elapsed = time.perf_counter() - started
            DURATION.labels("process").observe(elapsed)
            log_event("step", task_id=task.id, step="process", duration_ms=elapsed * 1000)
            result["record_id"] = task.record_id
            result["referral_status"] = referral["status"]
            if self.settings.fail_rate and random.random() < self.settings.fail_rate:
                log_event("injected_exit", task_id=task.id, token=task.token,
                          fail_rate=self.settings.fail_rate)
                os._exit(1)
            await self.store(task, result)
            TASKS.labels("done").inc()
            log_event("task_done", task_id=task.id, token=task.token,
                      attempts=task.attempts, worker_id=self.settings.worker_id)
        except LostOwnershipError as error:
            TASKS.labels("lost").inc()
            log_event("stale_store", task_id=task.id, token=task.token,
                      updated_rows=0, error=str(error))
        except Exception as error:
            await self.retry_or_fail(task, error)
        finally:
            self.owned.pop(task.id, None)
            IN_FLIGHT.set(len(self.owned))

    async def return_task(self, task: ClaimedTask, reason: str) -> int:
        assert self.pool is not None
        result = await self.pool.execute(
            """
            UPDATE tasks SET status='queued', worker_id=NULL, lease_until=NULL
            WHERE id=$1 AND token=$2 AND status='running' AND worker_id=$3
            """, task.id, task.token, self.settings.worker_id,
        )
        returned = int(result == "UPDATE 1")
        if returned:
            log_event("graceful_return", task_id=task.id, token=task.token,
                      reason=reason)
        else:
            log_event("stale_graceful_return", task_id=task.id, token=task.token,
                      reason=reason, updated_rows=0)
        self.owned.pop(task.id, None)
        IN_FLIGHT.set(len(self.owned))
        return returned

    async def return_owned(self) -> int:
        returned = 0
        for task in list(self.owned.values()):
            returned += await self.return_task(task, "shutdown_pending")
        return returned

    async def process_until_stopped(self, task: ClaimedTask) -> int:
        processing = asyncio.create_task(self.process(task))
        stop_waiter = asyncio.create_task(self.stopping.wait())
        try:
            done, _ = await asyncio.wait(
                (processing, stop_waiter), return_when=asyncio.FIRST_COMPLETED
            )
            if processing in done:
                await processing
                return 0
            try:
                await asyncio.wait_for(
                    asyncio.shield(processing), timeout=self.shutdown_time_remaining()
                )
                return 0
            except TimeoutError:
                log_event("shutdown_task_timeout", task_id=task.id, token=task.token,
                          timeout_seconds=self.settings.shutdown_task_timeout_seconds)
                processing.cancel()
                await asyncio.gather(processing, return_exceptions=True)
                return await self.return_task(task, "shutdown_timeout")
        finally:
            stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)

    async def run(self) -> None:
        await self.connect()
        heartbeat = asyncio.create_task(self.heartbeat())
        log_event("worker_started", worker_id=self.settings.worker_id,
                  mode=self.settings.claim_mode, batch=self.settings.claim_batch_size)
        returned = 0
        try:
            while not self.stopping.is_set():
                tasks = await self.claim()
                if not tasks:
                    try:
                        await asyncio.wait_for(self.stopping.wait(), self.settings.poll_seconds)
                    except TimeoutError:
                        pass
                    continue
                for task in tasks:
                    if self.stopping.is_set():
                        break
                    returned += await self.process_until_stopped(task)
            returned += await self.return_owned()
            log_event("worker_stopped", worker_id=self.settings.worker_id,
                      returned=returned, exit_code=0)
        finally:
            self.request_stop()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self.close()


async def async_main() -> None:
    worker = QueueWorker(load_settings())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(async_main())

