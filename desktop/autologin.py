"""桌面形态自动登录：抢在 tenancy 的租户绑定钩子**之前**把本地用户写进 session。

tenancy.py 的 `init_tenancy` 字面量被契约测试钉死（tests/contract/test_guards.py:79），
一行不改；Flask 的 before_request 按**注册顺序**执行，所以这里用
`before_request_funcs[None].insert(0, ...)` 插队到最前。
"""
from __future__ import annotations

import os

from flask import Flask, request, session

from app.logging import get_logger

log = get_logger(__name__)


def install_autologin(app: Flask, username: str = "local") -> None:
    """GEO_DESKTOP=1 才生效 —— SaaS 服务器路径完全不受影响。"""
    if os.environ.get("GEO_DESKTOP") != "1":
        return

    from app.security import find_user_for_login, login_session
    from app.tenancy import PUBLIC_ENDPOINTS

    def _auto_login():                        # noqa: ANN202 - Flask 钩子
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("static"):
            return None
        if session.get("uid"):
            return None                       # 快路径：已有会话，零查询
        user = find_user_for_login(username)  # 身份域引导查询（security.py 受控例外）
        if user is not None and user.active:
            login_session(user)
        return None

    app.before_request_funcs.setdefault(None, []).insert(0, _auto_login)
