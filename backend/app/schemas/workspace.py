from __future__ import annotations

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


# --- Run / Artifact ---


class RunTriggerRequest(BaseModel):
    changeset_id: str | None = None


class RunResponse(BaseModel):
    id: str
    workspace_id: str
    changeset_id: str | None
    run_type: str
    status: str
    command: str | None
    exit_code: int | None
    started_at: str | None
    completed_at: str | None
    duration_ms: int | None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class ArtifactResponse(BaseModel):
    id: str
    run_id: str
    artifact_type: str
    content: str | None
    size_bytes: int
    sha256_hash: str | None
    created_at: str

    model_config = {"from_attributes": True}


# --- SuiteQL Assertions ---


class AssertionExpected(BaseModel):
    type: str = Field(..., description="row_count | scalar | no_rows")
    operator: str = Field("eq", description="eq | ne | gt | gte | lt | lte | between")
    value: float | int | None = None
    value2: float | int | None = None


class AssertionDefinition(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    query: str = Field(..., min_length=1, max_length=4096)
    expected: AssertionExpected
    notes: str | None = None
    tags: list[str] | None = None


class SuiteQLAssertionsRequest(BaseModel):
    assertions: list[AssertionDefinition] = Field(..., min_length=1, max_length=50)


# --- Deploy Sandbox ---


class DeploySandboxRequest(BaseModel):
    override_reason: str | None = Field(
        None,
        description="Admin override reason if prerequisites not fully met",
        max_length=1000,
    )
    require_assertions: bool = Field(
        False,
        description="Whether SuiteQL assertions must pass before deploy",
    )


# --- UAT Report ---


class UATReportResponse(BaseModel):
    changeset_id: str
    changeset_title: str
    changeset_status: str
    gates: dict
    runs: list[dict]
    assertions_report: dict | None
    overall_status: str
    generated_at: str
