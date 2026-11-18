"""发布任务 / 发布日志 / 定时计划（distribution 蓝图管辖）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TenantScopedMixin, TimestampMixin

TASK_STATUS = ("queued", "running", "success", "failed", "canceled", "dead")
ERROR_CODES = ("rate_limited", "login_expired", "quota", "captcha", "timeout", "upstream", "unknown")


class PublishTask(db.Model, TimestampMixin, TenantScopedMixin):
    """发布任务。**承载账号与金钱，重复发布 = 事故** → 幂等键 + 唯一约束防重。"""

    __tablename__ = "publish_task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idem_key", name="uq_ptask_tenant_idem"),
        CheckConstraint(
            "status IN ('queued','running','success','failed','canceled','dead')",
            name="ck_ptask_status",
        ),
        # partial index：任务认领只扫活跃行，不随历史膨胀（ADR §4.3）
        Index("idx_ptask_claim", "status", "priority", "queued_at"),
        Index("idx_ptask_tenant", "tenant_id", "queued_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    article_dir: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    # ★ 幂等键 hash(tenant, dir, account, day) —— 同文章同账号当天只发一次
    idem_key: Mapped[str] = mapped_column(String(128), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(32))   # 归一化错误码
    error: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(512))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublishLog(db.Model, TimestampMixin, TenantScopedMixin):
    """发布日志：**append-only 事实记录**（ADR 问题 8：源系统只在接口里出现，无落地表）。"""

    __tablename__ = "publish_log"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", name="uq_plog_tenant_task"),
        Index("idx_plog_tenant_time", "tenant_id", "happened_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(Integer)
    article_dir: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(255))
    account_id: Mapped[str | None] = mapped_column(String(64))
    account_name: Mapped[str | None] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(32))
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(512))
    happened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 蚁小二是**异步发布**：提交成功只代表拿到 task_set_id，真正结果要回查。
    # 这三个字段存回查结果，避免"提交成功 = 已发布"的误导（2026-10-19 补）。
    remote_status: Mapped[str | None] = mapped_column(String(16))   # pending/ok/fail
    remote_detail: Mapped[str | None] = mapped_column(Text)         # 失败原因/阶段
    remote_url: Mapped[str | None] = mapped_column(String(512))     # 真实发布链接
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Schedule(db.Model, TimestampMixin, TenantScopedMixin):
    """定时计划。ADR 问题 11：补 lease_until，防多实例重复触发。"""

    __tablename__ = "schedule"
    __table_args__ = (
        CheckConstraint("kind IN ('title','article','publish')", name="ck_sched_kind"),
        CheckConstraint("mode IN ('daily','interval')", name="ck_sched_mode"),
        Index("idx_sched_due", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    at_time: Mapped[str | None] = mapped_column(String(8))
    every_min: Mapped[int | None] = mapped_column(Integer)
    count: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 计划的结构化参数（JSON）：publish 计划=每账号渠道配置数组；
    # title/article 计划留空（参数已在 mode/at_time/every_min/count 列）。
    # 存量库由 app/dbpatch.py 补列，SaaS 路径由 alembic 迁移补列。
    config: Mapped[str | None] = mapped_column(Text)
