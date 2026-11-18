"""distribution 蓝图 · 账号授权（auth.html「专用通道」卡契约）。

- GET  /api/my-accounts → {accounts:[{id,name,platform,enabled,verified,
                       daily_limit,limit,used_today,has_cookie}]}
- POST /api/my-accounts/add {platform,name,limit,cookie?,zone?} → {ok,msg}
- POST /api/my-accounts/limit {id,limit} / cookie {id,cookie} / verify {id}
     / toggle {id} / delete {id} → {ok,msg?}

Cookie 经 app.services.crypto **Fernet 加密**入 Account.cookie_blob（规约禁明文）。
verify 本期为模拟验证（标记 verified；真实 Playwright 登录校验随发布渠道驱动接入，
msg 如实说明）。id=平台短码+随机 12hex（主键即平台账号 ID，ADR-011 唯一性）。
"""
from __future__ import annotations

import secrets as _secrets
from datetime import date

from app.errors import NotFound, ValidationError
from app.extensions import db as _db
from app.logging import get_logger
from app.models import Account
from app.tenancy import TenantScopedRepository, current_tenant
from app.timeutil import natural_day_string

log = get_logger(__name__)

_PLATFORMS = ("meipian", "souhu", "baijiahao", "lieju", "wuba", "other")


class AccountService:
    def __init__(self, session=None) -> None:
        self.session = session or _db.session

    def list_accounts(self) -> dict:
        rows = TenantScopedRepository(Account, self.session).query().order_by(
            Account.id.asc()).all()
        accounts = [{"id": a.id, "name": a.name, "platform": a.platform,
                     "enabled": bool(a.enabled), "verified": bool(a.verified),
                     "status": a.status,
                     "daily_limit": int(a.daily_limit), "limit": int(a.daily_limit),
                     "used_today": self._used_today(a),
                     "has_cookie": bool(a.cookie_blob),
                     "yxer_id": a.yxer_id or None,
                     "zone": a.note or ""} for a in rows]
        return {"accounts": accounts}

    @staticmethod
    def _used_today(a: Account) -> int:
        # 与发布扣减/智能选号同一把尺子：Asia/Shanghai 自然日（ADR-013）。
        # 用 date.today() 会在容器跑 UTC 时，把"上海日已用"显示成 0——
        # 页面说没用，选号却按已用判定，展示与执行不一致。
        return int(a.used_today) if str(a.quota_date) == natural_day_string() else 0

    def add(self, body: dict) -> dict:
        from app.blueprints.distribution.service import PLATFORM_CODE

        name = str(body.get("name") or "").strip()[:128]
        if not name:
            raise ValidationError("账号名称必填")
        platform = str(body.get("platform") or "souhu")
        if platform not in _PLATFORMS:
            raise ValidationError(f"平台不支持：{platform}")
        try:
            limit = max(0, min(int(body.get("limit") or 0), 1000))
        except (TypeError, ValueError):
            limit = 0
        acc_id = f"{PLATFORM_CODE.get(platform, 'ac')}-{_secrets.token_hex(6)}"
        row = Account(id=acc_id, tenant_id=current_tenant(), platform=platform, name=name,
                      daily_limit=limit, used_today=0,
                      quota_date=date.fromisoformat(natural_day_string()),
                      note=str(body.get("zone") or "")[:64] or None)
        cookie = str(body.get("cookie") or "").strip()
        if cookie:
            from app.services.crypto import encrypt_text
            row.cookie_blob = encrypt_text(cookie)
        TenantScopedRepository(Account, self.session).add(row)
        self.session.commit()
        log.info("账号已添加", extra={"event": "account_added", "account_id": acc_id})
        return {"ok": True, "msg": f"已添加「{name}」（{acc_id}）", "id": acc_id}

    def _get(self, account_id: str) -> Account:
        row = TenantScopedRepository(Account, self.session).first(id=str(account_id or ""))
        if row is None:
            raise NotFound("账号不存在")
        return row

    def set_limit(self, account_id: str, limit) -> dict:
        try:
            limit = max(0, min(int(limit), 1000))
        except (TypeError, ValueError):
            raise ValidationError("limit 必须是整数") from None
        row = self._get(account_id)
        row.daily_limit = limit
        row.quota_date = date.fromisoformat(natural_day_string())
        row.used_today = 0
        self.session.commit()
        return {"ok": True}

    def set_cookie(self, account_id: str, cookie: str) -> dict:
        cookie = str(cookie or "").strip()
        if not cookie:
            raise ValidationError("cookie 不能为空")
        from app.services.crypto import encrypt_text
        row = self._get(account_id)
        row.cookie_blob = encrypt_text(cookie)
        self.session.commit()
        return {"ok": True, "msg": "Cookie 已加密更新"}

    def get_for_login(self, account_id: str) -> Account:
        """供扫码登录取账号行（租户作用域内，不存在即 404）。"""
        return self._get(account_id)

    def verify(self, account_id: str) -> dict:
        """真实验证：用已存 Cookie 实际访问平台，跑适配器的登录态判定。

        以前是"模拟验证"（只打 verified 标记）—— 用户以为校验过了，发布时
        照样被登录页拦下。现在校验不过就如实返回失败，不粉饰。
        """
        from datetime import timezone

        from app.timeutil import now_local

        from app.services.publish import login as qr_login

        row = self._get(account_id)
        result = qr_login.verify_cookie(row)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "登录态校验未通过"}
        row.verified = True
        row.verified_at = now_local().astimezone(timezone.utc).replace(tzinfo=None)
        if row.status in ("invalid", "frozen"):
            row.status = "active"
        self.session.commit()
        return {"ok": True, "msg": result.get("msg") or "登录态校验通过"}

    def toggle(self, account_id: str) -> dict:
        row = self._get(account_id)
        row.enabled = not bool(row.enabled)
        self.session.commit()
        return {"ok": True, "enabled": bool(row.enabled)}

    def delete(self, account_id: str) -> dict:
        row = self._get(account_id)
        TenantScopedRepository(Account, self.session).delete(row)
        self.session.commit()
        return {"ok": True}
