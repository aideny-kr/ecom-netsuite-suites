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
PROVIDER_TO_CANONICAL_SOURCE: dict[str, str] = {
    # NetSuite — both MCP and REST count as "netsuite"
    "netsuite_mcp": "netsuite",
    "netsuite": "netsuite",
    # BigQuery
    "bigquery": "bigquery",
    # Shopify — both MCP and direct API
    "shopify_mcp": "shopify",
    "shopify": "shopify",
    # Stripe — both MCP and direct REST count as "stripe"
    "stripe": "stripe",
    "stripe_mcp": "stripe",
    # Google Drive RAG reuses the google_sheets MCP connector for OAuth
    "google_sheets": "drive",
    "drive": "drive",
}


def canonicalize_connector_providers(active_connectors: list[str]) -> set[str]:
    """Translate raw provider strings into the canonical clarify-source set.

    Unknown providers (e.g., ``custom``) are dropped silently — they cannot
    satisfy the clarify schema's ``source`` enum.
    """
    return {PROVIDER_TO_CANONICAL_SOURCE[p] for p in active_connectors if p in PROVIDER_TO_CANONICAL_SOURCE}
