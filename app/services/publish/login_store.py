"""扫码登录的 Cookie 落库（从 login.py 拆出，守 A3 单文件 300 行预算）。

扫码线程抓到 Cookie 后交到这里：JSON → Fernet 加密 → 写 `Account.cookie_blob`
→ 标记 verified。与 login.py 的会话表 / 浏览器生命周期解耦，只通过 `notify`
回调把结果写回扫码会话（login._set）。

账号读取走**租户作用域查询**：`db.session.get(Account, id)` 按主键裸查、不过滤
tenant_id，读侧同样需要隔离（写侧有 tenancy 的 before_flush 兜底，读侧没有）。
"""
from __future__ import annotations

import json

from app.logging import get_logger

log = get_logger(__name__)


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def capture_and_save(login_id: str, account_id: str, tenant_id: str,
                     ctx, app, notify) -> None:
    """抓 Cookie → JSON → Fernet 加密 → 落库 → 标记 verified。

    `notify(login_id, **fields)` 由调用方传入（login._set），回报扫码会话状态。
    """
    cookies = ctx.cookies() or []
    if not cookies:
        notify(login_id, status="error", message="未取到任何 Cookie，请重试")
        return
    from app.services.crypto import encrypt_text
    from app.tenancy import TenantScopedRepository, tenant_scope

    with app.app_context(), tenant_scope(str(tenant_id or "")):
        from app.extensions import db
        from app.models import Account

        acc = (TenantScopedRepository(Account, db.session).query()
               .filter(Account.id == str(account_id)).first())
        if acc is None:
            notify(login_id, status="error", message="账号不存在")
            return
        acc.cookie_blob = encrypt_text(json.dumps(cookies, ensure_ascii=False))
        acc.verified = True
        acc.verified_at = _utcnow()
        if acc.status in ("invalid", "frozen"):
            acc.status = "active"
        db.session.commit()
    notify(login_id, status="done", logged_in=True,
           message=f"绑定成功：已保存 {len(cookies)} 条 Cookie（加密存储）")
    log.info("qr_login_captured", extra={"event": "qr_login_captured",
                                         "account_id": str(account_id)})


__all__ = ["capture_and_save"]
