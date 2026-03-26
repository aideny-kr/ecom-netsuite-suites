"""Add missing permissions to ops role (data parity with finance minus financial reports)."""

from alembic import op
import sqlalchemy as sa

revision = "056_ops_perms"
down_revision = "055_eval_cases"

# Permissions to add to ops role
_OPS_ADD = ["exports.excel", "tools.suiteql", "recon.run"]


def upgrade() -> None:
    conn = op.get_bind()

    # Get ops role ID
    ops_role = conn.execute(sa.text("SELECT id FROM roles WHERE name = 'ops'")).fetchone()
    if not ops_role:
        return

    ops_id = ops_role[0]

    for codename in _OPS_ADD:
        perm = conn.execute(
            sa.text("SELECT id FROM permissions WHERE codename = :c"),
            {"c": codename},
        ).fetchone()
        if not perm:
            continue

        # Skip if already assigned
        existing = conn.execute(
            sa.text(
                "SELECT 1 FROM role_permissions WHERE role_id = :rid AND permission_id = :pid"
            ),
            {"rid": ops_id, "pid": perm[0]},
        ).fetchone()
        if existing:
            continue

        conn.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id) VALUES (:rid, :pid)"
            ),
            {"rid": ops_id, "pid": perm[0]},
        )


def downgrade() -> None:
    conn = op.get_bind()

    ops_role = conn.execute(sa.text("SELECT id FROM roles WHERE name = 'ops'")).fetchone()
    if not ops_role:
        return

    ops_id = ops_role[0]

    for codename in _OPS_ADD:
        perm = conn.execute(
            sa.text("SELECT id FROM permissions WHERE codename = :c"),
            {"c": codename},
        ).fetchone()
        if not perm:
            continue

        conn.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE role_id = :rid AND permission_id = :pid"
            ),
            {"rid": ops_id, "pid": perm[0]},
        )
