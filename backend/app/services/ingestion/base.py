"""Shared utilities for ingestion services (synchronous SQLAlchemy Session for Celery tasks)."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.pipeline import CursorState


def load_cursor(db: Session, connection_id, object_type) -> str | None:
    """Load the last-saved cursor value for a connection + object_type pair."""
    stmt = select(CursorState.cursor_value).where(
        CursorState.connection_id == connection_id,
        CursorState.object_type == object_type,
    )
    result = db.execute(stmt).scalar_one_or_none()
    return result


def _build_cursor_upsert_stmt(connection_id, object_type, cursor_value):
    """Build the cursor INSERT...ON CONFLICT DO UPDATE statement.

    Shared by the sync (:func:`save_cursor`) and async (:func:`save_cursor_async`)
    upsert paths so the two stay byte-identical. Stamps ``last_synced_at = now(UTC)``
    explicitly (not via TimestampMixin), stringifies the cursor value, and keys on
    the (connection_id, object_type) unique constraint.
    """
    now = datetime.now(timezone.utc)
    return (
        insert(CursorState)
        .values(
            connection_id=connection_id,
            object_type=object_type,
            cursor_value=str(cursor_value),
            last_synced_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_cursor_states_conn_obj",
            set_={
                "cursor_value": str(cursor_value),
                "last_synced_at": now,
            },
        )
    )


def save_cursor(db: Session, connection_id, object_type, cursor_value) -> None:
    """Upsert a cursor value (INSERT or UPDATE on conflict)."""
    db.execute(_build_cursor_upsert_stmt(connection_id, object_type, cursor_value))


async def save_cursor_async(db: AsyncSession, connection_id, object_type, cursor_value) -> None:
    """Async twin of ``save_cursor``: upsert a cursor value (INSERT or UPDATE on conflict).

    For ingestion services that run on an AsyncSession (e.g. the NetSuite deposit
    sync) and therefore cannot call the synchronous ``save_cursor``. Mirrors its
    field semantics exactly via the shared :func:`_build_cursor_upsert_stmt`, so the
    recon data-status banner reflects every successful sync.
    """
    await db.execute(_build_cursor_upsert_stmt(connection_id, object_type, cursor_value))


def upsert_canonical(db: Session, model_class, tenant_id, dedupe_key: str, data: dict) -> None:
    """INSERT a canonical record or UPDATE on conflict using the per-table dedupe constraint."""
    now = datetime.now(timezone.utc)

    # Ensure required fields are present in data
    data.setdefault("tenant_id", tenant_id)
    data.setdefault("dedupe_key", dedupe_key)
    data.setdefault("updated_at", now)
    data.setdefault("created_at", now)

    constraint_name = f"uq_{model_class.__tablename__}_dedupe"

    # Build the SET clause: update everything except id, tenant_id, dedupe_key, created_at
    excluded_keys = {"id", "tenant_id", "dedupe_key", "created_at"}
    set_ = {k: v for k, v in data.items() if k not in excluded_keys}
    set_["updated_at"] = now

    stmt = (
        insert(model_class)
        .values(**data)
        .on_conflict_do_update(
            constraint=constraint_name,
            set_=set_,
        )
    )
    db.execute(stmt)
