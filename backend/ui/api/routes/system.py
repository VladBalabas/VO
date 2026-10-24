from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from backend.ui.api.dependencies import ReadinessProbeDependency


router = APIRouter()


@router.get("/livez", include_in_schema=False)
async def livez():
    return {"status": "alive"}


@router.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/healthz", tags=["Стан сервісу"])
async def healthz(readiness: ReadinessProbeDependency):
    if await readiness.is_ready():
        return {"status": "готовий"}
    return JSONResponse(
        status_code=503,
        content={"status": "база даних недоступна"},
    )


@router.get("/bench/noop", include_in_schema=False)
async def benchmark_noop():
    return {"status": "ok"}
