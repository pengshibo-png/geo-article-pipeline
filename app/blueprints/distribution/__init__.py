"""distribution 蓝图 —— 账号授权、渠道体检、排队发布、发布日志、定时计划。

**明确不做**：不生成内容
"""
from __future__ import annotations

from flask import Blueprint

bp = Blueprint("distribution", __name__)

from app.blueprints.distribution import api, views  # noqa: E402,F401  注册路由
