import socket
import time
from contextlib import AbstractAsyncContextManager
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge, Histogram

from backend.application.referrals import ReferralService
from backend.domain.ports import ReadinessProbe
from backend.ui.api.routes.referrals import router as referrals_router
from backend.ui.api.routes.system import router as system_router


Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]

HTTP_REQUESTS = Counter(
    "http_requests_total", "Completed HTTP requests", ["method", "path", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request duration in seconds",
    ["method", "path"], buckets=(.005, .010, .025, .050, .100, .250, .500, 1.0),
)
HTTP_IN_FLIGHT = Gauge("http_requests_in_flight", "HTTP requests in progress")


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
        started = time.perf_counter()
        HTTP_IN_FLIGHT.inc()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Pod"] = request.app.state.instance_name
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
            HTTP_DURATION.labels(request.method, path).observe(time.perf_counter() - started)
            HTTP_IN_FLIGHT.dec()

    return app
