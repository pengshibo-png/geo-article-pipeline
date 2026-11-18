"""console 蓝图 · JSON 接口（同 views 的职责，返回 JSON）。"""
from __future__ import annotations

from flask import jsonify

from app.blueprints.console import bp


@bp.get("/api/console/ping", endpoint="console_ping")
def console_ping():
    return jsonify({"ok": True, "blueprint": "console"})


@bp.get("/api/csrf", endpoint="csrf_token_api")
def csrf_token_api():
    """当前会话的 CSRF 令牌（公开：登录前也要能拿到，否则登录请求被自己拦）。

    令牌只在本会话内有效；配套校验见 `app/csrf.py`、`static/layout.js` 的 fetch 包装。
    """
    from app.csrf import get_token

    return jsonify({"ok": True, "token": get_token()})
