from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.application.referrals import ReferralList
from backend.application.tasks import TaskList
from backend.domain.entities import Modality, Referral, ReferralDraft, ReferralStatus
from backend.domain.tasks import ProcessingTask, TaskStatus


class ReferralPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_code: str = Field(min_length=1, max_length=64)
    modality: Modality
    priority: int = Field(ge=1, le=5)
    doctor: str = Field(min_length=1, max_length=120)
    issued_at: date
    status: ReferralStatus

    @field_validator("patient_code", "doctor")
    @classmethod
    def strip_and_validate_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Поле не може бути порожнім")
        return value

    def to_domain(self) -> ReferralDraft:
        return ReferralDraft(**self.model_dump())


class ReferralCreate(ReferralPayload):
    pass


class ReferralReplace(ReferralPayload):
    pass


class ReferralResponse(ReferralPayload):
    id: int

    @classmethod
    def from_domain(cls, referral: Referral) -> "ReferralResponse":
        return cls(
            id=referral.id,
            patient_code=referral.patient_code,
            modality=referral.modality,
            priority=referral.priority,
            doctor=referral.doctor,
            issued_at=referral.issued_at,
            status=referral.status,
        )


class ReferralListResponse(BaseModel):
    items: list[ReferralResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)

    @classmethod
    def from_domain(cls, referrals: ReferralList) -> "ReferralListResponse":
        return cls(
            items=[ReferralResponse.from_domain(item) for item in referrals.items],
            total=referrals.total,
            limit=referrals.limit,
            offset=referrals.offset,
        )


class ErrorResponse(BaseModel):
    detail: object


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: int = Field(default=2, ge=1, le=3)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


class TaskResponse(BaseModel):
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
    result: dict[str, Any] | None

    @classmethod
    def from_domain(cls, task: ProcessingTask) -> "TaskResponse":
        return cls(**{field: getattr(task, field) for field in cls.model_fields})


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)

    @classmethod
    def from_domain(cls, tasks: TaskList) -> "TaskListResponse":
        return cls(
            items=[TaskResponse.from_domain(item) for item in tasks.items],
            total=tasks.total,
            limit=tasks.limit,
            offset=tasks.offset,
        )
