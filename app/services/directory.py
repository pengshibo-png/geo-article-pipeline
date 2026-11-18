"""租户目录（身份域跨表读取）。

Tenant 表本身不带 tenant_id（它就是租户），TenantScopedRepository 用不了；
V1 的裸查静态扫描只针对 blueprints，身份域只读查询统一下沉到 services 层。
"""
from __future__ import annotations

from app.extensions import db
from app.models import Tenant


def list_tenants() -> list[dict]:
    rows = db.session.query(Tenant).order_by(Tenant.id.asc()).all()
    return [{"id": r.id, "name": r.name} for r in rows]


def get_tenant(tenant_id: str) -> dict | None:
    row = db.session.get(Tenant, tenant_id)
    return {"id": row.id, "name": row.name} if row else None
