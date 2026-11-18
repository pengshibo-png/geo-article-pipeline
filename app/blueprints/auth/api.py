"""auth 蓝图 · JSON 接口（登录 / 登出 / admin 切租户视角）。

端点用短名（如 `login_api`），Flask 会自动加上蓝图前缀，最终端点名
`auth.login_api` 与 `tenancy.PUBLIC_ENDPOINTS` 的白名单逐字对应。
"""
from __future__ import annotations

from flask import jsonify, request, session

from app.blueprints.auth import bp
from app.blueprints.auth.service import RegisterService
from app.errors import Forbidden, ValidationError
from app.security import authenticate, login_session, logout_session, switch_view_tenant
from app.services import directory
from app.tenancy import admin_scope, current_tenant, write_audit


@bp.post("/api/auth/login", endpoint="login_api")
def login_api():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        raise ValidationError("用户名与密码必填")

    user, error = authenticate(username, password, ip=request.remote_addr)
    if user is None:
        return jsonify({"ok": False, "error": error, "message": "账号或密码错误"}), 401
    login_session(user)
    return jsonify({"ok": True, "tenant": user.tenant_id, "role": user.role})


@bp.post("/api/auth/logout", endpoint="logout_api")
def logout_api():
    logout_session()
    return jsonify({"ok": True})


@bp.post("/api/tenants/register", endpoint="register_api")
def register_api():
    """一次填表铺全量：租户目录 + 登录账号 + 品牌骨架 + 标题配置。

    恒 200 + {ok/message}（原站页面读 d.ok 分支，不走 HTTP 错误码）。
    """
    body = request.get_json(silent=True) or {}
    return jsonify(RegisterService().register(body))


@bp.get("/api/me", endpoint="me_api")
def me_api():
    """layout.js 顶栏/侧栏消费（loadMe）：role 决定管理端导航，tenant_name 显示客户名。

    admin 切租户视角时 current_tenant() 已是被查看租户，tenant_name 随之切换；
    tenants 列表只对 admin 返回（侧栏租户下拉用），客户恒为空。
    """
    role = session.get("role", "client")
    tid = current_tenant()
    t = directory.get_tenant(tid)
    return jsonify({
        "ok": True,
        "role": role,
        "tenant": tid,
        "name": session.get("uname", ""),
        "tenant_name": (t or {}).get("name", tid),
        "tenants": directory.list_tenants() if role == "admin" else [],
    })


@bp.post("/api/admin/view-tenant", endpoint="view_tenant_api")
def view_tenant_api():
    """admin 跨租户视角入口。**必须给 reason**（ADR §2.4）。"""
    payload = request.get_json(silent=True) or {}
    if session.get("role") != "admin":
        raise Forbidden("仅 admin 可切换租户视角")
    tenant_id = str(payload.get("tenant_id") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if not tenant_id or not reason:
        raise ValidationError("tenant_id 与 reason 均必填")

    actor = session.get("uname", "-")
    with admin_scope(tenant_id, reason=reason, actor=actor):
        write_audit(actor, tenant_id, reason, request.endpoint)
    switch_view_tenant(tenant_id, reason)
    return jsonify({"ok": True, "view_tenant": tenant_id, "cross_tenant": True})
