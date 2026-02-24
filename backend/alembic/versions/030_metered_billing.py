"""Add tenant_wallets table and global_role to users

Revision ID: 030_metered_billing
Revises: 029_learned_rules_created_by
Create Date: 2026-02-24 05:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '030_metered_billing'
down_revision: Union[str, None] = '029_learned_rules_created_by'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # tenant_wallets — credit ledger
    op.create_table(
        'tenant_wallets',
        sa.Column('tenant_id', sa.UUID(), sa.ForeignKey('tenants.id', name=op.f('fk_tenant_wallets_tenant_id_tenants')), nullable=False),
        sa.Column('stripe_customer_id', sa.String(255), nullable=True),
        sa.Column('stripe_subscription_item_id', sa.String(255), nullable=True),
        sa.Column('billing_period_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('billing_period_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('base_credits_remaining', sa.Integer(), nullable=False, server_default='500'),
        sa.Column('metered_credits_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_synced_metered_credits', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_tenant_wallets')),
        sa.UniqueConstraint('tenant_id', name=op.f('uq_tenant_wallets_tenant_id')),
    )
    op.create_index(op.f('ix_tenant_wallets_tenant_id'), 'tenant_wallets', ['tenant_id'], unique=True)

    # global_role on users — platform-level role (user / admin / superadmin)
    op.add_column('users', sa.Column('global_role', sa.String(20), nullable=False, server_default='user'))


def downgrade() -> None:
    op.drop_column('users', 'global_role')
    op.drop_index(op.f('ix_tenant_wallets_tenant_id'), table_name='tenant_wallets')
    op.drop_table('tenant_wallets')
