"""auth 蓝图 —— 注册/登录/登出/会话、租户开通、admin 跨租户视角。

**明确不做**：不碰内容数据
"""
from __future__ import annotations

from flask import Blueprint

bp = Blueprint("auth", __name__)

from app.blueprints.auth import api, views  # noqa: E402,F401  注册路由
