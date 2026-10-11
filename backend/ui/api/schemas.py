from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.application.referrals import ReferralList
from backend.domain.entities import Modality, Referral, ReferralDraft, ReferralStatus


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
