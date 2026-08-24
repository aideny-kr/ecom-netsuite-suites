"""mcp_connector_service.create_mcp_connector — Celigo region-aware URL pinning.

FIX 2 (T2 gate round 3, PR #202): create_mcp_connector's provider=="celigo_mcp"
branch always resolved server_url to a single hardcoded US host, ignoring the
connection's region entirely -- so an EU tenant's agent_token got
authenticated against the US MCP host, discovery failed, and agent_access
silently ended up False. The pinning property itself (never trusting a
caller-supplied server_url for celigo_mcp -- see docstring in
mcp_connector_service.py) must survive; only the region derivation was wrong.
"""

from app.services.mcp_connector_service import create_mcp_connector


class TestCeligoMcpServerUrlRegionPinning:
    async def test_us_region_pins_us_host(self, db, admin_user):
        user, _ = admin_user
        connector = await create_mcp_connector(
            db=db,
            tenant_id=user.tenant_id,
            provider="celigo_mcp",
            label="Celigo (agent access)",
            server_url="https://attacker.example.com/mcp",
            auth_type="bearer",
            credentials={"token": "agent-tok"},
            region="us",
        )
        assert connector.server_url == "https://api.integrator.io/celigo-mcp"

    async def test_eu_region_pins_eu_host(self, db, admin_user):
        user, _ = admin_user
        connector = await create_mcp_connector(
            db=db,
            tenant_id=user.tenant_id,
            provider="celigo_mcp",
            label="Celigo (agent access)",
            server_url="https://attacker.example.com/mcp",
            auth_type="bearer",
            credentials={"token": "agent-tok"},
            region="eu",
        )
        assert connector.server_url == "https://api.eu.integrator.io/celigo-mcp"

    async def test_unknown_region_falls_back_to_us_host(self, db, admin_user):
        user, _ = admin_user
        connector = await create_mcp_connector(
            db=db,
            tenant_id=user.tenant_id,
            provider="celigo_mcp",
            label="Celigo (agent access)",
            server_url="https://attacker.example.com/mcp",
            auth_type="bearer",
            credentials={"token": "agent-tok"},
            region="xx",
        )
        assert connector.server_url == "https://api.integrator.io/celigo-mcp"

    async def test_omitted_region_defaults_to_us_host(self, db, admin_user):
        """Backward compat: a caller that doesn't pass region at all (the
        parameter must default sanely) resolves to the US host, exactly like
        celigo.client.base_url()'s own default."""
        user, _ = admin_user
        connector = await create_mcp_connector(
            db=db,
            tenant_id=user.tenant_id,
            provider="celigo_mcp",
            label="Celigo (agent access)",
            server_url="https://attacker.example.com/mcp",
            auth_type="bearer",
            credentials={"token": "agent-tok"},
        )
        assert connector.server_url == "https://api.integrator.io/celigo-mcp"

    async def test_caller_supplied_server_url_still_ignored_for_celigo_mcp(self, db, admin_user):
        """The anti-spoofing pin (closed by an earlier fix on this branch) must
        survive region-awareness: a caller cannot point a celigo_mcp connector
        at a server they control by passing server_url, regardless of region."""
        user, _ = admin_user
        connector = await create_mcp_connector(
            db=db,
            tenant_id=user.tenant_id,
            provider="celigo_mcp",
            label="Celigo (agent access)",
            server_url="https://attacker.example.com/mcp",
            auth_type="bearer",
            credentials={"token": "agent-tok"},
            region="eu",
        )
        assert connector.server_url != "https://attacker.example.com/mcp"
        assert connector.server_url == "https://api.eu.integrator.io/celigo-mcp"

    async def test_non_celigo_provider_keeps_caller_server_url(self, db, admin_user):
        """Region pinning is scoped to provider == 'celigo_mcp' only -- every
        other provider's server_url passes through unchanged."""
        user, _ = admin_user
        connector = await create_mcp_connector(
            db=db,
            tenant_id=user.tenant_id,
            provider="custom",
            label="Custom",
            server_url="https://example.com/mcp",
            auth_type="none",
            region="eu",
        )
        assert connector.server_url == "https://example.com/mcp"
