# backend/tests/test_metric_migration.py
from sqlalchemy import text


async def test_metric_definitions_table_and_permission_exist(db):
    cols = (
        (
            await db.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name='metric_definitions'")
            )
        )
        .scalars()
        .all()
    )
    assert {"id", "tenant_id", "key", "blessed_spec", "expression", "intent_embedding"} <= set(cols)

    perm = (
        await db.execute(text("SELECT codename FROM permissions WHERE codename='metrics.manage'"))
    ).scalar_one_or_none()
    assert perm == "metrics.manage"
