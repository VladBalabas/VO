import json
from collections.abc import Mapping, Sequence
from typing import Any

from backend.data.database import AsyncpgDatabase
from backend.domain.tasks import ProcessingTask, TaskStatus


def _to_task(record: Mapping[str, Any]) -> ProcessingTask:
    result = record.get("result")
    if isinstance(result, str):
        result = json.loads(result)
    return ProcessingTask(
        id=int(record["id"]),
        record_id=int(record["record_id"]),
        status=TaskStatus(record["status"]),
        priority=int(record["priority"]),
        seed=int(record["seed"]),
        attempts=int(record["attempts"]),
        max_attempts=int(record["max_attempts"]),
        token=int(record["token"]),
        lease_until=record["lease_until"],
        worker_id=record["worker_id"],
        created_at=record["created_at"],
        started_at=record["started_at"],
        finished_at=record["finished_at"],
        result=None if result is None else dict(result),
    )


SELECT_TASK = """
SELECT t.id, t.record_id, t.status, t.priority, t.seed, t.attempts,
       t.max_attempts, t.token, t.lease_until, t.worker_id,
       t.created_at, t.started_at, t.finished_at, r.result
FROM tasks AS t
LEFT JOIN task_results AS r ON r.task_id = t.id
"""


class PostgresTaskRepository:
    def __init__(self, database: AsyncpgDatabase) -> None:
        self._database = database

    async def create(self, record_id: int, priority: int, seed: int) -> ProcessingTask | None:
        record = await self._database.pool.fetchrow(
            """
            INSERT INTO tasks (record_id, priority, seed)
            SELECT id, $2, $3 FROM referrals WHERE id = $1
            RETURNING id, record_id, status, priority, seed, attempts,
                      max_attempts, token, lease_until, worker_id,
                      created_at, started_at, finished_at, NULL::jsonb AS result
            """,
            record_id,
            priority,
            seed,
        )
        return None if record is None else _to_task(record)

    async def get(self, task_id: int) -> ProcessingTask | None:
        record = await self._database.pool.fetchrow(
            SELECT_TASK + " WHERE t.id = $1",
            task_id,
        )
        return None if record is None else _to_task(record)

    async def list(
        self,
        limit: int,
        offset: int,
        status: TaskStatus | None,
    ) -> tuple[Sequence[ProcessingTask], int]:
        if status is None:
            records = await self._database.pool.fetch(
                SELECT_TASK + " ORDER BY t.id LIMIT $1 OFFSET $2",
                limit,
                offset,
            )
            total = await self._database.pool.fetchval("SELECT COUNT(*) FROM tasks")
        else:
            records = await self._database.pool.fetch(
                SELECT_TASK + " WHERE t.status = $1 ORDER BY t.id LIMIT $2 OFFSET $3",
                status.value,
                limit,
                offset,
            )
            total = await self._database.pool.fetchval(
                "SELECT COUNT(*) FROM tasks WHERE status = $1",
                status.value,
            )
        return [_to_task(record) for record in records], int(total)

    async def requeue_expired(self) -> tuple[int, int]:
        rows = await self._database.pool.fetch(
            """
            UPDATE tasks
            SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                worker_id = NULL,
                lease_until = NULL,
                finished_at = CASE
                    WHEN attempts >= max_attempts THEN clock_timestamp()
                    ELSE NULL
                END
            WHERE status = 'running' AND lease_until < clock_timestamp()
            RETURNING status
            """
        )
        requeued = sum(row["status"] == "queued" for row in rows)
        failed = len(rows) - requeued
        return requeued, failed

    async def queue_counts(self) -> tuple[int, int]:
        record = await self._database.pool.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                   COUNT(*) FILTER (WHERE status = 'running') AS running
            FROM tasks
            """
        )
        return int(record["queued"]), int(record["running"])

