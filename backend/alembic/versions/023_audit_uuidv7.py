"""Change audit_events PK from BIGINT to UUID

Eliminates sequence contention under high write throughput by allowing
client-side UUID generation.

Revision ID: 023_audit_uuidv7
Revises: 022_workspace_file_locking
Create Date: 2026-02-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "023_audit_uuidv7"
down_revision = "022_workspace_file_locking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add a new UUID column
    op.add_column(
        "audit_events",
        sa.Column("new_id", UUID(as_uuid=True), nullable=True),
    )

    # 2. Populate existing rows with generated UUIDs
    op.execute("UPDATE audit_events SET new_id = gen_random_uuid()")

    # 3. Make it NOT NULL
    op.alter_column("audit_events", "new_id", nullable=False)

    # 4. Drop the old PK constraint and column
    op.drop_constraint("audit_events_pkey", "audit_events", type_="primary")
    op.drop_column("audit_events", "id")

    # 5. Rename new_id -> id and set as PK
    op.alter_column("audit_events", "new_id", new_column_name="id")
    op.create_primary_key("audit_events_pkey", "audit_events", ["id"])

    # 6. Set default for new inserts
    op.alter_column(
        "audit_events",
        "id",
        server_default=sa.text("gen_random_uuid()"),
    )


def downgrade() -> None:
    # Revert to BIGINT with auto-increment
    # 1. Remove server default
    op.alter_column("audit_events", "id", server_default=None)

    # 2. Add a new bigint column
    op.add_column(
        "audit_events",
        sa.Column("old_id", sa.BigInteger, nullable=True),
    )

    # 3. Populate with sequential IDs
    op.execute(
        "UPDATE audit_events SET old_id = sub.rn FROM "
        "(SELECT id, ROW_NUMBER() OVER (ORDER BY timestamp) AS rn FROM audit_events) sub "
        "WHERE audit_events.id = sub.id"
    )
    op.alter_column("audit_events", "old_id", nullable=False)

    # 4. Drop UUID PK
    op.drop_constraint("audit_events_pkey", "audit_events", type_="primary")
    op.drop_column("audit_events", "id")

    # 5. Rename and create PK + sequence
    op.alter_column("audit_events", "old_id", new_column_name="id")
    op.execute("CREATE SEQUENCE audit_events_id_seq OWNED BY audit_events.id")
    op.execute("SELECT setval('audit_events_id_seq', COALESCE(MAX(id), 1)) FROM audit_events")
    op.alter_column(
        "audit_events",
        "id",
        server_default=sa.text("nextval('audit_events_id_seq')"),
    )
    op.create_primary_key("audit_events_pkey", "audit_events", ["id"])
