"""Shared utilities for ingestion services (synchronous SQLAlchemy Session for Celery tasks)."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
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


def save_cursor(db: Session, connection_id, object_type, cursor_value) -> None:
    """Upsert a cursor value (INSERT or UPDATE on conflict)."""
    now = datetime.now(timezone.utc)
    stmt = (
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
    db.execute(stmt)


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
