"""Provider regexes must accept the two named Celigo providers.

The spec chose named providers over overriding provider="custom" because region,
sandbox scope, tokenInfo health checks, and the flow-mapper entry point all
branch on the provider string.
"""

import pytest
from pydantic import ValidationError

from app.schemas.connection import ConnectionCreate
from app.schemas.mcp_connector import McpConnectorCreate


class TestCeligoRestProvider:
    def test_celigo_accepted(self):
        c = ConnectionCreate(provider="celigo", label="Celigo Production", credentials={})
        assert c.provider == "celigo"

    def test_existing_providers_still_accepted(self):
        for p in ("shopify", "stripe", "netsuite"):
            assert ConnectionCreate(provider=p, label="x", credentials={}).provider == p

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValidationError):
            ConnectionCreate(provider="celigo_typo", label="x", credentials={})


class TestCeligoMcpProvider:
    def test_celigo_mcp_accepted(self):
        c = McpConnectorCreate(
            provider="celigo_mcp",
            label="Celigo agent access",
            server_url="https://api.integrator.io/celigo-mcp",
            auth_type="bearer",
        )
        assert c.provider == "celigo_mcp"

    def test_existing_mcp_providers_still_accepted(self):
        for p in ("netsuite_mcp", "shopify_mcp", "stripe_mcp", "custom"):
            assert McpConnectorCreate(provider=p, label="x").provider == p

    def test_unknown_mcp_provider_rejected(self):
        with pytest.raises(ValidationError):
            McpConnectorCreate(provider="celigo", label="x")
