"""Add chat.financial_reports permission and assign to admin + finance roles."""

from alembic import op

revision = "044_financial_perm"
down_revision = "043_saved_query_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO permissions (id, codename)
        VALUES (gen_random_uuid(), 'chat.financial_reports')
    """)
    op.execute("""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r, permissions p
        WHERE r.name IN ('admin', 'finance')
        AND p.codename = 'chat.financial_reports'
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM role_permissions
        WHERE permission_id = (SELECT id FROM permissions WHERE codename = 'chat.financial_reports')
    """)
    op.execute("DELETE FROM permissions WHERE codename = 'chat.financial_reports'")
