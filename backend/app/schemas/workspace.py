from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


# --- Workspace ---

class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None


class WorkspaceResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None
    status: str
    created_by: str
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


# --- File Tree ---

class FileTreeNode(BaseModel):
    id: str
    name: str
    path: str
    is_directory: bool
    children: list[FileTreeNode] | None = None
    size_bytes: int | None = None


class FileReadResponse(BaseModel):
    id: str
    path: str
    file_name: str
    content: str
    truncated: bool
    total_lines: int
    mime_type: str | None


# --- Search ---

class SearchResult(BaseModel):
    file_id: str
    path: str
    line_number: int
    snippet: str
    context: str


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int


# --- Changeset ---

class ChangeSetCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None


class ChangeSetTransition(BaseModel):
    action: str = Field(..., description="submit, approve, reject, revert, revoke")
    rejection_reason: str | None = None


class PatchResponse(BaseModel):
    id: str
    changeset_id: str
    file_path: str
    operation: str
    unified_diff: str | None
    new_content: str | None
    baseline_sha256: str
    apply_order: int
    created_at: str

    model_config = {"from_attributes": True}


class ChangeSetResponse(BaseModel):
    id: str
    workspace_id: str
    title: str
    description: str | None
    status: str
    proposed_by: str
    reviewed_by: str | None
    applied_by: str | None
    proposed_at: str
    reviewed_at: str | None
    applied_at: str | None
    rejection_reason: str | None
    created_at: str
    updated_at: str
    patches: list[PatchResponse] | None = None

    model_config = {"from_attributes": True}


class DiffFileView(BaseModel):
    file_path: str
    operation: str
    original_content: str
    modified_content: str


class DiffViewResponse(BaseModel):
    changeset_id: str
    title: str
    files: list[DiffFileView]


# --- Patch Proposal ---

class PatchProposal(BaseModel):
    file_path: str = Field(..., max_length=512)
    unified_diff: str = Field(..., max_length=262144)  # 256KB
    title: str = Field(..., min_length=1, max_length=500)
    rationale: str | None = None
