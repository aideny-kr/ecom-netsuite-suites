"""recon.get_evidence param validation — structured errors, family-uniform.

Codex (independent-model delta review, PR #130 R4) found the sibling gap:
recon.get_exceptions / recon.approve_match return a structured
``{"success": False, "error": ...}`` for malformed UUIDs, but
recon.get_evidence returned ``success: True`` with a download URL embedding
the malformed id — handing the LLM a link that can only 400 downstream.
No DB session needed: the tool only validates + builds the URL.
"""

import pytest

from app.mcp.tools import recon_evidence


@pytest.mark.asyncio
async def test_missing_run_id_returns_structured_error():
    result = await recon_evidence.execute({})
    assert result["success"] is False
    assert "run_id is required" in result["error"]


@pytest.mark.asyncio
async def test_malformed_run_id_returns_structured_error_not_a_url():
    result = await recon_evidence.execute({"run_id": "not-a-uuid"})
    assert result["success"] is False
    assert "run_id" in result["error"]
    # Never hand the LLM a download link for an id that can only 400.
    assert "download_url" not in result


@pytest.mark.asyncio
async def test_valid_run_id_returns_download_url():
    run_id = "12345678-1234-5678-1234-567812345678"
    result = await recon_evidence.execute({"run_id": run_id})
    assert result["success"] is True
    assert result["download_url"] == f"/api/v1/reconciliation/evidence/{run_id}"
