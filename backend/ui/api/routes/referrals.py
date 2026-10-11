from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from backend.application.referrals import ReferralNotFoundError
from backend.domain.entities import ReferralStatus
from backend.ui.api.dependencies import ReferralServiceDependency
from backend.ui.api.schemas import (
    ErrorResponse,
    ReferralCreate,
    ReferralListResponse,
    ReferralReplace,
    ReferralResponse,
)


router = APIRouter(prefix="/referrals", tags=["Направлення"])


def not_found(error: ReferralNotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail="Направлення не знайдено")


@router.post(
    "",
    response_model=ReferralResponse,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
)
async def create_referral(
    payload: ReferralCreate,
    service: ReferralServiceDependency,
) -> ReferralResponse:
    referral = await service.create(payload.to_domain())
    return ReferralResponse.from_domain(referral)


@router.get("", response_model=ReferralListResponse)
async def list_referrals(
    service: ReferralServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    referral_status: Annotated[ReferralStatus | None, Query(alias="status")] = None,
) -> ReferralListResponse:
    referrals = await service.list(limit, offset, referral_status)
    return ReferralListResponse.from_domain(referrals)


@router.get(
    "/{referral_id}",
    response_model=ReferralResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_referral(
    referral_id: int,
    service: ReferralServiceDependency,
) -> ReferralResponse:
    try:
        referral = await service.get(referral_id)
    except ReferralNotFoundError as error:
        raise not_found(error) from error
    return ReferralResponse.from_domain(referral)


@router.put(
    "/{referral_id}",
    response_model=ReferralResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def replace_referral(
    referral_id: int,
    payload: ReferralReplace,
    service: ReferralServiceDependency,
) -> ReferralResponse:
    try:
        referral = await service.replace(referral_id, payload.to_domain())
    except ReferralNotFoundError as error:
        raise not_found(error) from error
    return ReferralResponse.from_domain(referral)


@router.delete(
    "/{referral_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse}},
)
async def delete_referral(
    referral_id: int,
    service: ReferralServiceDependency,
) -> Response:
    try:
        await service.delete(referral_id)
    except ReferralNotFoundError as error:
        raise not_found(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
