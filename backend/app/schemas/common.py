from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


class AuditEventResponse(BaseModel):
    id: str
    tenant_id: str
    timestamp: str
    actor_id: str | None = None
    actor_type: str
    category: str
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    correlation_id: str | None = None
    job_id: str | None = None
    payload: dict | None = None
    status: str
    error_message: str | None = None

    model_config = {"from_attributes": True}


class JobResponse(BaseModel):
    id: str
    tenant_id: str
    job_type: str
    status: str
    correlation_id: str | None = None
    connection_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    parameters: dict | None = None
    result_summary: dict | None = None
    error_message: str | None = None
    celery_task_id: str | None = None

    model_config = {"from_attributes": True}


class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str
