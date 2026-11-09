import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.application.referrals import ReferralService
from backend.application.tasks import TaskService
from backend.data.database import AsyncpgDatabase
from backend.data.postgres_referral_repository import PostgresReferralRepository
from backend.data.postgres_task_repository import PostgresTaskRepository
from backend.data.settings import load_settings
from backend.ui.api.factory import create_http_app


def build_application() -> FastAPI:
    database = AsyncpgDatabase(load_settings())
    repository = PostgresReferralRepository(database)
    referral_service = ReferralService(repository)
    task_service = TaskService(PostgresTaskRepository(database))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await database.connect()
        stop_maintenance = asyncio.Event()
        maintenance = asyncio.create_task(task_service.maintenance_loop(stop_maintenance))
        try:
            yield
        finally:
            stop_maintenance.set()
            await maintenance
            await database.disconnect()

    return create_http_app(
        referral_service=referral_service,
        task_service=task_service,
        readiness_probe=database,
        lifespan=lifespan,
        instance_name=os.getenv("POD_NAME"),
    )


app = build_application()
