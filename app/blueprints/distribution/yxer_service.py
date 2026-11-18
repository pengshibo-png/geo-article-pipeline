"""distribution 蓝图 · 蚁小二账号库（auth.html「蚁小二账号库」卡契约）。

- GET  /api/yxer-accounts → {accounts:[{id,name,platform,fans,online}], configured}
     实时从蚁小二云端拉取（services/publish/yxer.py）；未配置返回空列表（页面空态）。
- POST /api/yxer-accounts/sync {items:[{id,name,platform,limit}]} → {count}
     勾选启用 + 每日额度入本地 Account 表（yxer_id 绑定）；
     **不在 items 里的既往蚁小二账号 → 停用**（enabled=False，保留发布历史关联）。

原站语义（auth.html:145）："授权登录在蚁小二客户端管理；这里决定是否启用和每日额度"。
"""
from __future__ import annotations

from datetime import date

from app.errors import ValidationError
from app.extensions import db as _db
from app.logging import get_logger
from app.models import Account
from app.services.publish import yxer
from app.tenancy import TenantScopedRepository, current_tenant
from app.timeutil import natural_day_string

log = get_logger(__name__)

_MAX_ITEMS = 200


class YxerAccountService:
    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    def cloud_accounts(self) -> dict:
        out = yxer.list_accounts()
        return {"ok": True, "configured": out.get("configured", False),
                "mock": out.get("mock", False),
                "accounts": out.get("accounts", []), **({"error": out["error"]} if out.get("error") else {})}

    def sync(self, body: dict) -> dict:
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise ValidationError("items 必须是数组")
        if len(items) > _MAX_ITEMS:
            raise ValidationError(f"单次最多同步 {_MAX_ITEMS} 个账号")

        repo = TenantScopedRepository(Account, self.session)
        wanted: dict = {}
        for it in items:
            if not isinstance(it, dict) or not str(it.get("id") or "").strip():
                continue
            yx_id = str(it["id"]).strip()[:64]
            try:
                limit = max(0, min(int(it.get("limit") or 0), 999))
            except (TypeError, ValueError):
                limit = 0
            wanted[yx_id] = {
                "name": str(it.get("name") or yx_id)[:128],
                "platform": str(it.get("platform") or "other")[:32],
                "limit": limit,
            }

        existing = {a.yxer_id: a for a in repo.query().filter(
            Account.yxer_id.isnot(None)).all()}
        count = 0
        for yx_id, meta in wanted.items():
            row = existing.get(yx_id)
            if row is None:
                row = Account(id=f"yx-{yx_id}"[:64], tenant_id=current_tenant(),
                              platform=meta["platform"], name=meta["name"],
                              daily_limit=meta["limit"], used_today=0,
                              quota_date=date.fromisoformat(natural_day_string()), yxer_id=yx_id,
                              enabled=True, verified=True)   # 授权状态由蚁小二客户端管理
                repo.add(row)
            else:
                row.enabled = True
                row.name = meta["name"]
                row.platform = meta["platform"]
                row.daily_limit = meta["limit"]
            count += 1
        for yx_id, row in existing.items():
            if yx_id not in wanted:
                row.enabled = False      # 取消勾选 = 停用（不删除，保留发布历史）
        self.session.commit()
        log.info("蚁小二账号已同步", extra={"event": "yxer_synced", "enabled": count})
        return {"ok": True, "count": count}
