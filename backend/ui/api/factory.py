import socket
from contextlib import AbstractAsyncContextManager
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.application.referrals import ReferralService
from backend.domain.ports import ReadinessProbe
from backend.ui.api.routes.referrals import router as referrals_router
from backend.ui.api.routes.system import router as system_router


Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_http_app(
    referral_service: ReferralService,
    readiness_probe: ReadinessProbe,
    lifespan: Lifespan,
    instance_name: str | None = None,
) -> FastAPI:
    app = FastAPI(
        title="CRUD-сервіс направлень на дослідження",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.referral_service = referral_service
    app.state.readiness_probe = readiness_probe
    app.state.instance_name = instance_name or socket.gethostname()

    app.include_router(referrals_router)
    app.include_router(system_router)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request,
        exception: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": jsonable_encoder(exception.errors())},
        )

    @app.middleware("http")
    async def add_instance_header(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Pod"] = request.app.state.instance_name
        return response

    return app
