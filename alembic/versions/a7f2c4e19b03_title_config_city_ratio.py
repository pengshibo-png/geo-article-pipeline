"""title_config 增加城市词占比列

Revision ID: a7f2c4e19b03
Revises: c5300fa6dfa8
Create Date: 2026-10-12 09:40:00.000000

原站「业务城市」页的城市词占比（{city_rule} 变量的输入）此前没有落库列，
B3 前端还原需要它参与 GET /api/cities 与 /api/my-title-config 的响应。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7f2c4e19b03'
down_revision: Union[str, Sequence[str], None] = 'c5300fa6dfa8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('title_config', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('city_ratio', sa.Integer(), nullable=False, server_default='60')
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('title_config', schema=None) as batch_op:
        batch_op.drop_column('city_ratio')
