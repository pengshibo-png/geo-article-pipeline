"""report 补齐冻结 DDL 列：status/step/tokens_in/tokens_out/cost/summary_line/
share_token/share_active/share_expires（对应桌面路径 dbpatch）。

Revision ID: c19d5a7b2e46
Revises: b3e8d2f47a19
Create Date: 2026-10-13 11:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "c19d5a7b2e46"
down_revision = "b3e8d2f47a19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("report") as batch_op:
        batch_op.add_column(sa.Column("status", sa.String(16), nullable=False,
                                      server_default="queued"))
        batch_op.add_column(sa.Column("step", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("tokens_in", sa.Integer(), nullable=False,
                                      server_default="0"))
        batch_op.add_column(sa.Column("tokens_out", sa.Integer(), nullable=False,
                                      server_default="0"))
        batch_op.add_column(sa.Column("cost", sa.Float(), nullable=False,
                                      server_default="0"))
        batch_op.add_column(sa.Column("summary_line", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("share_token", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("share_active", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.add_column(sa.Column("share_expires", sa.DateTime(timezone=True),
                                      nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("report") as batch_op:
        for col in ("share_expires", "share_active", "share_token", "summary_line",
                    "cost", "tokens_out", "tokens_in", "step", "status"):
            batch_op.drop_column(col)
