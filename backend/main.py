import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.application.referrals import ReferralService
from backend.data.database import AsyncpgDatabase
from backend.data.postgres_referral_repository import PostgresReferralRepository
from backend.data.settings import load_settings
from backend.ui.api.factory import create_http_app


def build_application() -> FastAPI:
    database = AsyncpgDatabase(load_settings())
    repository = PostgresReferralRepository(database)
    referral_service = ReferralService(repository)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await database.connect()
        try:
            yield
        finally:
            await database.disconnect()

    return create_http_app(
        referral_service=referral_service,
        readiness_probe=database,
        lifespan=lifespan,
        instance_name=os.getenv("POD_NAME"),
    )


app = build_application()
