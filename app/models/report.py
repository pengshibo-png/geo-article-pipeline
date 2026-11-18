"""报告与报告配额（insight 蓝图管辖）。

报告 ID 形如 `r_<YYYYMMDD>_<HHmm>_<tenant>_<4hex>`（施工规格书命名规则）。
配额按 Q4 未定 → 先按"租户总量"，但预留 `period_start DATE NULL`
（NULL = 总量模式；将来改按月只需回填，**不改表结构**）。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TenantScopedMixin, TimestampMixin


REPORT_STATUS = ("queued", "generating", "verifying", "rendering", "done", "failed")


class Report(db.Model, TimestampMixin, TenantScopedMixin):
    """列对齐施工规格冻结 DDL：
    report(id, tenant_id, window_from, window_to, status, step, pdf_path,
           tokens_in, tokens_out, cost, summary_line, share_token, share_active)
    Phase 0 建模时缺了状态/成本列，本轮按冻结清单补齐；`share_expires` 是页面
    契约（expires_at 回显）所需的时间列扩展。失败原因写 note 列（不再加列）。
    存量库补列：app/dbpatch.py；SaaS 路径：alembic c19d5a7b2e46。
    """

    __tablename__ = "report"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_report_tenant_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)   # r_..._4hex
    title: Mapped[str | None] = mapped_column(String(255))
    window_from: Mapped[date | None] = mapped_column(Date)
    window_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    step: Mapped[str | None] = mapped_column(String(16))
    pdf_path: Mapped[str | None] = mapped_column(String(255))
    html_path: Mapped[str | None] = mapped_column(String(255))
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    summary_line: Mapped[str | None] = mapped_column(Text)
    share_token: Mapped[str | None] = mapped_column(String(64))
    share_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    share_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    share_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(Text)


class ReportQuota(db.Model, TimestampMixin, TenantScopedMixin):
    """报告配额。tenant_id 即主键。"""

    __tablename__ = "report_quota"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quota_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Q4 预留：NULL = 总量模式；将来按自然月只需回填该字段（ADR-001 v1.1 §8）
    period_start: Mapped[date | None] = mapped_column(Date)
