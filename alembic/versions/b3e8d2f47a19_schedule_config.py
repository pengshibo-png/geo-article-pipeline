"""schedule.config：发布计划的结构化参数列（JSON 文本）。

对应桌面路径 app/dbpatch.py 的同名补丁；模型见 app/models/publish.py。

Revision ID: b3e8d2f47a19
Revises: a7f2c4e19b03
Create Date: 2026-10-13 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "b3e8d2f47a19"
down_revision = "a7f2c4e19b03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedule") as batch_op:
        batch_op.add_column(sa.Column("config", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedule") as batch_op:
        batch_op.drop_column("config")
