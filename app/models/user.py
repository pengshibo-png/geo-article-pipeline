"""用户与登录限流（身份域）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import TenantScopedMixin, TimestampMixin


class AppUser(db.Model, TimestampMixin, TenantScopedMixin):
    """应用用户。role=admin 的服务端账号（身份域，可跨租户开通客户）。"""

    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_user_tenant_username"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="client")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class LoginAttempt(db.Model, TimestampMixin):
    """登录限流表。

    ARCH-ADR-001 §4.1 问题 3：实测限流**按账号维度**（X-Forwarded-For 不生效），
    因此主键是 `tenant_id`，IP 只作为记录字段（last_ip），不参与限流键。
    """

    __tablename__ = "login_attempt"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ip: Mapped[str | None] = mapped_column(String(64))
