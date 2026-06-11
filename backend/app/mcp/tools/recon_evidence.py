"""MCP tool: recon.get_evidence — fetch evidence pack for a run."""

from __future__ import annotations

import uuid


async def execute(params: dict, **kwargs) -> dict:
    """Get evidence pack download info for a reconciliation run.

    Params:
        run_id: Reconciliation run ID
    """
    run_id = params.get("run_id")
    if not run_id:
        return {"success": False, "error": "run_id is required"}

    # Family-uniform structured error (matches recon.get_exceptions /
    # recon.approve_match): never hand the LLM a download link embedding a
    # malformed id that can only 400 at the REST endpoint.
    try:
        uuid.UUID(str(run_id))
    except ValueError:
        return {"success": False, "error": f"run_id must be a valid UUID, got: {run_id!r}"}

    return {
        "success": True,
        "run_id": run_id,
        "download_url": f"/api/v1/reconciliation/evidence/{run_id}",
        "message": ("Evidence pack ready for download. Use the link or click the download button in the dashboard."),
    }
