"""An unrecognised NetSuite tool must fail CLOSED, not sail through.

`classify_mutation` recognises exactly four NetSuite write verbs
(mutation_guard.py: ns_createRecord / ns_updateRecord / ns_deleteRecord /
ns_upsertRecord). That is a DENY-list, and the NetSuite tool surface is
DISCOVERED AT RUNTIME from Oracle's MCP server (`session.list_tools()`,
mcp_client_service.py:164) — so Oracle can expose a write tool tomorrow that
this list has never heard of.

Such a tool would pass the HITL guard, reach the ERP, and mutate a production
record with no confirmation card. `.claude/rules/agent-graph.md` names the rule
being broken: "allow-list derived from a registry, never a deny-list".

So for NetSuite connectors the dispatcher inverts the default: a tool is
dispatchable only if it is on the known READ-ONLY allow-list. Anything else —
new, renamed, or simply unrecognised — requires human approval like any other
write.

The trade is deliberate and asymmetric. A new READ tool being refused is
visible, recoverable, and loudly logged. A new WRITE tool being allowed is an
unapproved, irreversible ERP mutation that nobody sees until afterwards.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chat import tools as tools_mod

_HEX = "b" * 32


def _ext(name: str) -> str:
    return f"ext__{_HEX}__{name}"


class _Conn:
    id = uuid.UUID(int=1)
    provider = "netsuite_mcp"
    is_enabled = True
    server_url = "https://example.com/mcp"
    auth_type = "none"
    encrypted_credentials = None
    metadata_json = {"account_id": "6738075"}


async def _dispatch(tool_name: str, connector=None, **kw):
    # get_mcp_connector is imported INSIDE _execute_external_tool, so it must be
    # patched at its source module rather than on `tools`.
    with patch(
        "app.services.mcp_connector_service.get_mcp_connector",
        AsyncMock(return_value=connector or _Conn()),
    ):
        return await tools_mod.execute_tool_call(
            tool_name=tool_name,
            tool_input={},
            tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            correlation_id="c",
            db=None,
            session_id="s",
            **kw,
        )


@pytest.mark.parametrize(
    "unknown_write",
    ["ns_transformRecord", "ns_submitFields", "ns_attachRecord", "ns_voidTransaction"],
)
@pytest.mark.asyncio
async def test_unknown_netsuite_tool_is_refused(unknown_write):
    """None of these are in the 4-name deny-list. All of them write."""
    out = json.loads(await _dispatch(_ext(unknown_write)))
    assert out.get("hitl_required") is True, f"{unknown_write} must not dispatch unapproved"


@pytest.mark.asyncio
async def test_known_read_tools_still_dispatch():
    """The allow-list must cover everything the product actually reads with,
    or validation, the slot form and the posting invariants all break."""
    for read_tool in (
        "ns_getRecord",
        "ns_getRecordTypeMetadata",
        "ns_getSubsidiaries",
        "ns_getSuiteQLMetadata",
        "ns_runCustomSuiteQL",
        "ns_runReport",
        "ns_runSavedSearch",
        "ns_listAllReports",
        "ns_listSavedSearches",
        "ns_getAccountingBooks",
        "ns_getAccountingContexts",
        "ns_getNexusIds",
        "ns_selector_app",
        "ns_report_filters_app",
        "ns_prompt_library_app",
    ):
        out = await _dispatch(_ext(read_tool))
        assert "hitl_required" not in out, f"{read_tool} must dispatch freely"


@pytest.mark.asyncio
async def test_an_approved_unknown_tool_still_dispatches():
    """Fail-closed, not fail-permanently: an operator can still approve it."""
    out = await _dispatch(_ext("ns_transformRecord"), human_approved=True)
    assert "hitl_required" not in out


@pytest.mark.asyncio
async def test_non_netsuite_providers_are_not_allow_listed():
    """The inversion is scoped to NetSuite. Celigo has its own read-only
    policy; applying a NetSuite allow-list to it would refuse every Celigo
    read."""

    class _Celigo(_Conn):
        provider = "celigo_mcp"

    out = await _dispatch(_ext("list_flows"), connector=_Celigo())
    assert "hitl_required" not in out
