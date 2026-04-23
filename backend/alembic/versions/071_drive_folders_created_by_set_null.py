"""drive_folders.created_by: SET NULL on user delete

Revision ID: 071_folders_setnull
Revises: 070_drive_rag
Create Date: 2026-04-22
"""

from alembic import op

revision = "071_folders_setnull"
down_revision = "070_drive_rag"


def upgrade() -> None:
    op.drop_constraint(
        "drive_folders_created_by_fkey", "drive_folders", type_="foreignkey"
    )
    op.create_foreign_key(
        "drive_folders_created_by_fkey",
        "drive_folders",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "drive_folders_created_by_fkey", "drive_folders", type_="foreignkey"
    )
    op.create_foreign_key(
        "drive_folders_created_by_fkey",
        "drive_folders",
        "users",
        ["created_by"],
        ["id"],
    )
