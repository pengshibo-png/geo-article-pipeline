"""文章模型（ADR-006：元数据入库 + 正文落盘）。

正文不落库，落在 data/articles/<tenant>/<dir>/：
  01_draft.html / 02_review.json / 03_final.html
—— 这三个文件名是**资产契约**（施工规格书 C4），也是断点续跑的依据。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TenantScopedMixin, TimestampMixin

ARTICLE_STATUS = ("draft", "reviewed", "final", "published")


class Article(db.Model, TimestampMixin, TenantScopedMixin):
    __tablename__ = "article"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dir", name="uq_article_tenant_dir"),   # ADR 问题 6
        CheckConstraint("dir NOT LIKE '%..%'", name="ck_article_dir_no_escape"),
        CheckConstraint(
            "status IN ('draft','reviewed','final','published')", name="ck_article_status"
        ),
        Index("idx_article_tenant_created", "tenant_id", "created_at"),
        Index("idx_article_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dir: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    source_title_id: Mapped[int | None] = mapped_column()
    # ADR 问题 7：不用 TEXT 存 "17:38"，改 TIMESTAMP
    draft_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArticleJob(db.Model, TimestampMixin, TenantScopedMixin):
    """生成任务的**断点/状态**记录（检查点落在库里，正文落在磁盘）。

    状态机：queued → running → done | failed
    断点续跑：running 且超过 lease_until 视为中断，可重投（幂等键见 idem_key）。
    """

    __tablename__ = "article_job"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idem_key", name="uq_ajob_tenant_idem"),
        Index("idx_ajob_claim", "status", "queued_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    idem_key: Mapped[str] = mapped_column(String(128), nullable=False)
    article_dir: Mapped[str] = mapped_column(String(128), nullable=False)
    stage: Mapped[str] = mapped_column(String(8), nullable=False, default="T")  # T|A|B|C
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
