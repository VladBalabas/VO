from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from backend.application.tasks import ReferralForTaskNotFoundError, TaskNotFoundError
from backend.domain.tasks import TaskStatus
from backend.ui.api.dependencies import TaskServiceDependency
from backend.ui.api.schemas import ErrorResponse, TaskCreate, TaskListResponse, TaskResponse


router = APIRouter(tags=["Задачі обробки"])
BigIntId = Annotated[int, Path(ge=1, le=2**63 - 1)]


@router.post(
    "/referrals/{referral_id}/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def create_task(
    referral_id: BigIntId,
    payload: TaskCreate,
    service: TaskServiceDependency,
) -> TaskResponse:
    try:
        task = await service.create(referral_id, payload.priority, payload.seed)
    except ReferralForTaskNotFoundError as error:
        raise HTTPException(status_code=404, detail="Направлення не знайдено") from error
    return TaskResponse.from_domain(task)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def get_task(task_id: BigIntId, service: TaskServiceDependency) -> TaskResponse:
    try:
        task = await service.get(task_id)
    except TaskNotFoundError as error:
        raise HTTPException(status_code=404, detail="Задачу не знайдено") from error
    return TaskResponse.from_domain(task)


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    service: TaskServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
) -> TaskListResponse:
    tasks = await service.list(limit, offset, task_status)
    return TaskListResponse.from_domain(tasks)

