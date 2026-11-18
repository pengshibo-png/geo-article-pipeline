"""account.yxer_id：蚁小二云账号绑定列（对应桌面路径 dbpatch）。

Revision ID: d7a3f1c85b02
Revises: c19d5a7b2e46
Create Date: 2026-10-13 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "d7a3f1c85b02"
down_revision = "c19d5a7b2e46"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("account") as batch_op:
        batch_op.add_column(sa.Column("yxer_id", sa.String(64), nullable=True))
        batch_op.create_index("idx_account_yxer", ["tenant_id", "yxer_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("account") as batch_op:
        batch_op.drop_index("idx_account_yxer")
        batch_op.drop_column("yxer_id")
