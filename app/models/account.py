"""自媒体账号（distribution 蓝图管辖）。

ARCH-ADR-001 v1.1 §2.1 结论：**不拆表**。一个物理账号唯一归属一个租户，
唯一性由**主键**（平台侧账号 ID）天然保证 —— 第二个租户绑定同一账号必然主键冲突。
虚拟聚合渠道（`mp-all` / `tenant-all`）**不入库**，由 API 层动态组装。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Index, Integer, LargeBinary, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TenantScopedMixin, TimestampMixin

ACCOUNT_STATUS = ("active", "frozen", "invalid")


class Account(db.Model, TimestampMixin, TenantScopedMixin):
    __tablename__ = "account"
    __table_args__ = (
        CheckConstraint("used_today >= 0", name="ck_account_used_nonneg"),
        CheckConstraint("daily_limit = 0 OR used_today <= daily_limit", name="ck_account_quota"),
        Index("idx_account_tenant", "tenant_id", "enabled", "platform"),
    )

    # ★ 主键即平台侧账号 ID —— 唯一性无需应用逻辑（ADR-011）
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_label: Mapped[str | None] = mapped_column(String(64))
    cookie_blob: Mapped[bytes | None] = mapped_column(LargeBinary)   # 加密存储，禁明文
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0=登记型
    used_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quota_date: Mapped[date] = mapped_column(Date, nullable=False)   # ADR-013 自然日
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    note: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 蚁小二云账号 id：非空 = 经蚁小二一键分发的账号（授权登录在蚁小二
    # 客户端管理，本表只存"是否启用 + 每日额度"）；空 = 专用通道。
    # 存量库由 app/dbpatch.py 补列，SaaS 路径由 alembic 迁移补列。
    yxer_id: Mapped[str | None] = mapped_column(String(64))

    @staticmethod
    def quota_update_sql() -> str:
        """并发安全的额度扣减（ADR v1.1 §2.3）。

        一条 UPDATE 同时完成"跨日重置 + 额度校验"；受影响行数 0 = 额度不足。
        **不要先 SELECT 再 UPDATE** —— 并发发布会超发。
        """
        return (
            "UPDATE account SET "
            "  used_today = CASE WHEN quota_date = :today THEN used_today + 1 ELSE 1 END, "
            "  quota_date = :today "
            "WHERE id = :id AND tenant_id = :tenant_id "
            "  AND (daily_limit = 0 OR quota_date <> :today OR used_today < daily_limit)"
        )


def account_now() -> datetime:  # pragma: no cover - 便于测试替换
    return datetime.now()
