"""MCP tool: recon.get_evidence — fetch evidence pack for a run."""

from __future__ import annotations


async def execute(params: dict, **kwargs) -> dict:
    """Get evidence pack download info for a reconciliation run.

    Params:
        run_id: Reconciliation run ID
    """
    run_id = params.get("run_id")
    if not run_id:
        return {"success": False, "error": "run_id is required"}

    return {
        "success": True,
        "run_id": run_id,
        "download_url": f"/api/v1/reconciliation/evidence/{run_id}",
        "message": (
            "Evidence pack ready for download. "
            "Use the link or click the download button in the dashboard."
        ),
    }
