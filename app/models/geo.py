"""GEO 监测模型（insight 蓝图管辖）。

口径一致性铁律（AI-ARCH-002 v1.1）：同一引擎同一周期**只用一种通道**，
故 `channel` 字段是必填的 —— 它让"混通道导致趋势失真"这件事变得可检测。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TenantScopedMixin, TimestampMixin

ENGINES = ("doubao", "qwen", "yuanbao", "deepseek")
# api/browser = 真实通道；simulate = 虚拟收录查询（模拟各平台回答，明确标注为"模拟"）
CHANNELS = ("api", "browser", "simulate")


class GeoResult(db.Model, TimestampMixin, TenantScopedMixin):
    """单次采样结果：一个 (提问词, 引擎) 一行。"""

    __tablename__ = "geo_result"
    __table_args__ = (
        CheckConstraint(
            "engine IN ('doubao','qwen','yuanbao','deepseek')", name="ck_geo_engine"
        ),
        CheckConstraint("channel IN ('api','browser','simulate')", name="ck_geo_channel"),
        # ADR 问题 9：未提及则 rank 必须为 NULL（消除 0 与 NULL 的口径歧义）
        CheckConstraint("mentioned = 1 OR rank IS NULL", name="ck_geo_rank_semantics"),
        Index("idx_geo_result_tenant_time", "tenant_id", "sampled_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    query: Mapped[str] = mapped_column(String(255), nullable=False)
    engine: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="api")
    mentioned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    cited_lv: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cited_url: Mapped[str | None] = mapped_column(String(512))
    is_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GeoDaily(db.Model, TimestampMixin, TenantScopedMixin):
    """按自然日（Asia/Shanghai，ADR-013）聚合的趋势点。"""

    __tablename__ = "geo_daily"
    __table_args__ = (
        UniqueConstraint("tenant_id", "date", "engine", "channel", name="uq_geodaily_uniq"),
        Index("idx_geodaily_tenant_date", "tenant_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    engine: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="api")
    valid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mention_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    top_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    top3_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cite_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
