"""Task 1 — tenant memory graph models are importable, registered, and carry the trust spine."""


def test_models_importable_and_registered():
    from app.models import TenantMemoryConcept, TenantMemoryEdge, TenantMemoryLink

    assert TenantMemoryConcept.__tablename__ == "tenant_memory_concept"
    assert TenantMemoryEdge.__tablename__ == "tenant_memory_edge"
    assert TenantMemoryLink.__tablename__ == "tenant_memory_link"

    # trust spine present
    cols = TenantMemoryConcept.__table__.columns.keys()
    for c in (
        "name",
        "summary",
        "concept_type",
        "embedding",
        "review_state",
        "confidence",
        "confirmed_by",
        "use_count",
    ):
        assert c in cols, f"missing trust-spine column: {c}"


def test_link_has_idempotency_unique_constraint():
    from sqlalchemy import UniqueConstraint

    from app.models import TenantMemoryLink

    uniques = [c for c in TenantMemoryLink.__table__.constraints if isinstance(c, UniqueConstraint)]
    names = {c.name for c in uniques}
    assert "uq_tenant_memory_link_source" in names
    target = next(c for c in uniques if c.name == "uq_tenant_memory_link_source")
    assert {col.name for col in target.columns} == {"tenant_id", "source_table", "source_id"}
