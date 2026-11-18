"""身份域模型：租户、审计日志（ADR-003 §3：admin 在身份域可写）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TimestampMixin


class Tenant(db.Model, TimestampMixin):
    """租户。**本身没有 tenant_id 列**（它就是租户），故不继承 TenantScopedMixin。"""

    __tablename__ = "tenant"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Q5：不要求销毁，仅软停用（ADR-001 v1.1 §4.2）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    contact: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Tenant {self.id} {self.name}>"


class AuditLog(db.Model, TimestampMixin):
    """admin 跨租户视角的审计（ADR-003 §2.4 / v1.1 §3.3）。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    endpoint: Mapped[str | None] = mapped_column(String(128))
    happened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
