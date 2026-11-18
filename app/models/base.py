"""模型基类与混入。

- `TimestampMixin`：统一 `created_at`。
- `TenantScopedMixin`：**任何租户业务表必须继承**。V2 元数据自检据此发现
  "新增了带 tenant_id 的表却没纳入隔离机制"的情况（ARCH-ADR-001 §2.6）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.extensions import db


class TimestampMixin:
    @declared_attr
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TenantScopedMixin:
    """租户归属列。仓储层与 before_flush 都会自动注入/校验它。"""

    @declared_attr
    def tenant_id(cls) -> Mapped[str]:
        return mapped_column(String(64), nullable=False, index=True)
