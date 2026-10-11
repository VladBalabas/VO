from collections.abc import Sequence
from typing import Protocol

from backend.domain.entities import Referral, ReferralDraft, ReferralStatus


class ReferralRepository(Protocol):
    async def create(self, referral: ReferralDraft) -> Referral: ...

    async def list(
        self,
        limit: int,
        offset: int,
        status: ReferralStatus | None,
    ) -> tuple[Sequence[Referral], int]: ...

    async def get(self, referral_id: int) -> Referral | None: ...

    async def replace(
        self,
        referral_id: int,
        referral: ReferralDraft,
    ) -> Referral | None: ...

    async def delete(self, referral_id: int) -> bool: ...


class ReadinessProbe(Protocol):
    async def is_ready(self) -> bool: ...
