from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProcessingTask:
    id: int
    record_id: int
    status: TaskStatus
    priority: int
    seed: int
    attempts: int
    max_attempts: int
    token: int
    lease_until: datetime | None
    worker_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.id <= 2**63 - 1:
            raise ValueError("Ідентифікатор задачі поза діапазоном BIGINT")
        if not 1 <= self.record_id <= 2**63 - 1:
            raise ValueError("Ідентифікатор направлення поза діапазоном BIGINT")
        if not 1 <= self.priority <= 3:
            raise ValueError("Пріоритет задачі має бути від 1 до 3")
        if not 0 <= self.seed <= 2**31 - 1:
            raise ValueError("Seed задачі має бути від 0 до 2147483647")
        if self.attempts < 0 or self.max_attempts < 1 or self.token < 0:
            raise ValueError("Лічильники задачі мають некоректне значення")

