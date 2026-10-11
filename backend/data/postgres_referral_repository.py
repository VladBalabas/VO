from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from backend.data.database import AsyncpgDatabase
from backend.domain.entities import Modality, Referral, ReferralDraft, ReferralStatus


def _to_domain(record: Mapping[str, Any]) -> Referral:
    return Referral(
        id=int(record["id"]),
        patient_code=str(record["patient_code"]),
        modality=Modality(record["modality"]),
        priority=int(record["priority"]),
        doctor=str(record["doctor"]),
        issued_at=record["issued_at"],
        status=ReferralStatus(record["status"]),
    )


class PostgresReferralRepository:
    def __init__(self, database: AsyncpgDatabase) -> None:
        self._database = database

    async def create(self, referral: ReferralDraft) -> Referral:
        record = await self._database.pool.fetchrow(
            """
            INSERT INTO referrals (
                patient_code, modality, priority, doctor, issued_at, status
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, patient_code, modality, priority, doctor, issued_at, status
            """,
            referral.patient_code,
            referral.modality,
            referral.priority,
            referral.doctor,
            referral.issued_at,
            referral.status,
        )
        if record is None:
            raise RuntimeError("PostgreSQL не повернув створене направлення")
        return _to_domain(record)

    async def list(
        self,
        limit: int,
        offset: int,
        status: ReferralStatus | None,
    ) -> tuple[Sequence[Referral], int]:
        pool = self._database.pool
        if status is None:
            records = await pool.fetch(
                """
                SELECT id, patient_code, modality, priority, doctor, issued_at, status
                FROM referrals
                ORDER BY id
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
            total = await pool.fetchval("SELECT COUNT(*) FROM referrals")
        else:
            records = await pool.fetch(
                """
                SELECT id, patient_code, modality, priority, doctor, issued_at, status
                FROM referrals
                WHERE status = $1
                ORDER BY id
                LIMIT $2 OFFSET $3
                """,
                status,
                limit,
                offset,
            )
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM referrals WHERE status = $1",
                status,
            )
        return [_to_domain(record) for record in records], int(total)

    async def get(self, referral_id: int) -> Referral | None:
        record = await self._database.pool.fetchrow(
            """
            SELECT id, patient_code, modality, priority, doctor, issued_at, status
            FROM referrals
            WHERE id = $1
            """,
            referral_id,
        )
        return None if record is None else _to_domain(record)

    async def replace(
        self,
        referral_id: int,
        referral: ReferralDraft,
    ) -> Referral | None:
        record = await self._database.pool.fetchrow(
            """
            UPDATE referrals
            SET patient_code = $2,
                modality = $3,
                priority = $4,
                doctor = $5,
                issued_at = $6,
                status = $7
            WHERE id = $1
            RETURNING id, patient_code, modality, priority, doctor, issued_at, status
            """,
            referral_id,
            referral.patient_code,
            referral.modality,
            referral.priority,
            referral.doctor,
            referral.issued_at,
            referral.status,
        )
        return None if record is None else _to_domain(record)

    async def delete(self, referral_id: int) -> bool:
        deleted_id = await self._database.pool.fetchval(
            "DELETE FROM referrals WHERE id = $1 RETURNING id",
            referral_id,
        )
        return deleted_id is not None
