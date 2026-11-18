"""选题与文体配比（topic 蓝图管辖）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TenantScopedMixin, TimestampMixin

GENRES = ("商业推荐", "价格咨询", "避坑对比", "科普知识")


class Title(db.Model, TimestampMixin, TenantScopedMixin):
    """选题库。软删（ADR 问题 12）：deleted_at 非空即视为已删，配合 partial 唯一索引。"""

    __tablename__ = "title"
    __table_args__ = (
        UniqueConstraint("tenant_id", "text", "deleted_at", name="uq_title_tenant_text"),
        Index("idx_title_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    genre: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="ai")
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenreMix(db.Model, TimestampMixin, TenantScopedMixin):
    """四文体配比。合计恒 100%（应用层归一化）；时效词占比**不在这里**（见 TitleConfig）。"""

    __tablename__ = "genre_mix"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    genre: Mapped[str] = mapped_column(String(16), primary_key=True)
    ratio: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)


class CompetitorTitle(db.Model, TimestampMixin, TenantScopedMixin):
    """同行标题训练材料（只读参考，严禁出现在产出里）。"""

    __tablename__ = "competitor_title"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(64))
