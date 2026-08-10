"""add_domain_to_brand

Revision ID: 91c6f753131e
Revises: 99fb80ecf5ad
Create Date: 2026-07-02 07:32:14.134178

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '91c6f753131e'
down_revision: Union[str, Sequence[str], None] = '99fb80ecf5ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('brands', sa.Column('domain', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('brands', 'domain')
