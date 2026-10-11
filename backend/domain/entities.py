from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Modality(StrEnum):
    CT = "CT"
    MRI = "MRI"
    XR = "XR"
    US = "US"
    PET = "PET"


class ReferralStatus(StrEnum):
    CREATED = "created"
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ReferralDraft:
    patient_code: str
    modality: Modality
    priority: int
    doctor: str
    issued_at: date
    status: ReferralStatus

    def __post_init__(self) -> None:
        patient_code = self.patient_code.strip()
        doctor = self.doctor.strip()
        if not patient_code or len(patient_code) > 64:
            raise ValueError("Код пацієнта має містити від 1 до 64 символів")
        if not doctor or len(doctor) > 120:
            raise ValueError("Ім'я лікаря має містити від 1 до 120 символів")
        if not 1 <= self.priority <= 5:
            raise ValueError("Пріоритет має бути в діапазоні від 1 до 5")
        object.__setattr__(self, "patient_code", patient_code)
        object.__setattr__(self, "doctor", doctor)


@dataclass(frozen=True, slots=True)
class Referral:
    id: int
    patient_code: str
    modality: Modality
    priority: int
    doctor: str
    issued_at: date
    status: ReferralStatus

    def __post_init__(self) -> None:
        if self.id < 1:
            raise ValueError("Ідентифікатор направлення має бути додатним")
        normalized = ReferralDraft(
            patient_code=self.patient_code,
            modality=self.modality,
            priority=self.priority,
            doctor=self.doctor,
            issued_at=self.issued_at,
            status=self.status,
        )
        object.__setattr__(self, "patient_code", normalized.patient_code)
        object.__setattr__(self, "doctor", normalized.doctor)
