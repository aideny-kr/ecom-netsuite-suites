"""DB-backed schema test for migration 079_order_ref_pattern.

R3 Part 1 Task T1. Runs against the local docker Postgres via the conftest ``db``
fixture (each test is rolled back). It asserts that the new column added by
migration 079 has the expected NULL-sentinel semantics:

  - tenant_configs.order_ref_pattern  VARCHAR(200) NULLABLE, no server_default
    (NULL = "use the engine default" — Framework stays byte-identical).

Written rigorously following the existing recon DB-test patterns
(``test_recon_buckets_materiality_migration.py``) but NOT run in the implementer
environment (no DB here); the PM runs it post-flight against local docker Postgres.
"""

from sqlalchemy import select, text

from app.models.tenant import TenantConfig


async def test_order_ref_pattern_column_defaults_null(db, tenant_a):
    """A freshly-created tenant config has order_ref_pattern == NULL (the sentinel)."""
    cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
    assert cfg.order_ref_pattern is None


async def test_order_ref_pattern_column_is_nullable(db):
    """The information_schema reports order_ref_pattern as a nullable VARCHAR(200) with no default."""
    row = (
        await db.execute(
            text(
                "SELECT data_type, character_maximum_length, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = 'tenant_configs' AND column_name = 'order_ref_pattern'"
            )
        )
    ).first()
    assert row is not None, "order_ref_pattern column missing from tenant_configs"
    data_type, max_len, is_nullable, column_default = row
    assert data_type == "character varying"
    assert max_len == 200
    assert is_nullable == "YES"
    # NULL is the sentinel meaning "use the engine default" — there must be no server_default.
    assert column_default is None


async def test_order_ref_pattern_roundtrips_when_set(db, tenant_a):
    """A set pattern persists and reads back (the non-default path the read-side will use)."""
    cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
    cfg.order_ref_pattern = r"(#\d{4,})"
    await db.flush()
    await db.refresh(cfg)
    assert cfg.order_ref_pattern == r"(#\d{4,})"
