"""Celigo must NOT be reachable through the generic connection/connector schemas.

Round 1 of the T2 gate widened these regexes to admit "celigo"/"celigo_mcp" so the
generic create endpoints (POST /connections, POST /mcp-connectors) would accept a
Celigo row. Round 2 found that widening was itself the bug: those generic paths
know nothing about the celigo feature flag, verify_token-before-write, server_url
pinning, or the verified-before-enabled invariant that
app/api/v1/connector_status.py enforces. Every one of those invariants had leaked
onto the generic paths identically -- the widened regex was the single mechanism
making all of them reachable at once.

Celigo's dedicated flow (connector_status.py) never goes through these schemas at
all -- it builds `Connection(...)` directly and calls
`mcp_connector_service.create_mcp_connector(...)` with kwargs. So narrowing these
regexes back closes the generic bypass with zero effect on the feature: Celigo
stays reachable only through connect_celigo/disconnect_celigo, which is the point.
"""

import pytest
from pydantic import ValidationError

from app.schemas.connection import ConnectionCreate
from app.schemas.mcp_connector import McpConnectorCreate


class TestCeligoRestProviderRejectedGenerically:
    def test_celigo_rejected(self):
        """celigo must only ever be created via connect_celigo's own Connection(...)
        construction -- never through the generic ConnectionCreate schema, which
        has no feature-flag gate, no verify_token-before-write, and no reconnect
        semantics."""
        with pytest.raises(ValidationError):
            ConnectionCreate(provider="celigo", label="Celigo Production", credentials={})

    def test_existing_providers_still_accepted(self):
        for p in ("shopify", "stripe", "netsuite"):
            assert ConnectionCreate(provider=p, label="x", credentials={}).provider == p

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValidationError):
            ConnectionCreate(provider="celigo_typo", label="x", credentials={})


class TestCeligoMcpProviderRejectedGenerically:
    def test_celigo_mcp_rejected(self):
        """celigo_mcp must only ever be created via
        connector_status._upsert_celigo_mcp_connector, which calls
        mcp_connector_service.create_mcp_connector directly (bypassing this
        schema) and pins server_url/auth_type/credentials itself. The generic
        McpConnectorCreate schema has no such pinning and no verified-before-
        enabled gate -- admitting celigo_mcp here was the only thing letting
        POST /mcp-connectors register one at all."""
        with pytest.raises(ValidationError):
            McpConnectorCreate(
                provider="celigo_mcp",
                label="Celigo agent access",
                server_url="https://api.integrator.io/celigo-mcp",
                auth_type="bearer",
            )

    def test_existing_mcp_providers_still_accepted(self):
        for p in ("netsuite_mcp", "shopify_mcp", "stripe_mcp", "custom"):
            assert McpConnectorCreate(provider=p, label="x").provider == p

    def test_unknown_mcp_provider_rejected(self):
        with pytest.raises(ValidationError):
            McpConnectorCreate(provider="celigo", label="x")
