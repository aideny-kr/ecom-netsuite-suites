"""Migration smoke: recon_resolution_proposals exists with RLS + active-unique index."""

from sqlalchemy import text


async def test_recon_resolution_proposals_table_exists(db):
    cols = (
        (
            await db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'recon_resolution_proposals'"
                )
            )
        )
        .scalars()
        .all()
    )
    for expected in (
        "id",
        "tenant_id",
        "run_id",
        "result_id",
        "root_cause",
        "action",
        "booking_vehicle",
        "group_key",
        "source",
        "narrative",
        "evidence",
        "proposed_amount",
        "currency",
        "above_materiality",
        "status",
        "failure_reason",
        "netsuite_record_refs",
        "correlation_id",
        "charge_source_id",
        "decided_by",
        "decided_at",
        "created_at",
        "updated_at",
    ):
        assert expected in cols, f"missing column {expected}"


async def test_rls_forced_on_proposals(db):
    row = (
        await db.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'recon_resolution_proposals'"
            )
        )
    ).one()
    assert row.relrowsecurity is True
    assert row.relforcerowsecurity is True


async def test_one_active_proposal_per_result_index(db):
    idx = (
        await db.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'recon_resolution_proposals' "
                "AND indexname = 'uq_recon_resolution_proposals_active_result'"
            )
        )
    ).scalar_one()
    assert "UNIQUE" in idx
    assert "proposed" in idx  # partial WHERE clause present
