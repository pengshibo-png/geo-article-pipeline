"""业务事实源模型：品牌资料 / 城市 / 读者画像 / 指令（config 蓝图管辖）。

依据《品牌资料九大分区规范》：品牌资料是**一个纯文本字段**，分区靠 `【】` 约定，
不落结构化列；完整度由算法派生，不入库（避免派生值与内容不一致）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TenantScopedMixin, TimestampMixin


class Brand(db.Model, TimestampMixin, TenantScopedMixin):
    """品牌资料（唯一事实来源）。tenant_id 即主键 —— 一个租户一份。"""

    __tablename__ = "brand"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class City(db.Model, TimestampMixin, TenantScopedMixin):
    __tablename__ = "city"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_city_tenant_name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    ratio: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Audience(db.Model, TimestampMixin, TenantScopedMixin):
    __tablename__ = "audience"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Prompt(db.Model, TimestampMixin, TenantScopedMixin):
    """四段指令（T/A/B/C）的租户级定制。空则用默认 Prompt。"""

    __tablename__ = "prompt"
    __table_args__ = (UniqueConstraint("tenant_id", "step", name="uq_prompt_tenant_step"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    step: Mapped[str] = mapped_column(String(4), nullable=False)
    name: Mapped[str | None] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Asset(db.Model, TimestampMixin, TenantScopedMixin):
    """素材库条目（图片等），实际文件落 data/，此处存元数据与租户内相对路径。"""

    __tablename__ = "asset"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    rel_path: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="image")
    note: Mapped[str | None] = mapped_column(Text)


class TitleConfig(db.Model, TimestampMixin, TenantScopedMixin):
    """时效词占比**只存这里**（ADR 问题 5：不与 genre_mix 双存）。"""

    __tablename__ = "title_config"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    business: Mapped[str | None] = mapped_column(String(255))
    cities: Mapped[str | None] = mapped_column(Text)
    expert: Mapped[str | None] = mapped_column(String(128))
    directions: Mapped[str | None] = mapped_column(Text)
    timeliness_ratio: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=25)
    # 城市词占比（%）：原站「业务城市」页可调，选题指令 {city_rule} 由此计算。
    # 存量库由 app/dbpatch.py 在启动时补列，SaaS 路径由 alembic 迁移补列。
    city_ratio: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
