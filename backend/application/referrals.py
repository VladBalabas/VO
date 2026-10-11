from dataclasses import dataclass

from backend.domain.entities import Referral, ReferralDraft, ReferralStatus
from backend.domain.ports import ReferralRepository


class ReferralNotFoundError(LookupError):
    def __init__(self, referral_id: int) -> None:
        super().__init__(f"Направлення {referral_id} не знайдено")
        self.referral_id = referral_id


@dataclass(frozen=True, slots=True)
class ReferralList:
    items: list[Referral]
    total: int
    limit: int
    offset: int


class ReferralService:
    def __init__(self, repository: ReferralRepository) -> None:
        self._repository = repository

    async def create(self, referral: ReferralDraft) -> Referral:
        return await self._repository.create(referral)

    async def list(
        self,
        limit: int,
        offset: int,
        status: ReferralStatus | None,
    ) -> ReferralList:
        items, total = await self._repository.list(limit, offset, status)
        return ReferralList(list(items), total, limit, offset)

    async def get(self, referral_id: int) -> Referral:
        referral = await self._repository.get(referral_id)
        if referral is None:
            raise ReferralNotFoundError(referral_id)
        return referral

    async def replace(self, referral_id: int, referral: ReferralDraft) -> Referral:
        updated = await self._repository.replace(referral_id, referral)
        if updated is None:
            raise ReferralNotFoundError(referral_id)
        return updated

    async def delete(self, referral_id: int) -> None:
        if not await self._repository.delete(referral_id):
            raise ReferralNotFoundError(referral_id)
