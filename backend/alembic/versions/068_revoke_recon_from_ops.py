"""Revoke recon.run from ops role.

Ops should not have access to reconciliation — only admin and finance.
Migration 056 incorrectly added recon.run to ops.
"""

import sqlalchemy as sa

from alembic import op

revision = "068_revoke_recon_ops"
down_revision = "067_drop_source_pin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    ops_role = conn.execute(sa.text("SELECT id FROM roles WHERE name = 'ops'")).fetchone()
    if not ops_role:
        return

    perm = conn.execute(
        sa.text("SELECT id FROM permissions WHERE codename = 'recon.run'"),
    ).fetchone()
    if not perm:
        return

    conn.execute(
        sa.text("DELETE FROM role_permissions WHERE role_id = :rid AND permission_id = :pid"),
        {"rid": ops_role[0], "pid": perm[0]},
    )


def downgrade() -> None:
    conn = op.get_bind()

    ops_role = conn.execute(sa.text("SELECT id FROM roles WHERE name = 'ops'")).fetchone()
    if not ops_role:
        return

    perm = conn.execute(
        sa.text("SELECT id FROM permissions WHERE codename = 'recon.run'"),
    ).fetchone()
    if not perm:
        return

    existing = conn.execute(
        sa.text("SELECT 1 FROM role_permissions WHERE role_id = :rid AND permission_id = :pid"),
        {"rid": ops_role[0], "pid": perm[0]},
    ).fetchone()
    if existing:
        return

    conn.execute(
        sa.text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:rid, :pid)"),
        {"rid": ops_role[0], "pid": perm[0]},
    )
