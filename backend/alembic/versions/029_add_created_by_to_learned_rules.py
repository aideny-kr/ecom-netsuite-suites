"""Add created_by to tenant_learned_rules

Revision ID: 029_add_created_by_to_learned_rules
Revises: 028_tenant_learned_rules
Create Date: 2026-02-24 03:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '029_learned_rules_created_by'
down_revision: Union[str, None] = '028_tenant_learned_rules'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'tenant_learned_rules',
        sa.Column(
            'created_by', sa.UUID(),
            sa.ForeignKey('users.id', name=op.f('fk_tenant_learned_rules_created_by_users')),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('tenant_learned_rules', 'created_by')
