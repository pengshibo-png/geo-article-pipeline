"""会话鉴权 / 密码 / 登录限流（ADR-008）。

限流维度：**按账号**（ARCH-ADR-001 §4.1 问题 3 —— 实测 X-Forwarded-For 不生效）。
admin 切租户：落在 **session**，不入 URL（防参数篡改）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import session
from werkzeug.security import check_password_hash, generate_password_hash

from app.errors import RateLimited
from app.extensions import db
from app.logging import get_logger
from app.models import AppUser, LoginAttempt
from app.tenancy import TenantScopedRepository, tenant_scope

log = get_logger(__name__)

MAX_FAILS = 5
LOCK_MINUTES = 10


def hash_password(raw: str) -> str:
    return generate_password_hash(raw)


def verify_password(hashed: str, raw: str) -> bool:
    return check_password_hash(hashed, raw)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def find_user_for_login(username: str) -> AppUser | None:
    """★ 身份引导查询 —— 全系统**唯一**允许在无租户上下文下执行的查询。

    原因：登录时还不知道租户是谁，必须先用 username 定位用户。
    受控例外说明：V1 静态扫描只禁 `blueprints/` 下的裸查询；本函数位于 `security.py`，
    且**只碰身份域的 app_user 表**，不触及任何业务数据。
    """
    return db.session.query(AppUser).filter(AppUser.username == username).first()


def authenticate(username: str, password: str, *, ip: str | None = None):
    """返回 (user, None) 或 (None, error_code)；被锁定则抛 RateLimited。"""
    user = find_user_for_login(username)
    if user is None or not user.active:
        return None, "invalid_credentials"

    with tenant_scope(user.tenant_id):
        repo = TenantScopedRepository(LoginAttempt)
        attempt = repo.first()
        now = _utcnow()
        if attempt and attempt.locked_until and attempt.locked_until > now:
            raise RateLimited("尝试次数过多，请 10 分钟后再试")

        if verify_password(user.password_hash, password):
            if attempt is not None:
                attempt.fail_count = 0
                attempt.locked_until = None
                attempt.last_ip = ip
            return user, None

        if attempt is None:
            attempt = repo.add(LoginAttempt(tenant_id=user.tenant_id, fail_count=0, last_ip=ip))
        attempt.fail_count += 1
        attempt.last_ip = ip
        if attempt.fail_count >= MAX_FAILS:
            attempt.locked_until = now + timedelta(minutes=LOCK_MINUTES)
        log.warning("login_failed", extra={"event": "login_failed", "actor": username})
        return None, "invalid_credentials"


def login_session(user: AppUser) -> None:
    # 保留 CSRF 令牌：登录前页面已渲染的令牌要继续有效，否则刚拿到的令牌
    # 会在登录那一刻被 clear() 抹掉、紧接着的 POST 被自己拦掉。
    csrf = session.get("_csrf")
    session.clear()
    if csrf:
        session["_csrf"] = csrf
    session["uid"] = user.tenant_id          # 会话里的租户身份
    session["uname"] = user.username
    session["role"] = user.role


def logout_session() -> None:
    session.clear()


def switch_view_tenant(tenant_id: str, reason: str | None = None) -> None:
    """admin 切租户看数据：写 session，**不写 URL**（ADR-008）。"""
    session["view_tenant"] = tenant_id
    session["view_reason"] = reason
