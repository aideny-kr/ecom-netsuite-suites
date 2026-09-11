"""Shared canonical-source resolution for Plan Mode.

Both the clarify intercept (mint path) and the resume tool filter (consume
path) need to translate raw provider strings — as they appear in
``mcp_connectors.provider`` and ``connections.provider`` — into the bare
canonical source names used by the clarify schema enum (``netsuite``,
``bigquery``, ``shopify``, ``stripe``, ``drive``).

Keeping this map in one place prevents drift: if a new provider is added
(or an alias changes), updating it here propagates to both call sites.
"""

from __future__ import annotations

# Provider-string → canonical clarify-schema source.
#
# The clarify tool's ``source`` enum (see ``clarify_tool.py``) uses bare
# names: ``netsuite, bigquery, shopify, stripe, drive``. But callers pass
# ``active_connectors`` as raw provider strings from two tables:
#
#   - ``mcp_connectors.provider``: ``netsuite_mcp, shopify_mcp, bigquery,
#     google_sheets, custom``
#   - ``connections.provider``: ``netsuite, shopify, stripe``
#
# Drive is a special case: there is no ``drive`` provider row. Drive RAG
# auth piggybacks on the ``google_sheets`` MCP connector (see
# ``app/api/v1/drive_folders.py:73``). We treat ``google_sheets`` as
# evidence Drive is reachable until Drive gets its own provider row.
#
# Round 8 Bug 2: a provider only canonicalizes if it actually has chat
# tools the resume-turn filter can preserve. REST shopify/stripe
# (``connections.provider == 'shopify' / 'stripe'``) are
# reconciliation-only — they have NO local chat tools and no ext__ MCP
# tools. If they canonicalized, clarify intercept would advertise
# ``source='stripe'`` for a tenant with only REST Stripe, the user
# would pick it, the resume turn would filter tools to chosen-source,
# and the agent would be left with zero source-specific tools — stuck.
# So those bare REST entries are intentionally absent below.
PROVIDER_TO_CANONICAL_SOURCE: dict[str, str] = {
    # NetSuite — both MCP and REST have local chat tools (netsuite_*),
    # so both count as "netsuite".
    "netsuite_mcp": "netsuite",
    "netsuite": "netsuite",
    # BigQuery — MCP only (no REST analogue).
    "bigquery": "bigquery",
    "metabase": "metabase",
    "metabase_mcp": "metabase",
    # Shopify — MCP only. REST 'shopify' has no chat tools (Round 8 Bug 2).
    "shopify_mcp": "shopify",
    # Stripe — MCP only. REST 'stripe' is reconciliation-only, no chat
    # tools (Round 8 Bug 2).
    "stripe_mcp": "stripe",
    # Google Drive RAG reuses the google_sheets MCP connector for OAuth
    "google_sheets": "drive",
    "drive": "drive",
}


def source_provider_for_connector(connector) -> str:
    """Recognize native Metabase connections stored under the custom provider."""
    from app.services.chat.metabase_context import is_metabase_connector

    return "metabase_mcp" if is_metabase_connector(connector) else getattr(connector, "provider", "")


def canonicalize_connector_providers(active_connectors: list[str]) -> set[str]:
    """Translate raw provider strings into the canonical clarify-source set.

    Unknown providers (e.g., ``custom``) are dropped silently — they cannot
    satisfy the clarify schema's ``source`` enum.
    """
    return {PROVIDER_TO_CANONICAL_SOURCE[p] for p in active_connectors if p in PROVIDER_TO_CANONICAL_SOURCE}
