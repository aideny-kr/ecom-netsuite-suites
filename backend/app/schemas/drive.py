from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DriveFolderCreate(BaseModel):
    folder_id_or_url: str = Field(min_length=1, max_length=1024)

    @field_validator("folder_id_or_url")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("folder_id_or_url is required")
        return v


class DriveFolderUpdate(BaseModel):
    is_enabled: bool


class DriveFolderResponse(BaseModel):
    id: str
    tenant_id: str
    folder_id: str
    folder_name: str
    is_enabled: bool
    sync_status: Literal["idle", "syncing", "success", "error"]
    last_synced_at: datetime | None
    last_sync_error: str | None
    chunk_count: int
    file_count: int
    created_at: datetime


class DriveFolderStatus(BaseModel):
    id: str
    sync_status: Literal["idle", "syncing", "success", "error"]
    last_synced_at: datetime | None
    last_sync_error: str | None
    chunk_count: int
    file_count: int
