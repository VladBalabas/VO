import asyncio
import logging
import secrets
from dataclasses import dataclass

from prometheus_client import Gauge

from backend.data.postgres_task_repository import PostgresTaskRepository
from backend.domain.tasks import ProcessingTask, TaskStatus


LOGGER = logging.getLogger(__name__)
TASKS_QUEUED = Gauge("tasks_queued", "Кількість задач у стані queued")
TASKS_RUNNING = Gauge("tasks_running", "Кількість задач у стані running")


class TaskNotFoundError(LookupError):
    pass


class ReferralForTaskNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class TaskList:
    items: list[ProcessingTask]
    total: int
    limit: int
    offset: int


class TaskService:
    def __init__(self, repository: PostgresTaskRepository) -> None:
        self._repository = repository

    async def create(
        self,
        record_id: int,
        priority: int,
        seed: int | None,
    ) -> ProcessingTask:
        actual_seed = seed if seed is not None else secrets.randbelow(2**31 - 1) + 1
        task = await self._repository.create(record_id, priority, actual_seed)
        if task is None:
            raise ReferralForTaskNotFoundError
        return task

    async def get(self, task_id: int) -> ProcessingTask:
        task = await self._repository.get(task_id)
        if task is None:
            raise TaskNotFoundError
        return task

    async def list(
        self,
        limit: int,
        offset: int,
        status: TaskStatus | None,
    ) -> TaskList:
        items, total = await self._repository.list(limit, offset, status)
        return TaskList(list(items), total, limit, offset)

    async def maintenance_tick(self) -> dict[str, int]:
        requeued, failed = await self._repository.requeue_expired()
        queued, running = await self._repository.queue_counts()
        TASKS_QUEUED.set(queued)
        TASKS_RUNNING.set(running)
        if requeued or failed:
            LOGGER.info("lease maintenance: requeued=%d failed=%d", requeued, failed)
        return {"requeued": requeued, "failed": failed, "queued": queued, "running": running}

    async def maintenance_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.maintenance_tick()
            except Exception:
                LOGGER.exception("Не вдалося оновити метрики черги або lease")
            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
            except TimeoutError:
                pass

