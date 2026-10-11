from typing import Annotated

from fastapi import Depends, Request

from backend.application.referrals import ReferralService
from backend.domain.ports import ReadinessProbe


def get_referral_service(request: Request) -> ReferralService:
    return request.app.state.referral_service


def get_readiness_probe(request: Request) -> ReadinessProbe:
    return request.app.state.readiness_probe


ReferralServiceDependency = Annotated[ReferralService, Depends(get_referral_service)]
ReadinessProbeDependency = Annotated[ReadinessProbe, Depends(get_readiness_probe)]
