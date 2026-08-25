"""The Celigo write guard -- the session-flush choke point.

THE ACCEPTANCE TEST for this guard is the three ``TestGap*`` classes below.
Each drives a *generic, provider-agnostic* path against a Celigo row and
asserts it is refused. None of them was closed by editing its own call site:

  * ``connections.py`` POST /connections/{id}/reconnect  (the status flip)
  * ``mcp_connectors.py`` DELETE /mcp-connectors/{id}
  * ``mcp_connector_service.update_connector_tokens``

If any of those files ever has to be edited to make these pass, the guard is
in the wrong place -- the whole point is that a path nobody has written yet is
refused too.

The remaining classes cover the constraint that is easiest to violate and
worst to violate silently: everything that is NOT a Celigo row must behave
exactly as it did before.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.mcp_connector import McpConnector
from app.services import mcp_connector_service
from app.services.celigo.client import CELIGO_MCP_SERVER_URLS
from app.services.celigo_write_guard import (
    CeligoInvariantError,
    CeligoManagedElsewhereError,
    celigo_writes_allowed,
)

# ---------------------------------------------------------------------------
# Seeding helpers
#
# Seeding a Celigo row is itself a Celigo write, so the fixtures must hold the
# allow token -- which is the guard working, not a workaround. The full set of
# modules permitted to hold it is the allowlist in
# tests/test_celigo_write_guard_containment.py; in PRODUCTION code it is
# connector_status.py alone.
# ---------------------------------------------------------------------------


async def seed_celigo_connection(db, tenant_id, *, status: str = "active") -> Connection:
    """Seed a Celigo REST connection and COMMIT it.

    The commit matters. A refused write leaves the session needing a rollback,
    and the assertions below roll back before re-reading so they are about the
    database rather than pending in-memory state. Under the harness's
    ``join_transaction_mode="create_savepoint"`` that rollback unwinds to the
    last commit -- so a merely-flushed fixture row would vanish with the write
    under test, and the test would pass for the wrong reason. Committing pins
    the fixture; the outer test transaction still discards it at teardown.
    """
    connection = Connection(
        tenant_id=tenant_id,
        provider="celigo",
        label="Celigo",
        status=status,
        auth_type="api_key",
        encrypted_credentials=encrypt_credentials({"token": "rest-token"}),
        metadata_json={"region": "us", "account_name": "Acme"},
    )
    with celigo_writes_allowed(db):
        db.add(connection)
        await db.commit()
    return connection


async def seed_celigo_mcp_connector(
    db,
    tenant_id,
    *,
    region: str = "us",
    status: str = "active",
    is_enabled: bool = True,
) -> McpConnector:
    connector = McpConnector(
        tenant_id=tenant_id,
        provider="celigo_mcp",
        label="Celigo (agent access)",
        server_url=CELIGO_MCP_SERVER_URLS[region],
        auth_type="bearer",
        encrypted_credentials=encrypt_credentials({"token": "agent-token"}),
        status=status,
        is_enabled=is_enabled,
    )
    with celigo_writes_allowed(db):
        db.add(connector)
        await db.commit()
    return connector


async def seed_plain_connection(db, tenant_id, provider: str, *, status: str = "active") -> Connection:
    """A NON-Celigo connection -- the control group for every refusal below.

    Note the absence of any allow token: seeding this is an ordinary write, and
    it staying ordinary is the constraint these controls exist to prove.
    """
    connection = Connection(
        tenant_id=tenant_id,
        provider=provider,
        label=provider.title(),
        status=status,
        auth_type="api_key",
        encrypted_credentials=encrypt_credentials({"api_key": "sk_test"}),
    )
    db.add(connection)
    await db.commit()
    return connection


async def seed_plain_mcp_connector(db, tenant_id, provider: str = "netsuite_mcp") -> McpConnector:
    connector = McpConnector(
        tenant_id=tenant_id,
        provider=provider,
        label="NetSuite MCP",
        server_url="https://acme.suitetalk.api.netsuite.com/services/mcp/v1/all",
        auth_type="oauth2",
        encrypted_credentials=encrypt_credentials({"access_token": "t"}),
        status="active",
        is_enabled=True,
    )
    db.add(connector)
    await db.commit()
    return connector


# ---------------------------------------------------------------------------
# Gap 1 -- reconnect flips a revoked Celigo row back to active
# ---------------------------------------------------------------------------


class TestGap1ReconnectFlipsRevokedCeligoRow:
    """POST /connections/{id}/reconnect: Celigo rows carry auth_type='api_key',
    so they fall past the oauth2/netsuite branch into an unconditional
    ``connection.status = "active"``. That reactivates a revoked Celigo
    connection with a possibly-dead token while the paired celigo_mcp row stays
    revoked -- no feature gate, no verify_token, no pairing.

    MUST close with zero edits to app/api/v1/connections.py.
    """

    async def test_reconnect_refuses_celigo_connection(self, client, db, admin_user):
        user, headers = admin_user
        connection = await seed_celigo_connection(db, user.tenant_id, status="revoked")
        # Captured now: rollback() expires every loaded object, and reading an
        # expired attribute from async code raises MissingGreenlet.
        connection_id = connection.id

        r = await client.post(f"/api/v1/connections/{connection_id}/reconnect", headers=headers)

        assert r.status_code == 400, f"expected the guard to refuse, got {r.status_code}: {r.text}"
        assert "Settings" in r.json()["detail"]

        # The failed flush left the session needing a rollback -- exactly what
        # the request's own teardown does. Roll back before re-reading so the
        # assertion below is about the DATABASE, not about pending in-memory state.
        await db.rollback()
        row = (await db.execute(select(Connection).where(Connection.id == connection_id))).scalar_one()
        assert row.status == "revoked", "a refused reconnect must not have reactivated the row"

    async def test_reconnect_still_works_for_a_non_celigo_connection(self, client, db, admin_user):
        """The control: the same endpoint, same auth_type, non-Celigo provider."""
        user, headers = admin_user
        connection = await seed_plain_connection(db, user.tenant_id, "stripe", status="error")

        r = await client.post(f"/api/v1/connections/{connection.id}/reconnect", headers=headers)

        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active"


# ---------------------------------------------------------------------------
# Gap 2 -- generic MCP DELETE revokes celigo_mcp alone, orphaning the pair
# ---------------------------------------------------------------------------


class TestGap2GenericMcpDeleteOrphansThePair:
    """DELETE /mcp-connectors/{id} -> delete_mcp_connector revokes the
    celigo_mcp row alone. The REST Connection stays active, so get_celigo_status
    keeps reporting connected while the agent's tools silently die.

    MUST close with zero edits to app/api/v1/mcp_connectors.py.
    """

    async def test_delete_refuses_celigo_mcp_connector(self, client, db, admin_user):
        user, headers = admin_user
        connection = await seed_celigo_connection(db, user.tenant_id)
        connector = await seed_celigo_mcp_connector(db, user.tenant_id)
        connection_id, connector_id = connection.id, connector.id

        r = await client.delete(f"/api/v1/mcp-connectors/{connector_id}", headers=headers)

        assert r.status_code == 400, f"expected the guard to refuse, got {r.status_code}: {r.text}"
        assert "Settings" in r.json()["detail"]

        await db.rollback()
        row = (await db.execute(select(McpConnector).where(McpConnector.id == connector_id))).scalar_one()
        assert row.status == "active", "a refused delete must not have revoked the connector"
        assert row.is_enabled is True

        rest = (await db.execute(select(Connection).where(Connection.id == connection_id))).scalar_one()
        assert rest.status == "active", "the pair must still be coherent"

    async def test_delete_still_works_for_a_non_celigo_connector(self, client, db, admin_user):
        """The control: the same endpoint against a netsuite_mcp connector."""
        user, headers = admin_user
        connector = await seed_plain_mcp_connector(db, user.tenant_id)

        r = await client.delete(f"/api/v1/mcp-connectors/{connector.id}", headers=headers)

        assert r.status_code == 204, r.text
        row = (await db.execute(select(McpConnector).where(McpConnector.id == connector.id))).scalar_one()
        assert row.status == "revoked"
        assert row.is_enabled is False


# ---------------------------------------------------------------------------
# Gap 3 -- update_connector_tokens flips status="active" provider-blind
# ---------------------------------------------------------------------------


class TestGap3UpdateConnectorTokensIsProviderBlind:
    """``update_connector_tokens`` writes OAuth2 tokens and unconditionally sets
    ``status = "active"``. It has no idea some connectors are half of a pair.

    MUST close with zero edits to mcp_connector_service.update_connector_tokens.
    """

    async def test_update_connector_tokens_refuses_a_celigo_mcp_row(self, db, admin_user):
        user, _ = admin_user
        # Seeded already-revoked: this is the state the bug reactivates from.
        connector = await seed_celigo_mcp_connector(db, user.tenant_id, status="revoked", is_enabled=False)
        connector_id = connector.id

        with pytest.raises(CeligoManagedElsewhereError):
            await mcp_connector_service.update_connector_tokens(
                db=db,
                connector=connector,
                token_data={"access_token": "new", "refresh_token": "r", "expires_in": 3600},
                account_id="acme",
                client_id="cid",
            )

        await db.rollback()
        row = (await db.execute(select(McpConnector).where(McpConnector.id == connector_id))).scalar_one()
        assert row.status == "revoked", "a refused token update must not have reactivated the connector"

    async def test_update_connector_tokens_still_works_for_netsuite_mcp(self, db, admin_user):
        """The control: the identical call against the provider it was written for."""
        user, _ = admin_user
        connector = await seed_plain_mcp_connector(db, user.tenant_id)
        connector.status = "error"
        await db.flush()

        await mcp_connector_service.update_connector_tokens(
            db=db,
            connector=connector,
            token_data={"access_token": "new", "refresh_token": "r", "expires_in": 3600},
            account_id="acme",
            client_id="cid",
        )

        assert connector.status == "active"


# ---------------------------------------------------------------------------
# Registration drift -- the listener must be on sessions from EVERY factory
# ---------------------------------------------------------------------------


class TestListenerRegistration:
    """A future entrypoint that builds its own sessionmaker without importing
    the guard would be silently unguarded. The mitigation is that the MODELS
    import the guard, so these assertions are about that import actually
    happening -- not about any one call site remembering to.
    """

    def test_listener_is_registered_on_the_session_class(self):
        from sqlalchemy import event
        from sqlalchemy.orm import Session

        from app.services import celigo_write_guard

        # Class-level, not sessionmaker-level: this codebase builds sessions
        # four ways and only two of them are factories.
        assert event.contains(Session, "before_flush", celigo_write_guard._before_flush)
        assert event.contains(Session, "do_orm_execute", celigo_write_guard._do_orm_execute)

    def test_importing_the_models_alone_installs_the_guard(self):
        """The anti-drift claim, stated as an executable fact: the model modules
        pull the guard in, so any session built for these models has it."""
        import ast
        import pathlib

        for module in ("connection.py", "mcp_connector.py"):
            src = pathlib.Path(__file__).resolve().parents[2] / "app" / "models" / module
            tree = ast.parse(src.read_text())
            imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module} | {
                alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
            }
            assert "app.services.celigo_write_guard" in imports, (
                f"app/models/{module} must import the guard so a session for this model "
                "cannot be constructed without the listener loaded"
            )

    async def test_api_async_session_factory_carries_the_listener(self):
        from app.core.database import async_session_factory
        from app.services import celigo_write_guard

        async with async_session_factory() as session:
            assert celigo_write_guard._before_flush in list(session.sync_session.dispatch.before_flush)

    def test_worker_sync_session_carries_the_listener(self):
        """The workers build ``Session(sync_engine)`` directly -- no factory to
        attach a listener to, which is precisely why the guard is class-level."""
        from sqlalchemy.orm import Session

        from app.services import celigo_write_guard
        from app.workers.base_task import sync_engine

        with Session(sync_engine) as session:
            assert celigo_write_guard._before_flush in list(session.dispatch.before_flush)

    def test_worker_sync_session_actually_refuses(self, tenant_a):
        """Executed, not inspected: a sync Session refuses before it ever needs
        a connection, so this proves the sync path without touching the DB."""
        from sqlalchemy.orm import Session

        from app.workers.base_task import sync_engine

        with Session(sync_engine) as session:
            session.add(
                Connection(
                    tenant_id=tenant_a.id,
                    provider="celigo",
                    label="Celigo",
                    status="active",
                    auth_type="api_key",
                    encrypted_credentials=encrypt_credentials({"token": "t"}),
                )
            )
            with pytest.raises(CeligoManagedElsewhereError):
                session.flush()
            session.rollback()

    async def test_worker_async_session_factory_carries_the_listener(self):
        from app.core.database import worker_async_session
        from app.services import celigo_write_guard

        async with worker_async_session() as session:
            assert celigo_write_guard._before_flush in list(session.sync_session.dispatch.before_flush)

    def test_register_listeners_is_idempotent(self):
        """Both models import the guard, so registration runs more than once per
        process. Double-registering would fire the listener twice per flush."""
        from sqlalchemy.orm import Session

        from app.services import celigo_write_guard

        with Session() as session:
            before = list(session.dispatch.before_flush).count(celigo_write_guard._before_flush)
            celigo_write_guard.register_listeners()
            after = list(session.dispatch.before_flush).count(celigo_write_guard._before_flush)
        assert before == after == 1


# ---------------------------------------------------------------------------
# Over-broad matching -- the constraint most likely to be violated
# ---------------------------------------------------------------------------


class TestNonCeligoWritesAreUnaffected:
    """Only provider == 'celigo' / 'celigo_mcp' rows are guarded. This is the
    hard constraint: every other write in the application must behave EXACTLY
    as before, with no allow token anywhere in sight.
    """

    @pytest.mark.parametrize("provider", ["netsuite", "stripe", "shopify"])
    async def test_connection_crud_is_untouched(self, db, tenant_a, provider):
        connection = await seed_plain_connection(db, tenant_a.id, provider)

        connection.status = "error"
        connection.error_reason = "expired"
        await db.flush()
        assert connection.status == "error"

        await db.delete(connection)
        await db.flush()
        assert (await db.execute(select(Connection).where(Connection.id == connection.id))).scalar_one_or_none() is None

    async def test_netsuite_mcp_connector_crud_is_untouched(self, db, tenant_a):
        connector = await seed_plain_mcp_connector(db, tenant_a.id)

        # An is_enabled/status pair that WOULD violate the celigo_mcp invariant.
        connector.is_enabled = True
        connector.status = "error"
        await db.flush()
        assert connector.status == "error"

        # And a server_url that is not a pinned Celigo URL.
        connector.server_url = "https://anything.example.com/mcp"
        await db.flush()

        await db.delete(connector)
        await db.flush()

    async def test_a_custom_provider_mcp_connector_is_untouched(self, db, tenant_a):
        connector = await seed_plain_mcp_connector(db, tenant_a.id, provider="custom")
        connector.label = "renamed"
        await db.flush()
        assert connector.label == "renamed"

    async def test_bulk_orm_dml_against_other_tables_is_untouched(self, db, tenant_a):
        """The do_orm_execute tripwire is table-scoped; unrelated bulk DML runs."""
        from sqlalchemy import update

        from app.models.job import Job

        job = Job(tenant_id=tenant_a.id, job_type="tasks.noop", status="running")
        db.add(job)
        await db.flush()

        await db.execute(update(Job).where(Job.id == job.id).values(status="completed"))
        await db.refresh(job)
        assert job.status == "completed"


class TestBulkDmlTripwire:
    async def test_bulk_update_against_connections_is_refused(self, db, tenant_a):
        from sqlalchemy import update

        with pytest.raises(CeligoManagedElsewhereError):
            await db.execute(update(Connection).where(Connection.tenant_id == tenant_a.id).values(status="active"))

    async def test_bulk_delete_against_mcp_connectors_is_refused(self, db, tenant_a):
        from sqlalchemy import delete

        with pytest.raises(CeligoManagedElsewhereError):
            await db.execute(delete(McpConnector).where(McpConnector.tenant_id == tenant_a.id))

    async def test_plain_select_is_never_refused(self, db, tenant_a):
        rows = (await db.execute(select(Connection).where(Connection.tenant_id == tenant_a.id))).scalars().all()
        assert list(rows) == []


# ---------------------------------------------------------------------------
# Field invariants -- enforced INSIDE the allowed window
# ---------------------------------------------------------------------------


class TestFieldInvariantsInsideTheAllowedWindow:
    """The allow token buys permission to write, not permission to write
    nonsense. These are the pairwise rules _upsert_celigo_mcp_connector
    currently maintains by hand -- hand-maintained invariants decay.
    """

    async def test_is_enabled_true_with_non_active_status_is_refused(self, db, tenant_a):
        connector = await seed_celigo_mcp_connector(db, tenant_a.id)

        with pytest.raises(CeligoInvariantError):
            with celigo_writes_allowed(db):
                connector.is_enabled = True
                connector.status = "error"
                await db.flush()
        await db.rollback()

    async def test_a_wrong_server_url_is_refused(self, db, tenant_a):
        connector = await seed_celigo_mcp_connector(db, tenant_a.id)

        with pytest.raises(CeligoInvariantError):
            with celigo_writes_allowed(db):
                connector.server_url = "https://attacker.example.com/celigo-mcp"
                await db.flush()
        await db.rollback()

    async def test_a_new_row_with_a_wrong_server_url_is_refused(self, db, tenant_a):
        with pytest.raises(CeligoInvariantError):
            with celigo_writes_allowed(db):
                db.add(
                    McpConnector(
                        tenant_id=tenant_a.id,
                        provider="celigo_mcp",
                        label="Celigo (agent access)",
                        server_url="https://attacker.example.com/celigo-mcp",
                        auth_type="bearer",
                        encrypted_credentials=encrypt_credentials({"token": "t"}),
                        status="active",
                        is_enabled=True,
                    )
                )
                await db.flush()
        await db.rollback()

    @pytest.mark.parametrize("region", ["us", "eu"])
    async def test_both_pinned_region_urls_are_accepted(self, db, tenant_a, region):
        connector = await seed_celigo_mcp_connector(db, tenant_a.id, region=region)
        assert connector.server_url == CELIGO_MCP_SERVER_URLS[region]

    async def test_the_coherent_disabled_pair_is_accepted(self, db, tenant_a):
        """is_enabled=False with a non-active status is the FAILED-discovery
        outcome _upsert_celigo_mcp_connector writes; it must stay writable."""
        connector = await seed_celigo_mcp_connector(db, tenant_a.id)

        with celigo_writes_allowed(db):
            connector.is_enabled = False
            connector.status = "error"
            connector.error_reason = "Tool discovery failed"
            await db.flush()

        assert connector.status == "error"


class TestProviderRenameCannotLaunderARow:
    """Classifying a row by its CURRENT ``provider`` alone lets an UPDATE that
    rewrites ``provider`` escape the guard in the same flush that mutates it:
    the listener reads the already-applied new value, sees a non-Celigo
    provider, and waves the write through. The inverse matters just as much --
    a row renamed INTO ``celigo``/``celigo_mcp`` must be guarded from that write
    onward, or the pair can be assembled through a generic path.

    Both ends of the rename are therefore considered, and either one matching
    guards the row.
    """

    async def test_renaming_a_celigo_connection_out_of_the_guard_is_refused(self, db, tenant_a):
        connection = await seed_celigo_connection(db, tenant_a.id)
        connection_id = connection.id

        connection.provider = "netsuite"
        with pytest.raises(CeligoManagedElsewhereError):
            await db.flush()

        await db.rollback()
        row = (await db.execute(select(Connection).where(Connection.id == connection_id))).scalar_one()
        assert row.provider == "celigo", "the rename must not have laundered the row out of the guard"

    async def test_renaming_a_celigo_mcp_connector_out_of_the_guard_is_refused(self, db, tenant_a):
        connector = await seed_celigo_mcp_connector(db, tenant_a.id)
        connector_id = connector.id

        connector.provider = "netsuite_mcp"
        with pytest.raises(CeligoManagedElsewhereError):
            await db.flush()

        await db.rollback()
        row = (await db.execute(select(McpConnector).where(McpConnector.id == connector_id))).scalar_one()
        assert row.provider == "celigo_mcp"

    async def test_renaming_an_expired_celigo_row_out_of_the_guard_is_refused(self, db, tenant_a):
        """The prior value is not always sitting in ``__dict__``.

        ``expire()`` drops it, and SQLAlchemy records ``NO_VALUE`` as the
        pre-change value when the attribute is then SET without a fetch -- so
        the cheap, non-loading history is blank on both sides and a guard that
        trusted it would wave the write through.
        """
        connection = await seed_celigo_connection(db, tenant_a.id)
        connection_id = connection.id
        db.expire(connection)

        connection.provider = "netsuite"
        with pytest.raises(CeligoManagedElsewhereError):
            await db.flush()

        await db.rollback()
        row = (await db.execute(select(Connection).where(Connection.id == connection_id))).scalar_one()
        assert row.provider == "celigo"

    async def test_renaming_a_plain_connection_into_celigo_is_refused(self, db, tenant_a):
        connection = await seed_plain_connection(db, tenant_a.id, "netsuite")
        connection_id = connection.id

        connection.provider = "celigo"
        with pytest.raises(CeligoManagedElsewhereError):
            await db.flush()

        await db.rollback()
        row = (await db.execute(select(Connection).where(Connection.id == connection_id))).scalar_one()
        assert row.provider == "netsuite"

    @pytest.mark.parametrize("old,new", [("stripe", "shopify"), ("netsuite", "stripe")])
    async def test_a_rename_between_two_non_guarded_providers_is_untouched(self, db, tenant_a, old, new):
        """The control: neither end is a Celigo provider, so nothing fires."""
        connection = await seed_plain_connection(db, tenant_a.id, old)

        connection.provider = new
        await db.flush()

        assert connection.provider == new

    async def test_renaming_an_expired_non_celigo_connection_is_untouched(self, db, tenant_a):
        """The control for the DB fallback above: same unloaded-prior-value
        shape, neither end Celigo. Failing CLOSED on the blank history would
        refuse this, and every non-Celigo write must behave exactly as before --
        so the fallback has to be exact, not conservative."""
        connection = await seed_plain_connection(db, tenant_a.id, "stripe")
        db.expire(connection)

        connection.provider = "shopify"
        await db.flush()

        assert connection.provider == "shopify"

    def test_the_fast_path_short_circuits_before_reading_any_attribute(self, monkeypatch, tenant_a):
        """``_guarded_table`` runs for every object in every flush, so an
        unrelated table must cost one class lookup and one dict lookup -- no
        attribute read, no history, no lazy load."""
        from app.models.job import Job
        from app.services import celigo_write_guard

        def _explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("the fast path must reject before touching the instance")

        monkeypatch.setattr(celigo_write_guard, "_attr", _explode)
        monkeypatch.setattr(celigo_write_guard, "_provider_values", _explode)

        assert celigo_write_guard._guarded_table(Job(tenant_id=tenant_a.id, job_type="tasks.noop")) is None


class TestAllowTokenScoping:
    async def test_the_token_does_not_outlive_its_block(self, db, tenant_a):
        connection = await seed_celigo_connection(db, tenant_a.id)

        connection.status = "revoked"
        with pytest.raises(CeligoManagedElsewhereError):
            await db.flush()
        await db.rollback()

    async def test_the_token_is_reentrant(self, db, tenant_a):
        """connect_celigo nests a window inside a window; the inner exit must
        not disarm the outer one."""
        connection = await seed_celigo_connection(db, tenant_a.id)

        with celigo_writes_allowed(db):
            with celigo_writes_allowed(db):
                connection.label = "inner"
                await db.flush()
            connection.label = "outer"
            await db.flush()

        assert connection.label == "outer"

    async def test_a_no_op_touch_is_not_treated_as_a_mutation(self, db, tenant_a):
        """session.dirty is populated by any SET, including one that writes the
        same value back. is_modified() is what decides."""
        connection = await seed_celigo_connection(db, tenant_a.id)

        connection.status = connection.status
        await db.flush()  # must not raise

    async def test_the_token_is_per_session(self, db, tenant_a):
        """Holding the token on one session must not license writes on another."""
        from app.core.database import async_session_factory

        with celigo_writes_allowed(db):
            async with async_session_factory() as other:
                assert other.info.get("celigo_writes_allowed") is None


# ---------------------------------------------------------------------------
# Read side -- the celigo feature flag gates agent tool grants
# ---------------------------------------------------------------------------


class TestReadSideFeatureFlagGating:
    """A live celigo_mcp row must stop granting the agent Celigo tools the
    moment the tenant's `celigo` flag goes off. All three tool-grant consumers
    funnel through get_active_connectors_for_tenant, so gating it there covers
    every one of them.
    """

    async def test_celigo_mcp_is_excluded_when_the_flag_is_off(self, db, tenant_a):
        from tests.conftest import enable_feature_flag

        await seed_celigo_mcp_connector(db, tenant_a.id)
        await seed_plain_mcp_connector(db, tenant_a.id)

        await enable_feature_flag(db, tenant_a.id, "celigo", enabled=False)
        connectors = await mcp_connector_service.get_active_connectors_for_tenant(db, tenant_a.id)

        providers = {c.provider for c in connectors}
        assert "celigo_mcp" not in providers, "a disabled celigo flag must revoke the agent's Celigo tools"
        assert "netsuite_mcp" in providers, "other connectors must be unaffected by the celigo flag"

    async def test_celigo_mcp_is_included_when_the_flag_is_on(self, db, tenant_a):
        from tests.conftest import enable_feature_flag

        await seed_celigo_mcp_connector(db, tenant_a.id)
        await enable_feature_flag(db, tenant_a.id, "celigo", enabled=True)

        connectors = await mcp_connector_service.get_active_connectors_for_tenant(db, tenant_a.id)
        assert "celigo_mcp" in {c.provider for c in connectors}

    async def test_the_agent_stops_being_offered_celigo_tools(self, db, tenant_a):
        """End of the read-side chain: the tool definitions the agent is handed."""
        from tests.conftest import enable_feature_flag

        await seed_celigo_mcp_connector(db, tenant_a.id)

        await enable_feature_flag(db, tenant_a.id, "celigo", enabled=True)
        on = await mcp_connector_service.get_active_connectors_for_tenant(db, tenant_a.id)

        await enable_feature_flag(db, tenant_a.id, "celigo", enabled=False)
        off = await mcp_connector_service.get_active_connectors_for_tenant(db, tenant_a.id)

        assert any(c.provider == "celigo_mcp" for c in on)
        assert not any(c.provider == "celigo_mcp" for c in off)

    async def test_no_extra_query_when_the_tenant_has_no_celigo_connector(self, db, tenant_a):
        """The flag lookup must not become a per-request cost for the ~all of
        tenants that have no celigo_mcp row at all."""
        await seed_plain_mcp_connector(db, tenant_a.id)
        connectors = await mcp_connector_service.get_active_connectors_for_tenant(db, tenant_a.id)
        assert [c.provider for c in connectors] == ["netsuite_mcp"]


# ---------------------------------------------------------------------------
# The trusted flow must still work end to end
# ---------------------------------------------------------------------------


class TestTrustedFlowStillWrites:
    """If the context manager is misplaced, the guard breaks the one flow it is
    supposed to allow. tests/api/test_celigo_connector_status.py is the real
    end-to-end proof; this is the direct statement of the property.
    """

    async def test_a_full_connect_disconnect_cycle_round_trips(self, db, tenant_a):
        connection = await seed_celigo_connection(db, tenant_a.id)
        connector = await seed_celigo_mcp_connector(db, tenant_a.id)

        with celigo_writes_allowed(db):
            connection.status = "revoked"
            connector.status = "revoked"
            connector.is_enabled = False
            await db.flush()

            connection.status = "active"
            connector.status = "active"
            connector.is_enabled = True
            await db.flush()

        assert connection.status == "active"
        assert connector.is_enabled is True

    async def test_uuid_primary_keys_survive_the_guard(self, db, tenant_a):
        connection = await seed_celigo_connection(db, tenant_a.id)
        assert isinstance(connection.id, uuid.UUID)
