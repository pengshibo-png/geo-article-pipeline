"""CSRF 防护（自研，零额外依赖）。

为什么不引 flask-wtf：桌面包每加一个依赖都增加打包体积与风险，而本应用
全部是 JSON API，用「会话令牌 + 自定义请求头」即可，不需要表单渲染那套。

机制：
- 每个会话（**含匿名会话**）持一个随机令牌，存在 `session["_csrf"]`；
- 页面 `<meta name="csrf-token">` 暴露它，`layout.js` 里的 fetch 包装对同源的
  非安全方法（POST/PUT/PATCH/DELETE）自动带上 `X-CSRFToken` 头；
- `before_request` 校验，不符 → 403。

可关闭：`GEO_CSRF_ENABLED=0`（测试环境默认关闭，避免给既有测试套上令牌）。
"""
from __future__ import annotations

import hmac
import secrets

from flask import current_app, request, session

from app.errors import Forbidden
from app.logging import get_logger

log = get_logger(__name__)

SESSION_KEY = "_csrf"
HEADER = "X-CSRFToken"
FORM_FIELD = "csrf_token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def get_token() -> str:
    """取（并按需生成）当前会话的 CSRF 令牌。

    在 before_request 里对每个请求调用，保证匿名会话也先拿到令牌 —— 否则
    登录页的第一次 POST 会因为没有前置 GET 而被自己拦掉。
    """
    t = session.get(SESSION_KEY)
    if not t:
        t = secrets.token_urlsafe(32)
        session[SESSION_KEY] = t
    return t


def _presented_token() -> str:
    got = request.headers.get(HEADER) or ""
    if not got and request.form:
        got = request.form.get(FORM_FIELD) or ""
    if not got and request.is_json:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            got = str(body.get(FORM_FIELD) or "")
    return got


def install_csrf(app) -> None:
    """注册 before_request 钩子 + 模板全局函数。"""
    app.jinja_env.globals["csrf_token"] = get_token

    @app.before_request
    def _ensure_token():
        if current_app.config.get("CSRF_ENABLED", True):
            get_token()                      # 保证会话里始终有令牌
        return None

    @app.before_request
    def _verify():
        if not current_app.config.get("CSRF_ENABLED", True):
            return None
        if request.method in _SAFE_METHODS:
            return None
        expected = str(session.get(SESSION_KEY) or "")
        got = _presented_token()
        if not expected or not got or not hmac.compare_digest(expected, got):
            log.warning("csrf_rejected", extra={
                "event": "csrf_rejected", "path": request.path,
                "method": request.method})
            raise Forbidden("CSRF 校验失败：请刷新页面后重试")
        return None


__all__ = ["get_token", "install_csrf"]
